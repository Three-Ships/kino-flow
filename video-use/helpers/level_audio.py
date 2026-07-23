"""Peak-normalize the audio of a media file to a target dBFS window.

This is **peak** leveling (true sample peak, dBFS), not LUFS loudness. The
target is expressed as a [min, max] window — the helper applies a linear
gain so the output's peak lands at the **midpoint** of the window. After
encoding, it re-probes the output and warns if the result fell outside
the requested window.

Why peak (not LUFS): the operator's mental model on this rig is
peak-based. The DJI lav consistently records dialogue around -12 dBFS,
the spec is to deliver dialogue between -3 and -6 dBFS (peak), and music
beds finalized between -20 and -24 dBFS. Those numbers are dBFS peak by
shop convention. LUFS-based normalization would silently re-rank "loud"
across content types and is the wrong tool here.

The helper does **only** the audio side: video stream is `-c:v copy`,
preserving every existing video encode setting downstream of sync.

Usage:
    python helpers/level_audio.py <video> --dialogue
    python helpers/level_audio.py <video> --music
    python helpers/level_audio.py <video> --peak-min -6 --peak-max -3
    python helpers/level_audio.py <video> --output out.mp4 --json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Built-in target windows. Add new presets here as the operator's spec evolves.
PRESETS: dict[str, tuple[float, float]] = {
    "dialogue": (-6.0, -3.0),     # talking head, lav-mic'd VO, interview
    "music":   (-24.0, -20.0),    # bed music under dialogue
}

# Sanity guards.
GAIN_WARN_DB = 20.0   # gains > this amplify noise floor noticeably
SILENCE_FLOOR_DB = -85.0  # input peak below this is treated as silent → skip


def probe_peak_dbfs(media: Path) -> float:
    """Return the source's max sample peak in dBFS (negative).

    Implementation uses `ffmpeg -af volumedetect`, which emits both
    max_volume and mean_volume to stderr. We only need max_volume.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(media),
         "-af", "volumedetect", "-vn", "-sn", "-dn", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr)
    if not m:
        # ffmpeg returns nonzero when an input is malformed, but volumedetect
        # itself doesn't fail — if max_volume is missing the input is silent
        # or the audio stream is unreadable.
        if "Audio:" not in proc.stderr:
            raise RuntimeError(f"no audio stream found in {media}")
        return SILENCE_FLOOR_DB
    return float(m.group(1))


def resolve_window(args: argparse.Namespace) -> tuple[float, float, str]:
    """Pick the target [min, max] window from preset or explicit flags.

    Returns (peak_min, peak_max, label) where label names the preset for
    logging. Explicit --peak-min/--peak-max win over presets.
    """
    if args.peak_min is not None and args.peak_max is not None:
        lo, hi = sorted([float(args.peak_min), float(args.peak_max)])
        return lo, hi, f"custom({lo:+.1f}..{hi:+.1f})"
    label = "dialogue" if args.dialogue else ("music" if args.music else "dialogue")
    lo, hi = PRESETS[label]
    return lo, hi, label


def apply_level(
    input_path: Path,
    output_path: Path,
    peak_min: float,
    peak_max: float,
    audio_bitrate: str = "192k",
    sample_rate: int = 48000,
) -> dict:
    """Probe, encode with linear gain, verify, return a metadata dict."""
    src_peak = probe_peak_dbfs(input_path)
    target = (peak_min + peak_max) / 2.0
    gain_db = target - src_peak

    print(f"  source peak     : {src_peak:+.2f} dBFS", file=sys.stderr)
    print(f"  target window   : {peak_min:+.2f} to {peak_max:+.2f} dBFS  "
          f"(midpoint {target:+.2f})", file=sys.stderr)
    print(f"  gain to apply   : {gain_db:+.2f} dB", file=sys.stderr)

    if src_peak <= SILENCE_FLOOR_DB:
        sys.exit(f"input peak {src_peak:.1f} dBFS is at or below the silence "
                 f"floor — refusing to amplify silence")
    if gain_db > GAIN_WARN_DB:
        print(f"  WARNING: applying {gain_db:+.1f} dB will significantly amplify "
              f"the noise floor of this source", file=sys.stderr)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", f"volume={gain_db:.3f}dB",
        "-c:a", "aac", "-b:a", audio_bitrate, "-ar", str(sample_rate),
        "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    out_peak = probe_peak_dbfs(output_path)
    in_window = peak_min <= out_peak <= peak_max
    print(f"  output peak     : {out_peak:+.2f} dBFS  "
          f"{'(in target window)' if in_window else 'OUTSIDE WINDOW'}", file=sys.stderr)
    if not in_window:
        # This is not fatal — log it and let the caller decide. Linear gain on
        # a clean source should land within ~0.1 dB of target; >0.5 dB drift
        # usually means the source had inter-sample peaks the encoder smoothed.
        print(f"  WARNING: output peak {out_peak:+.2f} dBFS is outside the "
              f"requested [{peak_min:+.2f}, {peak_max:+.2f}] window", file=sys.stderr)

    meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input": str(input_path),
        "output": str(output_path),
        "source_peak_dbfs": round(src_peak, 2),
        "target_peak_min_dbfs": peak_min,
        "target_peak_max_dbfs": peak_max,
        "target_midpoint_dbfs": round(target, 2),
        "gain_applied_db": round(gain_db, 2),
        "output_peak_dbfs": round(out_peak, 2),
        "in_target_window": in_window,
    }
    _append_log(meta)
    return meta


def _append_log(entry: dict) -> None:
    """Append one JSONL record to videos/edit/level_log.jsonl."""
    log_path = Path(__file__).resolve().parent.parent.parent / "videos" / "edit" / "level_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="source media file (any container ffmpeg can read)")
    ap.add_argument("--output", type=Path, default=None,
                    help="output path (default: <input_stem>_leveled.mp4 next to source)")

    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--dialogue", action="store_true",
                     help=f"target {PRESETS['dialogue'][0]:+g} to {PRESETS['dialogue'][1]:+g} dBFS (default if no preset/flags)")
    grp.add_argument("--music", action="store_true",
                     help=f"target {PRESETS['music'][0]:+g} to {PRESETS['music'][1]:+g} dBFS")

    ap.add_argument("--peak-min", type=float, default=None,
                    help="explicit lower bound, dBFS (overrides --dialogue/--music)")
    ap.add_argument("--peak-max", type=float, default=None,
                    help="explicit upper bound, dBFS (overrides --dialogue/--music)")
    ap.add_argument("--bitrate", default="192k", help="AAC bitrate (default 192k)")
    ap.add_argument("--sample-rate", type=int, default=48000, help="output sample rate (default 48000)")
    ap.add_argument("--json", action="store_true", help="print result as JSON")
    args = ap.parse_args()

    if not args.input.exists():
        sys.exit(f"input not found: {args.input}")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")

    peak_min, peak_max, label = resolve_window(args)
    output = args.output or (args.input.parent / f"{args.input.stem}_leveled.mp4")

    print(f"leveling {args.input.name} ({label} window):", file=sys.stderr)
    meta = apply_level(args.input, output, peak_min, peak_max,
                       audio_bitrate=args.bitrate, sample_rate=args.sample_rate)
    meta["preset"] = label
    print(f"wrote: {output}", file=sys.stderr)
    if args.json:
        print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
