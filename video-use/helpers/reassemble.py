"""Re-assemble a VO + b-roll concat video from a timeline sidecar.

The deterministic re-render path for sidecar-based runs (variant_factory
outputs and hand-assembled runs like assemble.py): the studio timeline
edits the sidecar's `broll_clips` (trim / delete / reorder), then this
helper re-executes the exact assembly — per-clip trim + normalize →
concat → mux VO → burn captions — with NO LLM and NO new generation.

Reuses variant_factory's step functions so the re-render is bit-for-bit
the same pipeline that built the original.

Sidecar fields used: broll_clips[{source,in,out,note}], resolution
("1080x1920"), fps, and optionally vo_file / ass_file. When those two are
absent, they're discovered in the run folder (vo/*.mp3|wav, *.vo.mp3;
newest *.ass).

Usage:
    python helpers/reassemble.py --sidecar <run>/final.mp4.timeline.json \\
        --output <run>/final_edit1.mp4 [--ass <override.ass>] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import variant_factory as vf  # noqa: E402


def heal_drive_letter(path: Path) -> Path:
    """Asset sources live on removable drives whose letter can change
    (D:→E: bit us once). Same logic as studio/server.py."""
    if path.exists():
        return path
    drive = os.path.splitdrive(str(path))[0]
    if not (len(drive) == 2 and drive[1] == ":"):
        return path
    rest = str(path)[len(drive):]
    for letter in "EDFGHIJKLMNOPQRSTUVWXYZCAB":
        cand = Path(f"{letter}:{rest}")
        if cand.exists():
            return cand
    return path


def _discover(run_dir: Path, sidecar: dict, video_stem: str) -> tuple[Path | None, Path | None]:
    """Resolve (vo_file, ass_file) from sidecar fields or run-folder layout."""
    vo = sidecar.get("vo_file")
    vo_path = heal_drive_letter(Path(vo)) if vo else None
    if not (vo_path and vo_path.exists()):
        candidates = list(run_dir.glob(f"{video_stem}.vo.*"))
        if (run_dir / "vo").exists():
            candidates += sorted((run_dir / "vo").glob("*"),
                                 key=lambda f: -f.stat().st_size)
        candidates = [c for c in candidates
                      if c.is_file() and c.suffix.lower() in (".mp3", ".wav", ".m4a")]
        vo_path = candidates[0] if candidates else None

    ass = sidecar.get("ass_file")
    ass_path = heal_drive_letter(Path(ass)) if ass else None
    if not (ass_path and ass_path.exists()):
        # NEVER pick generated edit artifacts as the caption track — a
        # titles_edit*.ass chosen here once silently replaced the real
        # captions with a duplicate title burn.
        asses = [a for a in sorted(run_dir.glob("*.ass"), key=lambda f: -f.stat().st_mtime)
                 if not a.name.startswith(("captions_edit", "titles_edit"))]
        ass_path = asses[0] if asses else None
    return vo_path, ass_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sidecar", type=Path, required=True,
                    help="timeline.json sidecar (edited or original)")
    ap.add_argument("--output", type=Path, required=True, help="output MP4")
    ap.add_argument("--ass", type=Path, default=None,
                    help="override .ass to burn (e.g. rebuilt from edited cues)")
    ap.add_argument("--titles-ass", type=Path, default=None,
                    help="additional .ass of user title overlays, burned on top")
    ap.add_argument("--no-captions", action="store_true",
                    help="skip the caption burn even if an .ass exists")
    ap.add_argument("--json", dest="emit_json", action="store_true")
    args = ap.parse_args()

    if not args.sidecar.exists():
        sys.exit(f"sidecar not found: {args.sidecar}")
    sidecar = json.loads(args.sidecar.read_text(encoding="utf-8"))
    run_dir = args.sidecar.resolve().parent
    video_stem = args.sidecar.name.split(".")[0]

    clips = sidecar.get("broll_clips") or []
    if not clips:
        sys.exit("sidecar has no broll_clips — nothing to assemble")

    plan: list[tuple[Path, float, float]] = []
    for c in clips:
        src = heal_drive_letter(Path(str(c["source"])))
        if not src.exists():
            sys.exit(f"clip source not found (after drive-letter heal): {c['source']}")
        s_in, s_out = float(c.get("in", 0)), float(c.get("out", 0))
        if s_out - s_in < 0.05:
            sys.exit(f"clip too short ({s_in}-{s_out}s): {src.name}")
        plan.append((src, s_in, s_out))

    try:
        width, height = (int(x) for x in str(sidecar.get("resolution", "1080x1920")).split("x"))
    except ValueError:
        width, height = 1080, 1920
    fps_raw = sidecar.get("fps", 30)
    fps = fps_raw if isinstance(fps_raw, str) and "/" in fps_raw else f"{int(fps_raw)}/1"

    vo_path, ass_path = _discover(run_dir, sidecar, video_stem)
    if args.ass:
        ass_path = args.ass
    if vo_path is None:
        sys.exit("no VO audio found (sidecar vo_file / <stem>.vo.* / vo/*.mp3) — "
                 "cannot re-mux. Re-render is not possible for this run.")

    total = sum(b - a for _, a, b in plan)
    vo_dur = float(sidecar.get("vo_duration_s") or 0)
    if vo_dur and total < vo_dur - 0.25:
        print(f"warning: clips total {total:.2f}s but VO is {vo_dur:.2f}s — "
              f"the last {vo_dur - total:.2f}s of VO will have no picture",
              file=sys.stderr)

    tmp_silent = run_dir / f"_reasm_silent_{args.output.stem}.mp4"
    tmp_comp = run_dir / f"_reasm_comp_{args.output.stem}.mp4"

    print(f"[1/3] concat {len(plan)} clips @ {width}x{height} {fps}", file=sys.stderr)
    vf.step_render_broll_concat(plan, tmp_silent, width=width, height=height, fps=fps)
    print(f"[2/3] mux VO: {vo_path.name}", file=sys.stderr)
    vf.step_mux_vo(tmp_silent, vo_path, tmp_comp)

    import shutil
    import subprocess

    burn = ass_path is not None and not args.no_captions
    caps_out = args.output if args.titles_ass is None \
        else run_dir / f"_reasm_caps_{args.output.stem}.mp4"
    if burn:
        print(f"[3/4] burn captions: {ass_path.name}", file=sys.stderr)
        vf.step_burn_captions(tmp_comp, ass_path, caps_out,
                              hook_text=None, frame_height=height,
                              frame_width=width, hook_font=None,
                              cta_text=None, cta_font=None, cta_mode="full",
                              video_duration=vo_dur or None, disclaimer_text=None)
    else:
        print("[3/4] no captions", file=sys.stderr)
        caps_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_comp), str(caps_out))

    if args.titles_ass is not None:
        print(f"[4/4] burn titles: {args.titles_ass.name}", file=sys.stderr)
        ass_arg = str(args.titles_ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-i", str(caps_out),
             "-vf", f"ass='{ass_arg}'",
             "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19",
             "-c:a", "copy", str(args.output)],
            capture_output=True,
        )
        if r.returncode != 0:
            sys.exit("titles burn failed: "
                     + (r.stderr or b"").decode("utf-8", errors="replace")[-500:])

    for t in (tmp_silent, tmp_comp,
              caps_out if caps_out != args.output else None):
        if t is None:
            continue
        try:
            t.unlink()
        except OSError:
            pass

    # sidecar for the NEW output so it's timeline-loadable itself
    new_sidecar = dict(sidecar)
    new_sidecar["reassembled_at"] = datetime.now(timezone.utc).isoformat()
    new_sidecar["reassembled_from"] = str(args.sidecar.name)
    new_sidecar["vo_file"] = str(vo_path)
    if ass_path:
        new_sidecar["ass_file"] = str(ass_path)
    (args.output.parent / (args.output.name + ".timeline.json")).write_text(
        json.dumps(new_sidecar, indent=2), encoding="utf-8")

    result = {"output": str(args.output), "clips": len(plan),
              "total_clip_seconds": round(total, 2), "captions_burned": burn}
    print(f"wrote: {args.output}", file=sys.stderr)
    if args.emit_json:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
