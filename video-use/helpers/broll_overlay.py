"""Composite b-roll cutaways over a master video.

Takes a master video (talking head or VO-driven) and an EDL of b-roll segments
to splice in. For each segment, the b-roll clip's video replaces the master's
video for that time range; the master's audio plays continuously underneath.
B-roll audio is always muted (the operator's hard rule).

EDL format (JSON file or stdin):
    [
      {"start": 5.2, "end": 8.7, "source": "broll/installing/clip_001.mp4", "source_in": 1.5},
      {"start": 15.0, "end": 18.4, "source": "broll/after/clip_007.mp4", "source_in": 0.0}
    ]

- `start` / `end` are seconds into the master.
- `source` is an absolute path or a path relative to --broll-root.
- `source_in` is the in-point in the b-roll source (so the same clip can be
  used multiple times at different ranges without repetition — caller is
  responsible for tracking what ranges have been used).

Implementation: builds a filter_complex that concatenates trimmed master and
trimmed-and-scaled b-roll segments along a single video timeline, then maps the
master's audio (untouched, full duration) onto the result. B-roll is scaled to
match the master's resolution preserving aspect via fit-and-pad.

Usage:
    python helpers/broll_overlay.py master.mp4 --edl edl.json --output composite.mp4
    python helpers/broll_overlay.py master.mp4 --edl - --output out.mp4 < edl.json
    python helpers/broll_overlay.py master.mp4 --edl edl.json --output out.mp4 --broll-root /path/to/broll --json
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def ffprobe_video(path: Path) -> dict:
    """Return basic video stream info: width, height, duration, framerate."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    vstream = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if vstream is None:
        raise RuntimeError(f"no video stream in {path}")
    return {
        "width":    int(vstream["width"]),
        "height":   int(vstream["height"]),
        "duration": float(data["format"].get("duration", 0.0)),
        # r_frame_rate is a rational ("30/1", "30000/1001"). Caller may pass
        # it through to fps= and -r to keep the timeline stable across
        # trim+concat. Defaulting to "30000/1001" was the original quiet
        # behavior; surfacing it explicitly here so build_filter_complex
        # can force CFR on every segment.
        "r_frame_rate": str(vstream.get("r_frame_rate", "30000/1001")),
    }


def load_edl(path: str) -> list[dict]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    edl = json.loads(raw)
    if not isinstance(edl, list):
        raise ValueError("EDL must be a JSON array of segment objects")
    for i, seg in enumerate(edl):
        for key in ("start", "end", "source"):
            if key not in seg:
                raise ValueError(f"EDL segment {i} missing required key: {key}")
        seg.setdefault("source_in", 0.0)
        if seg["end"] <= seg["start"]:
            raise ValueError(f"EDL segment {i}: end ({seg['end']}) must exceed start ({seg['start']})")
    edl.sort(key=lambda s: s["start"])
    # Reject overlapping segments — caller's responsibility to keep clean.
    for a, b in zip(edl, edl[1:]):
        if b["start"] < a["end"]:
            raise ValueError(
                f"overlapping segments: {a['start']}-{a['end']} vs {b['start']}-{b['end']}"
            )
    return edl


def resolve_source(seg: dict, broll_root: Path | None) -> Path:
    src = Path(seg["source"])
    if src.is_absolute():
        return src
    if broll_root is not None:
        return (broll_root / src).resolve()
    return src.resolve()


