"""Pick the single best take that delivers a target script.

Given a video that may contain multiple takes of the same line and/or
multiple speakers, find the **one** contiguous take that is the closest
verbatim match to the target script and emit a cut MP4 of just that take.

Pipeline:

  1. transcribe (cached) — uses ElevenLabs Scribe with diarize=true and
     word-level timestamps, so each word has a speaker_id label
  2. detect the main speaker by loudness — per-speaker average RMS over the
     source audio at each word's time window. Main = loudest cluster
  3. discard non-main-speaker words entirely. Side speakers are filtered
     before take detection so a stray comment from the room doesn't
     fragment a take
  4. segment main-speaker words into candidate takes by silence gap > 1.5s
  5. score each take vs the script by token-level Levenshtein distance,
     normalized to [0,1] (1.0 = verbatim match, 0.0 = nothing in common)
  6. pick the highest-scoring take and cut [start - HEAD_PAD, end + TAIL_PAD]
     with NVENC + 30 ms audio fades, per TRIMMING_PHILOSOPHY.md

No approval gate. Prints the candidate scoring table either way so you can
see why a take won.

Usage:
    python helpers/best_take.py <video> --script script.txt
    python helpers/best_take.py <video> --script script.txt --output out.mp4
    python helpers/best_take.py <video> --script script.txt --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Pad and fade constants — keep aligned with TRIMMING_PHILOSOPHY.md.
HEAD_PAD = 0.05
TAIL_PAD = 0.08
FADE_MS = 30
SILENCE_GAP_S = 1.5  # silence > this between main-speaker words = new take


# --------------------------------------------------------------------------
# Audio loudness analysis
# --------------------------------------------------------------------------

def _decode_mono_16k_int16(video_path: Path) -> tuple[bytes, int]:
    """Decode the video's audio track to mono 16-bit PCM @ 16 kHz, in memory.
    Returns (samples_bytes, sample_rate). Raises on ffmpeg failure.
    """
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        "-f", "s16le", "-c:a", "pcm_s16le", "-",
    ]
    proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stdout, 16000


def _word_rms(samples: bytes, sample_rate: int, start_s: float, end_s: float) -> float:
    """Compute RMS of int16 PCM in [start_s, end_s]. Returns 0.0 on bad range."""
    n = len(samples) // 2  # int16 = 2 bytes
    if n == 0 or end_s <= start_s:
        return 0.0
    s_idx = max(0, int(start_s * sample_rate))
    e_idx = min(n, int(end_s * sample_rate))
    if e_idx <= s_idx:
        return 0.0
    # Walk int16 samples without numpy — quick and dependency-free.
    import struct
    span = samples[s_idx * 2:e_idx * 2]
    count = len(span) // 2
    if count == 0:
        return 0.0
    fmt = f"<{count}h"
    sq_sum = 0.0
    for v in struct.unpack(fmt, span):
        sq_sum += v * v
    return (sq_sum / count) ** 0.5


def detect_main_speaker(words: list[dict], video_path: Path) -> str:
    """Return the speaker_id with the highest mean per-word RMS.

    Falls back to the speaker with the most word events if audio decoding
    fails. If only one speaker is present, returns it without decoding.
    """
    speakers = sorted({w.get("speaker_id") for w in words if w.get("type") == "word" and w.get("speaker_id")})
    if not speakers:
        raise RuntimeError("transcript has no diarized word events")
    if len(speakers) == 1:
        return speakers[0]

    try:
        pcm, sr = _decode_mono_16k_int16(video_path)
    except subprocess.CalledProcessError:
        # Fallback: most-words wins.
        counts: dict[str, int] = {}
        for w in words:
            if w.get("type") != "word":
                continue
            sid = w.get("speaker_id")
            if sid:
                counts[sid] = counts.get(sid, 0) + 1
        return max(counts, key=counts.get)

    sums: dict[str, float] = {sid: 0.0 for sid in speakers}
    counts: dict[str, int] = {sid: 0 for sid in speakers}
    for w in words:
        if w.get("type") != "word":
            continue
        sid = w.get("speaker_id")
        if not sid:
            continue
        rms = _word_rms(pcm, sr, float(w.get("start", 0.0)), float(w.get("end", 0.0)))
        sums[sid] += rms
        counts[sid] += 1
    means = {sid: (sums[sid] / counts[sid]) if counts[sid] else 0.0 for sid in speakers}
    return max(means, key=means.get)


# --------------------------------------------------------------------------
# Take segmentation + scoring
# --------------------------------------------------------------------------

@dataclass
class Take:
    index: int
    start: float
    end: float
    words: list[dict] = field(default_factory=list)
    score: float = 0.0
    text: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def segment_takes(main_words: list[dict], gap_s: float = SILENCE_GAP_S) -> list[Take]:
    takes: list[Take] = []
    current: list[dict] = []
    last_end = -1e9
    for w in main_words:
        if w.get("type") != "word":
            continue
        ws = float(w.get("start", 0.0))
        if current and (ws - last_end) > gap_s:
            takes.append(_finalize_take(len(takes), current))
            current = []
        current.append(w)
        last_end = float(w.get("end", ws))
    if current:
        takes.append(_finalize_take(len(takes), current))
    return takes


def _finalize_take(idx: int, words: list[dict]) -> Take:
    start = float(words[0].get("start", 0.0))
    end = float(words[-1].get("end", start))
    text = " ".join(w.get("text", "").strip() for w in words if w.get("text", "").strip())
    return Take(index=idx, start=start, end=end, words=words, text=text)


_TOKEN_RE = re.compile(r"[a-z0-9']+", re.IGNORECASE)

def tokenize(s: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(s)]


def levenshtein(a: list[str], b: list[str]) -> int:
    """Token-level edit distance. O(len(a)*len(b)) DP, two-row buffer."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    cur = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur[0] = i
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev, cur = cur, prev
    return prev[len(b)]


