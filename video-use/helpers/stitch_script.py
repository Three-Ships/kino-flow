"""Stitch best takes across MULTIPLE videos to deliver a target script.

This is best_take.py extended to a per-sentence search across many videos:

  1. Resolve video list (a folder is expanded to *.mp4/mov/mkv; or pass paths)
  2. For each video — transcribe (cached), detect main speaker, segment takes
     (silence > 1.5s = take boundary). Reused verbatim from best_take.py.
  3. Split the script into sentences on . ! ? boundaries.
  4. For each sentence — score every take across every video (token-level
     Levenshtein, normalized). Pick the highest-scoring take. "Pure best
     match" — no continuity bonus, no per-video constraint.
  5. Cut each winning take with NVENC + 30 ms audio fades into a temp MP4.
  6. Concat all winning segments into the final stitched output.

Per TRIMMING_PHILOSOPHY.md: hard cuts, 30 ms audio fades, HEAD_PAD=0.05,
TAIL_PAD=0.08. Per CLAUDE.md: closest-match-wins is settled, no approval
gate, no clarifying questions. Always prints the per-sentence scoring table.

Usage:
    python helpers/stitch_script.py --videos folder/ --script s.txt
    python helpers/stitch_script.py --videos a.mp4 b.mp4 c.mp4 --script s.txt
    python helpers/stitch_script.py --videos folder/ --script s.txt --output out.mp4 --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Reuse best_take's pieces — segmentation, scoring, cutting, transcript load.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from best_take import (  # type: ignore
    Take,
    cut_take,
    detect_main_speaker,
    load_or_run_transcript,
    score_take,
    segment_takes,
    tokenize,
)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v"}


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def resolve_videos(inputs: list[Path]) -> list[Path]:
    """Expand folders to their video children; keep explicit files as-is.
    Returns absolute paths, deduplicated, sorted by name for stable output.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for p in inputs:
        p = p.resolve()
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if child.is_file() and child.suffix.lower() in VIDEO_EXTS:
                    if child not in seen:
                        out.append(child); seen.add(child)
        elif p.is_file():
            if p.suffix.lower() not in VIDEO_EXTS:
                sys.exit(f"not a recognized video: {p}")
            if p not in seen:
                out.append(p); seen.add(p)
        else:
            sys.exit(f"video path not found: {p}")
    if not out:
        sys.exit("no videos resolved from --videos argument")
    return out


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")

def split_sentences(text: str) -> list[str]:
    """Split a script into sentences. Conservative — splits on . ! ? followed
    by whitespace and a capital/quote/digit. Handles "Mr. Smith said." style
    cases by requiring the next token to look like a new sentence.
    """
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned:
        return []
    parts = _SENT_SPLIT.split(cleaned)
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------
# Per-video take preparation
# --------------------------------------------------------------------------

@dataclass
class VideoTakes:
    video: Path
    main_speaker: str
    takes: list[Take]


def prepare_video(video: Path, edit_dir: Path) -> VideoTakes:
    print(f"  prepping: {video.name}")
    transcript = load_or_run_transcript(video, edit_dir)
    words = transcript.get("words") or []
    main_speaker = detect_main_speaker(words, video)
    main_words = [w for w in words if w.get("speaker_id") == main_speaker]
    takes = segment_takes(main_words)
    print(f"    main speaker = {main_speaker} · {len(takes)} take(s)")
    return VideoTakes(video=video, main_speaker=main_speaker, takes=takes)


# --------------------------------------------------------------------------
# Per-sentence assignment
# --------------------------------------------------------------------------

@dataclass
class Pick:
    sentence_idx: int
    sentence: str
    video: Path
    take: Take
    score: float


def pick_best_takes(sentences: list[str], pool: list[VideoTakes]) -> list[Pick]:
    picks: list[Pick] = []
    for i, sent in enumerate(sentences):
        sent_tokens = tokenize(sent)
        if not sent_tokens:
            continue
        best: tuple[float, VideoTakes | None, Take | None] = (-1.0, None, None)
        for vt in pool:
            for tk in vt.takes:
                s = score_take(tk, sent_tokens)
                if s > best[0]:
                    best = (s, vt, tk)
        if best[1] is None or best[2] is None:
            sys.exit(f"no candidate take for sentence #{i}: {sent[:80]!r}")
        picks.append(Pick(
            sentence_idx=i, sentence=sent,
            video=best[1].video, take=best[2], score=best[0],
        ))
    return picks


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------