def build_filter_complex(
    master_dur: float,
    master_w: int,
    master_h: int,
    edl: list[dict],
    master_fps: str = "30000/1001",
) -> tuple[str, list[str]]:
    """Build filter_complex string and the segment-label list to concat.

    Master is input [0]; b-roll sources are inputs [1..N] in EDL order.

    CRITICAL: every trimmed segment is normalized to the master's framerate
    and a consistent timebase. Without this, ffmpeg's concat filter drops
    1–4 frames at each trim boundary because the source streams have
    different timebases / PTS jitter at non-keyframe trim points. Across 7
    b-roll cutaways that compounds to ~1 second of total video drift while
    the audio (encoded separately, untouched) stays at full length —
    producing a visible audio-after-video desync at the end of the clip.
    Fix: `fps={master_fps},settb=AVTB` after every trim locks each segment
    to the master's timeline.
    """
    parts: list[str] = []
    labels: list[str] = []  # order matters — concat reads them in sequence
    cursor = 0.0
    seg_idx = 0

    # The framerate-and-timebase normalizer applied after every trim.
    norm = f"fps={master_fps},settb=AVTB"

    for i, seg in enumerate(edl):
        # Master segment before this cutaway
        if seg["start"] > cursor + 1e-3:
            lbl = f"m{seg_idx}"
            parts.append(
                f"[0:v]trim=start={cursor:.6f}:end={seg['start']:.6f},"
                f"setpts=PTS-STARTPTS,{norm}[{lbl}]"
            )
            labels.append(lbl)
            seg_idx += 1
        # The cutaway itself — input index is i+1 (master is 0)
        lbl = f"b{i}"
        dur = seg["end"] - seg["start"]
        src_in = seg["source_in"]
        # Scale b-roll to master resolution preserving aspect (fit-and-pad black).
        # fps= forces this segment to the master's framerate so concat doesn't
        # drop boundary frames trying to reconcile different rates.
        parts.append(
            f"[{i+1}:v]trim=start={src_in:.6f}:end={src_in + dur:.6f},"
            f"setpts=PTS-STARTPTS,"
            f"scale=w={master_w}:h={master_h}:force_original_aspect_ratio=decrease,"
            f"pad={master_w}:{master_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,{norm}[{lbl}]"
        )
        labels.append(lbl)
        cursor = seg["end"]

    # Trailing master segment after the final cutaway
    if cursor < master_dur - 1e-3:
        lbl = f"m{seg_idx}"
        parts.append(
            f"[0:v]trim=start={cursor:.6f}:end={master_dur:.6f},"
            f"setpts=PTS-STARTPTS,{norm}[{lbl}]"
        )
        labels.append(lbl)

    concat_inputs = "".join(f"[{lbl}]" for lbl in labels)
    parts.append(f"{concat_inputs}concat=n={len(labels)}:v=1:a=0[outv]")
    return ";".join(parts), labels