def score_take(take: Take, script_tokens: list[str]) -> float:
    take_tokens = tokenize(take.text)
    if not take_tokens or not script_tokens:
        return 0.0
    dist = levenshtein(take_tokens, script_tokens)
    norm = max(len(script_tokens), len(take_tokens))
    return 1.0 - (dist / norm)


# --------------------------------------------------------------------------
# Cut + encode
# --------------------------------------------------------------------------

def _encoder_args() -> list[str]:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from render import select_encoder  # type: ignore
        enc = select_encoder()
    except Exception:
        enc = "x264"
    if enc.startswith("nvenc"):
        return [
            "-c:v", "h264_nvenc", "-preset", "p6",
            "-rc", "vbr", "-cq", "19", "-b:v", "0",
            "-tune", "hq", "-spatial_aq", "1",
        ]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


def cut_take(video: Path, take: Take, output: Path) -> None:
    """Encode [take.start - HEAD_PAD, take.end + TAIL_PAD] with audio fades."""
    src_dur_probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        check=True, capture_output=True, text=True,
    )
    src_dur = float(src_dur_probe.stdout.strip() or 0.0)
    start = max(0.0, take.start - HEAD_PAD)
    end = min(src_dur if src_dur else (take.end + TAIL_PAD), take.end + TAIL_PAD)
    duration = max(0.05, end - start)
    fade = FADE_MS / 1000.0
    fade_out_at = max(0.0, duration - fade)
    afilter = f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={fade_out_at:.3f}:d={fade:.3f}"
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start:.4f}", "-i", str(video),
        "-t", f"{duration:.4f}",
        *_encoder_args(),
        "-c:a", "aac", "-b:a", "192k",
        "-af", afilter,
        "-movflags", "+faststart",
        str(output),
    ]
    subprocess.run(cmd, check=True)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def load_or_run_transcript(video: Path, edit_dir: Path) -> dict:
    transcripts_dir = edit_dir / "transcripts"
    out_path = transcripts_dir / f"{video.stem}.json"
    if not out_path.exists():
        # Delegate to transcribe.py — it handles caching, audio extraction,
        # and the Scribe upload with diarize+word-timestamps already enabled.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from transcribe import transcribe_one, load_api_key  # type: ignore
        transcribe_one(video, edit_dir, load_api_key(), verbose=True)
    return json.loads(out_path.read_text(encoding="utf-8"))


def run(
    video: Path,
    script_path: Path,
    output: Path | None,
    edit_dir: Path,
    emit_json: bool,
) -> dict:
    if not video.exists():
        sys.exit(f"video not found: {video}")
    if not script_path.exists():
        sys.exit(f"script not found: {script_path}")
    script_text = script_path.read_text(encoding="utf-8")
    script_tokens = tokenize(script_text)
    if not script_tokens:
        sys.exit("script tokenized to zero words — is the file empty?")

    transcript = load_or_run_transcript(video, edit_dir)
    words = transcript.get("words") or []

    main_speaker = detect_main_speaker(words, video)
    main_words = [w for w in words if w.get("speaker_id") == main_speaker]

    takes = segment_takes(main_words)
    if not takes:
        sys.exit(f"no takes detected for main speaker {main_speaker}")

    for t in takes:
        t.score = score_take(t, script_tokens)
    takes.sort(key=lambda t: t.score, reverse=True)
    best = takes[0]

    print(f"\nMain speaker: {main_speaker}  (filtered out: "
          f"{sorted({w.get('speaker_id') for w in words if w.get('type') == 'word'} - {main_speaker})})")
    print(f"Script: {len(script_tokens)} tokens · {len(takes)} candidate take(s)\n")
    print(f"  {'#':>2}  {'start':>8}  {'end':>8}  {'dur':>6}  {'score':>6}  preview")
    for t in takes:
        preview = t.text[:60].replace("\n", " ") + ("…" if len(t.text) > 60 else "")
        flag = "  <- BEST" if t is best else ""
        print(f"  {t.index:>2}  {t.start:>8.2f}  {t.end:>8.2f}  {t.duration:>6.2f}  "
              f"{t.score:>6.3f}  {preview}{flag}")
    print()

    if output is None:
        output = video.parent / f"{video.stem}_take.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    cut_take(video, best, output)
    print(f"wrote: {output}")

    result = {
        "video": str(video),
        "script": str(script_path),
        "main_speaker": main_speaker,
        "takes": [
            {
                "index": t.index,
                "start": t.start, "end": t.end, "duration": t.duration,
                "score": t.score, "text": t.text,
                "is_best": t is best,
            }
            for t in takes
        ],
        "best": {
            "index": best.index,
            "start": best.start, "end": best.end, "score": best.score,
        },
        "output": str(output),
        "head_pad": HEAD_PAD, "tail_pad": TAIL_PAD, "fade_ms": FADE_MS,
    }
    if emit_json:
        print(json.dumps(result, indent=2))
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path, help="source video (MP4)")
    ap.add_argument("--script", type=Path, required=True, help="path to .txt script")
    ap.add_argument("--output", type=Path, default=None,
                    help="output MP4 (default: <video_stem>_take.mp4 next to source)")
    ap.add_argument("--edit-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent.parent / "videos" / "edit",
                    help="dir holding transcripts/ cache")
    ap.add_argument("--json", action="store_true", help="also print machine-readable JSON")
    args = ap.parse_args()
    run(args.video.resolve(), args.script.resolve(), args.output, args.edit_dir, args.json)


if __name__ == "__main__":
    main()
