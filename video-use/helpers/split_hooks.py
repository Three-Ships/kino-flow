"""Split a multi-hook take into per-hook MP4 deliverables.

A "hook" is a self-contained ad opening. Talent often shoots several hooks
back-to-back in one take, sometimes re-recording the same hook 2–3 times.
This helper slices each hook out as its own MP4 with a descriptive name.

Two modes:

1. **--auto** (free, deterministic): walk the cached transcript phrases and
   split wherever an inter-phrase silence exceeds ``--silence-threshold``
   (default 1.5 s). Drop fragments shorter than ``--min-duration`` (default
   4.0 s). No model reasoning — fast and predictable.

2. **--edl <path>**: caller (Claude) supplies the hook EDL directly:

       [
         {"name": "renewal_offer_take1", "start": 0.92, "end": 32.40},
         {"name": "renewal_offer_take2", "start": 35.10, "end": 65.85, "best": true},
         ...
       ]

   This is how Claude wraps the smart path: read the transcript, group takes
   of the same hook, mark the best one per group, write the EDL, then call
   this helper.

Output files land in ``<edit_dir>/hooks/<basename>/<NN>_<name>.mp4`` with a
sidecar manifest ``hooks_manifest.json`` listing every produced clip plus
metadata (duration, source range, take group, marked-best flag).

Usage:
    python helpers/split_hooks.py <video> --auto --json
    python helpers/split_hooks.py <video> --auto --silence-threshold 2.0 --min-duration 5.0
    python helpers/split_hooks.py <video> --edl hooks.json --json
    python helpers/split_hooks.py <video> --edl - --json < hooks.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------- helpers shared with rest of toolchain ----------------------------

def find_transcript(video: Path, edit_dir: Path) -> Path | None:
    candidates = [
        edit_dir / "transcripts" / f"{video.stem}.json",
        # transcribe.py occasionally writes to a nested edit/edit/ — check both.
        edit_dir / "edit" / "transcripts" / f"{video.stem}.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def slugify(text: str, max_len: int = 32) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return s[:max_len] or "hook"


# ---------- auto mode --------------------------------------------------------

def detect_hooks_auto(
    transcript: dict,
    silence_threshold: float,
    min_duration: float,
    head_pad: float,
    tail_pad: float,
) -> list[dict]:
    """Walk word-level transcript; cut wherever inter-word gap > threshold."""
    words = [
        w for w in transcript.get("words", [])
        if w.get("type") == "word" and w.get("start") is not None
        and w.get("end") is not None and (w.get("text") or "").strip()
    ]
    if not words:
        return []

    groups: list[list[dict]] = [[words[0]]]
    for prev, cur in zip(words, words[1:]):
        gap = cur["start"] - prev["end"]
        if gap >= silence_threshold:
            groups.append([cur])
        else:
            groups[-1].append(cur)

    hooks: list[dict] = []
    for g in groups:
        start = max(0.0, g[0]["start"] - head_pad)
        end = g[-1]["end"] + tail_pad
        dur = end - start
        if dur < min_duration:
            continue
        first_words = " ".join(
            (w.get("text") or "").strip() for w in g[:5]
        )
        hooks.append({
            "name":    slugify(first_words),
            "start":   round(start, 3),
            "end":     round(end, 3),
            "duration": round(dur, 3),
            "preview": first_words[:80],
        })
    return hooks


# ---------- encoding ---------------------------------------------------------

def encode_segment(
    src: Path,
    start: float,
    end: float,
    out_path: Path,
    encoder: str,
    fade_ms: int,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dur = end - start
    fade_s = fade_ms / 1000.0
    # 30 ms fades on both ends to kill click artifacts at boundaries.
    af = (
        f"afade=t=in:st=0:d={fade_s},"
        f"afade=t=out:st={max(0.0, dur - fade_s):.3f}:d={fade_s}"
    )
    # Two-stage seek for frame-accurate cut without full-decode penalty:
    #   - `-ss BEFORE -i` to a keyframe ~2 s before requested start (fast seek)
    #   - `-ss AFTER -i` for the precise remainder (frame-accurate decode-and-drop)
    # Also explicitly use `setpts/asetpts=PTS-STARTPTS` and
    # `-avoid_negative_ts make_zero` so the output's video and audio streams
    # start cleanly at t=0 with no drift between them.
    coarse_seek = max(0.0, start - 2.0)
    fine_seek   = start - coarse_seek
    fine_to     = fine_seek + dur

    vf = "setpts=PTS-STARTPTS"
    af_full = f"asetpts=PTS-STARTPTS,{af}"

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-ss", f"{coarse_seek:.3f}",
        "-i", str(src),
        "-ss", f"{fine_seek:.3f}",
        "-to", f"{fine_to:.3f}",
        "-vf", vf,
        "-af", af_full,
        "-avoid_negative_ts", "make_zero",
        "-fflags", "+genpts",
        "-c:v", encoder,
    ]
    if encoder.startswith("h264_nvenc"):
        cmd.extend(["-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"])
    else:
        cmd.extend(["-preset", "medium", "-crf", "20"])
    cmd.extend([
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"ffmpeg failed encoding {out_path.name} (exit {proc.returncode})")


# ---------- main -------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Split a multi-hook take into per-hook MP4s.")
    ap.add_argument("video", help="source video (typically a synced talking head)")
    ap.add_argument("--edit-dir", default=None,
                    help="edit dir (default: <video_parent>/edit)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--auto", action="store_true",
                      help="auto-detect hooks via silence between phrases")
    mode.add_argument("--edl",
                      help="path to a hook EDL JSON file (or '-' for stdin)")
    ap.add_argument("--silence-threshold", type=float, default=1.5,
                    help="auto mode: split on inter-word silence ≥ this many seconds (default 1.5)")
    ap.add_argument("--min-duration", type=float, default=4.0,
                    help="auto mode: drop hooks shorter than this many seconds (default 4.0)")
    ap.add_argument("--head-pad", type=float, default=0.10,
                    help="seconds to extend BEFORE first word of a hook (default 0.10)")
    ap.add_argument("--tail-pad", type=float, default=0.30,
                    help="seconds to extend AFTER last word of a hook (default 0.30)")
    ap.add_argument("--fade-ms", type=int, default=30,
                    help="audio fade in/out length in ms (default 30)")
    ap.add_argument("--encoder", default="h264_nvenc",
                    help="ffmpeg video encoder (default h264_nvenc)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned hook EDL without encoding")
    ap.add_argument("--json", dest="emit_json", action="store_true",
                    help="machine-readable result on stdout")
    args = ap.parse_args()

    src = Path(args.video).resolve()
    if not src.exists():
        raise SystemExit(f"video not found: {src}")
    if args.edit_dir:
        edit_dir = Path(args.edit_dir).resolve()
    elif src.parent.name == "edit":
        # Already inside an `edit/` directory — don't nest another one.
        edit_dir = src.parent
    else:
        edit_dir = src.parent / "edit"
    out_dir = edit_dir / "hooks" / src.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    src_dur = ffprobe_duration(src)

    # --- gather hook list -----------------------------------------------------
    if args.auto:
        tr_path = find_transcript(src, edit_dir)
        if tr_path is None:
            raise SystemExit(
                f"no transcript found for {src.name} — run transcribe.py first "
                f"(checked {edit_dir}/transcripts/ and {edit_dir}/edit/transcripts/)"
            )
        transcript = json.loads(tr_path.read_text(encoding="utf-8"))
        hooks = detect_hooks_auto(
            transcript,
            args.silence_threshold,
            args.min_duration,
            args.head_pad,
            args.tail_pad,
        )
        if not hooks:
            raise SystemExit(
                "auto detection found 0 hooks. Try lowering --silence-threshold "
                "or --min-duration, or use --edl to specify hooks manually."
            )
    else:
        raw = sys.stdin.read() if args.edl == "-" else Path(args.edl).read_text(encoding="utf-8")
        hooks = json.loads(raw)
        if not isinstance(hooks, list):
            raise SystemExit("EDL must be a JSON array of hook objects")
        for i, h in enumerate(hooks):
            for k in ("start", "end"):
                if k not in h:
                    raise SystemExit(f"EDL hook {i} missing key: {k}")
            h.setdefault("name", f"hook_{i:02d}")
            if h["end"] <= h["start"]:
                raise SystemExit(f"EDL hook {i}: end <= start")
            if h["end"] > src_dur + 0.05:
                raise SystemExit(
                    f"EDL hook {i}: end {h['end']} exceeds source duration {src_dur:.3f}"
                )
            h["duration"] = round(h["end"] - h["start"], 3)

    # Sort by start time for sane numbering.
    hooks.sort(key=lambda h: h["start"])

    if args.dry_run:
        print(json.dumps({"hooks": hooks, "source_duration": src_dur}, indent=2))
        return 0

    # --- encode --------------------------------------------------------------
    manifest_entries: list[dict] = []
    used_names: set[str] = set()
    for i, h in enumerate(hooks, start=1):
        # Disambiguate name collisions (same hook shot twice → name + _take2 etc).
        base = h["name"] or f"hook_{i:02d}"
        name = base
        n = 2
        while name in used_names:
            name = f"{base}_take{n}"
            n += 1
        used_names.add(name)

        out_path = out_dir / f"{i:02d}_{name}.mp4"
        encode_segment(
            src, h["start"], h["end"], out_path,
            args.encoder, args.fade_ms,
        )
        manifest_entries.append({
            "index":     i,
            "name":      name,
            "file":      str(out_path),
            "start":     h["start"],
            "end":       h["end"],
            "duration":  h["duration"],
            "preview":   h.get("preview"),
            "best":      bool(h.get("best", False)),
            "take_of":   h.get("take_of"),  # caller-supplied: which group this re-take belongs to
        })

    manifest = {
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "source":         str(src),
        "source_duration": round(src_dur, 3),
        "edit_dir":       str(edit_dir),
        "out_dir":        str(out_dir),
        "mode":           "auto" if args.auto else "edl",
        "params": {
            "silence_threshold": args.silence_threshold,
            "min_duration":      args.min_duration,
            "head_pad":          args.head_pad,
            "tail_pad":          args.tail_pad,
            "fade_ms":           args.fade_ms,
            "encoder":           args.encoder,
        },
        "hooks":          manifest_entries,
        "hook_count":     len(manifest_entries),
        "total_hook_s":   round(sum(e["duration"] for e in manifest_entries), 3),
    }
    manifest_path = out_dir / "hooks_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if args.emit_json:
        print(json.dumps(manifest, indent=2))
    else:
        print(f"OK — {len(manifest_entries)} hooks → {out_dir}")
        for e in manifest_entries:
            tag = " ★best" if e["best"] else ""
            print(f"  {e['index']:02d}  {e['duration']:5.1f}s  {e['file']}{tag}")
        print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