def _concat(segments: list[Path], output: Path) -> None:
    """ffmpeg concat demuxer. All segments share encoder/params from cut_take,
    so stream-copy is safe and fast.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for seg in segments:
            posix = str(seg.resolve()).replace("\\", "/")
            f.write(f"file '{posix}'\n")
        list_path = Path(f.name)
    try:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart",
            str(output),
        ]
        subprocess.run(cmd, check=True)
    finally:
        try: list_path.unlink()
        except OSError: pass


def render_stitched(picks: list[Pick], output: Path, edit_dir: Path) -> list[Path]:
    tmp_dir = edit_dir / "stitch_tmp" / datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    segs: list[Path] = []
    for i, pk in enumerate(picks):
        seg_path = tmp_dir / f"seg_{i:03d}.mp4"
        cut_take(pk.video, pk.take, seg_path)
        segs.append(seg_path)
    _concat(segs, output)
    return segs


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run(
    video_inputs: list[Path],
    script_path: Path,
    output: Path | None,
    edit_dir: Path,
    emit_json: bool,
    keep_temps: bool,
) -> dict:
    if not script_path.exists():
        sys.exit(f"script not found: {script_path}")
    videos = resolve_videos(video_inputs)
    sentences = split_sentences(script_path.read_text(encoding="utf-8"))
    if not sentences:
        sys.exit("script split to zero sentences — is the file empty?")

    print(f"\nVideos ({len(videos)}):")
    for v in videos:
        print(f"  · {v.name}")
    print(f"Script: {len(sentences)} sentence(s)\n")

    pool = [prepare_video(v, edit_dir) for v in videos]

    print()
    picks = pick_best_takes(sentences, pool)

    print(f"\n{'#':>2}  {'video':<32}  {'start':>7}  {'end':>7}  {'score':>6}  sentence")
    for pk in picks:
        vname = pk.video.name[:30] + ("…" if len(pk.video.name) > 30 else "")
        preview = pk.sentence[:50].replace("\n", " ") + ("…" if len(pk.sentence) > 50 else "")
        print(f"{pk.sentence_idx:>2}  {vname:<32}  "
              f"{pk.take.start:>7.2f}  {pk.take.end:>7.2f}  {pk.score:>6.3f}  {preview}")
    print()

    if output is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = edit_dir / f"stitched_{stamp}.mp4"
    segs = render_stitched(picks, output, edit_dir)
    print(f"wrote: {output}")

    if not keep_temps:
        for s in segs:
            try: s.unlink()
            except OSError: pass

    result = {
        "videos": [str(v) for v in videos],
        "script": str(script_path),
        "output": str(output),
        "sentences": [
            {
                "index": pk.sentence_idx,
                "text": pk.sentence,
                "video": str(pk.video),
                "take_index": pk.take.index,
                "start": pk.take.start, "end": pk.take.end,
                "score": pk.score,
                "take_text": pk.take.text,
            }
            for pk in picks
        ],
        "segment_count": len(picks),
        "total_duration": sum((pk.take.end - pk.take.start) for pk in picks),
    }
    if emit_json:
        print(json.dumps(result, indent=2))
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--videos", nargs="+", required=True, type=Path,
        help="folder of source videos OR explicit paths (mp4/mov/mkv/m4v)",
    )
    ap.add_argument("--script", type=Path, required=True, help="path to .txt script")
    ap.add_argument("--output", type=Path, default=None,
                    help="output MP4 (default: videos/edit/stitched_<timestamp>.mp4)")
    ap.add_argument("--edit-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent.parent / "videos" / "edit",
                    help="dir holding transcripts/ cache and stitch_tmp/")
    ap.add_argument("--json", action="store_true", help="also print machine-readable JSON")
    ap.add_argument("--keep-temps", action="store_true",
                    help="leave per-segment MP4s in stitch_tmp/ for inspection")
    args = ap.parse_args()
    run(
        video_inputs=args.videos,
        script_path=args.script.resolve(),
        output=args.output.resolve() if args.output else None,
        edit_dir=args.edit_dir.resolve(),
        emit_json=args.json,
        keep_temps=args.keep_temps,
    )


if __name__ == "__main__":
    main()