def main() -> int:
    ap = argparse.ArgumentParser(description="Composite b-roll cutaways over a master video.")
    ap.add_argument("master", help="master video (talking head or VO-driven base)")
    ap.add_argument("--edl", required=True, help="EDL JSON file path, or '-' for stdin")
    ap.add_argument("--output", required=True, help="output mp4 path")
    ap.add_argument("--broll-root", help="root folder b-roll source paths are relative to")
    ap.add_argument("--audio-source", help="optional separate audio file (e.g. ElevenLabs VO) — overrides master audio")
    ap.add_argument("--encoder", default="h264_nvenc", help="ffmpeg video encoder (default: h264_nvenc)")
    ap.add_argument("--crf", default="19", help="quality for x264; ignored on nvenc")
    ap.add_argument("--json", dest="emit_json", action="store_true", help="print structured result on stdout")
    ap.add_argument("--dry-run", action="store_true", help="print the ffmpeg command without running")
    args = ap.parse_args()

    master = Path(args.master).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    broll_root = Path(args.broll_root).resolve() if args.broll_root else None

    edl = load_edl(args.edl)

    minfo = ffprobe_video(master)
    master_dur = minfo["duration"]
    master_w, master_h = minfo["width"], minfo["height"]
    master_fps = minfo["r_frame_rate"]

    # ── DURATION SANITY PASS ────────────────────────────────────────────
    # The trim filter in ffmpeg silently truncates when the source is shorter
    # than what's requested — and that lost video time CAUSES AUDIO DESYNC
    # downstream because the master audio stays at full length while the
    # video timeline shrinks. We probe every b-roll source upfront and
    # clamp `seg["end"]` so the EDL's video timeline matches what we can
    # actually deliver. The cursor logic in build_filter_complex picks up
    # the clamped ends and the master fills the gap, so the master+b-roll
    # timeline stays exactly the master's length. Warnings are loud — the
    # planner needs to know it picked too-short sources.
    truncations: list[dict] = []
    for seg in edl:
        src = resolve_source(seg, Path(args.broll_root).resolve() if args.broll_root else None)
        if not src.exists():
            continue  # let the resolve-and-input step below produce the canonical error
        try:
            src_dur = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(src)],
                check=True, capture_output=True, text=True,
            ).stdout.strip() or 0.0)
        except Exception:
            continue
        requested = seg["source_in"] + (seg["end"] - seg["start"])
        if src_dur > 0 and requested > src_dur + 1e-3:
            available = max(0.0, src_dur - seg["source_in"])
            if available <= 0.05:
                raise SystemExit(
                    f"b-roll source too short to use: {src.name} is {src_dur:.2f}s "
                    f"but EDL asks to start at {seg['source_in']:.2f}s "
                    f"(segment {seg['start']:.2f}-{seg['end']:.2f}). Pick a different clip."
                )
            new_end = seg["start"] + available
            truncations.append({
                "source":           seg["source"],
                "edl_start":        seg["start"],
                "edl_end_original": seg["end"],
                "edl_end_clamped":  round(new_end, 3),
                "source_duration":  round(src_dur, 3),
                "source_in":        seg["source_in"],
                "lost_seconds":     round(seg["end"] - new_end, 3),
            })
            original_end = truncations[-1]["edl_end_original"]
            seg["end"] = new_end
            sys.stderr.write(
                f"WARNING: b-roll source {src.name} is only {src_dur:.2f}s — "
                f"truncating segment {seg['start']:.2f}-{original_end:.2f} "
                f"to end at {new_end:.2f}s. Master video fills the gap. "
                f"Re-plan with a longer source if you want the full cutaway.\n"
            )
    if truncations:
        sys.stderr.write(
            f"\n=== {len(truncations)} b-roll segment(s) truncated to prevent audio desync. ===\n\n"
        )

    if not edl:
        raise SystemExit("EDL is empty — nothing to overlay. Aborting.")
    last_end = max(seg["end"] for seg in edl)
    if last_end > master_dur + 0.05:
        raise SystemExit(f"EDL segment ends at {last_end:.3f}s but master is only {master_dur:.3f}s")

    # Build the input list and resolve all b-roll source paths.
    inputs: list[str] = ["-i", str(master)]
    resolved_sources: list[Path] = []
    for seg in edl:
        src = resolve_source(seg, broll_root)
        if not src.exists():
            raise SystemExit(f"b-roll source not found: {src}")
        resolved_sources.append(src)
        inputs.extend(["-i", str(src)])

    fc, labels = build_filter_complex(master_dur, master_w, master_h, edl, master_fps=master_fps)

    # Audio: master audio (or external --audio-source if provided), full duration.
    if args.audio_source:
        audio_path = Path(args.audio_source).resolve()
        if not audio_path.exists():
            raise SystemExit(f"audio source not found: {audio_path}")
        audio_input_idx = 1 + len(edl)  # after master + all b-roll inputs
        inputs.extend(["-i", str(audio_path)])
        audio_map = ["-map", f"{audio_input_idx}:a:0"]
    else:
        audio_map = ["-map", "0:a:0?"]  # ? = optional (master might be silent)

    cmd: list[str] = [
        "ffmpeg", "-hide_banner", "-y",
        *inputs,
        "-filter_complex", fc,
        "-map", "[outv]",
        *audio_map,
        # Force constant frame rate output locked to the master's rate. Belt
        # AND suspenders with the in-graph fps= filters — vsync cfr keeps the
        # muxer from dropping/duplicating to "fix" any residual jitter.
        "-vsync", "cfr",
        "-r", master_fps,
        "-c:v", args.encoder,
    ]
    if args.encoder.startswith("h264_nvenc"):
        cmd.extend(["-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"])
    else:
        cmd.extend(["-preset", "medium", "-crf", args.crf])
    cmd.extend(["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)])

    if args.dry_run:
        print(" ".join(shlex.quote(c) for c in cmd))
        return 0

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"ffmpeg failed (exit {proc.returncode})")

    result = {
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "master":        str(master),
        "output":        str(output),
        "broll_root":    str(broll_root) if broll_root else None,
        "audio_source":  str(audio_path) if args.audio_source else None,
        "master_duration_s": round(master_dur, 3),
        "master_resolution": f"{master_w}x{master_h}",
        "edl_segments":  len(edl),
        "broll_total_s": round(sum(seg["end"] - seg["start"] for seg in edl), 3),
        "broll_sources_used": sorted({str(p) for p in resolved_sources}),
        "encoder":       args.encoder,
        "truncations":   truncations,
    }
    if args.emit_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"OK — {len(edl)} cutaways totaling {result['broll_total_s']}s composited to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
