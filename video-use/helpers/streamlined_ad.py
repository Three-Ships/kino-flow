"""Build a "streamlined ad" — the studio's lightweight direct-response format.

Two formats, both 10–20 s, 9:16 by default, music bed under everything:

  bullets  — background image/video · hook text top-center · selling-point
             chips that pop in one by one · CTA at the bottom.
             (Screenshot reference: Endurance "protection plan includes" ad.)

  text-vo  — big boxed on-screen hook read out by an ElevenLabs VO, then a
             CTA card read out the same way.
             (Screenshot reference: "See What A Walk-in Shower Could Cost
             You in 2026".)

Fully deterministic — no LLM in the loop. All text timing/styling is one
generated ASS file burned over the background; the sidecar + kept assets
make the output editable in the studio timeline.

Usage:
    python helpers/streamlined_ad.py --format bullets \\
        --background path/to/bg.mp4 --hook "An Endurance protection plan includes:" \\
        --bullet "The freedom to pick the mechanic you trust" \\
        --bullet "Zero bills for covered vehicle repairs" \\
        --bullet "24/7 roadside assistance" \\
        --cta "Tap below for your free quote" \\
        --music path/to/music_folder_or_file --duration 15 \\
        --output videos/edit/streamlined_x/final.mp4 [--logo logo.png] [--json]

    python helpers/streamlined_ad.py --format text-vo \\
        --background bg.mp4 --hook "See what a walk-in shower could cost you in 2026" \\
        --cta "Tap the link below to get your free quote" \\
        --voice-id <11labs id> --music path/to/Music --output out.mp4
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def die(msg: str) -> None:
    sys.exit(f"streamlined_ad: {msg}")


def run_ffmpeg(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        die("ffmpeg failed: " + (r.stderr or b"").decode("utf-8", errors="replace")[-600:])


def probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def mean_volume_db(path: Path) -> float | None:
    """volumedetect mean_volume — used to level the music bed to a target."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
             "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", r.stderr)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def pick_media(path: Path, exts: set[str], label: str) -> Path:
    """A file passes through; a folder yields a random file of the right kind."""
    if path.is_file():
        return path
    if path.is_dir():
        pool = [f for f in path.rglob("*")
                if f.is_file() and f.suffix.lower() in exts and not f.name.startswith("._")]
        if not pool:
            die(f"no {label} files found under {path}")
        return random.choice(pool)
    die(f"{label} path not found: {path}")
    raise SystemExit  # unreachable


def ass_ts(t: float) -> str:
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def esc(text: str) -> str:
    return text.replace("\n", "\\N")


def parse_hex(color: str) -> tuple[int, int, int]:
    c = color.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        die(f"bad hex color: {color!r} (want #RRGGBB)")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def ass_color(color: str, alpha: int = 0) -> str:
    """#RRGGBB → ASS &HAABBGGRR."""
    r, g, b = parse_hex(color)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def text_on(color: str) -> str:
    """Black or white ASS text color, whichever reads better on `color`."""
    r, g, b = parse_hex(color)
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    return "&H00111111" if lum > 0.55 else "&H00FFFFFF"


def wrap_lines(text: str, max_chars: int) -> str:
    """Greedy wrap to \\N lines so big hooks stack like the reference ads."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\\N".join(lines)


# ── ASS document ────────────────────────────────────────────────────────

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Arial,{hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H66000000,-1,0,0,0,100,100,0,0,1,3,2,8,60,60,{hook_margin_v},1
Style: BigHook,Arial,{bighook_size},{bighook_text},{bighook_text},{bighook_box},{bighook_box},-1,0,0,0,100,100,0,0,3,{chip_pad},0,5,90,90,0,1
Style: Bullet,Arial,{bullet_size},{bullet_text},{bullet_text},{bullet_box},{bullet_box},-1,0,0,0,100,100,0,0,3,{chip_pad},0,7,{bullet_margin_l},60,0,1
Style: CTA,Arial,{cta_size},{cta_text},{cta_text},{cta_box},&HB0000000,-1,0,0,0,100,100,0,0,3,{chip_pad},0,2,70,70,{cta_margin_v},1
Style: Disclaimer,Arial,18,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,1,2,{disc_margin_h},{disc_margin_h},{disc_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(fmt: str, w: int, h: int, total: float, hook: str, bullets: list[str],
              cta: str, out: Path, hook_end: float | None, cta_start: float,
              brand_primary: str | None = None,
              brand_accent: str | None = None,
              disclaimer: str = "") -> None:
    # brand colors: primary tints the CTA box + big-hook box, accent tints the
    # bullet chips; text flips black/white by luminance. No flags = house look.
    header = ASS_HEADER.format(
        w=w, h=h,
        hook_size=int(h * 0.034),        # ~64 @1920
        bighook_size=int(h * 0.045),     # ~86 @1920
        bullet_size=int(h * 0.024),      # ~46 @1920
        cta_size=int(h * 0.028),
        chip_pad=int(h * 0.010),
        hook_margin_v=int(h * 0.30),
        bullet_margin_l=int(w * 0.045),
        cta_margin_v=int(h * 0.15),   # was 0.08 — Sean wants the CTA higher
        disc_margin_h=int(w * 0.06),  # side margins keep the disclaimer in-frame
        disc_margin_v=int(h * 0.02),  # bottom margin (~38px @1920)
        bighook_box=ass_color(brand_primary) if brand_primary else "&H00FFFFFF",
        bighook_text=text_on(brand_primary) if brand_primary else "&H00111111",
        bullet_box=ass_color(brand_accent) if brand_accent else "&H00FFFFFF",
        bullet_text=text_on(brand_accent) if brand_accent else "&H00111111",
        cta_box=ass_color(brand_primary) if brand_primary else "&H00000000",
        cta_text=text_on(brand_primary) if brand_primary else "&H00FFFFFF",
    )
    ev: list[str] = []

    def dlg(style: str, start: float, end: float, text: str,
            override: str = "") -> None:
        ev.append(f"Dialogue: 0,{ass_ts(start)},{ass_ts(end)},{style},,0,0,0,,"
                  f"{override}{text}")

    if fmt == "bullets":
        # hook: fades in early, stays to the end
        dlg("Hook", 0.3, total, "{\\fad(250,0)}" + wrap_lines(hook, 26))
        # bullets: pop in sequentially down the left side, stay to the end
        first, spacing = 1.4, 0.9
        y0 = int(h * 0.46)
        step = int(h * 0.048)
        for i, b in enumerate(bullets):
            t0 = min(first + i * spacing, max(0.5, total - 2.5))
            dlg("Bullet", t0, total, esc(b),
                "{\\fad(160,0)\\pos(%d,%d)}" % (int(w * 0.045), y0 + i * step))
        if cta:
            dlg("CTA", cta_start, total, "{\\fad(200,0)}" + wrap_lines(cta, 30))
    else:  # text-vo
        he = hook_end if hook_end is not None else max(0.5, cta_start - 0.3)
        dlg("BigHook", 0.15, he, "{\\fad(200,120)}" + wrap_lines(hook, 16))
        if cta:
            dlg("BigHook", cta_start, total, "{\\fad(200,0)}" + wrap_lines(cta, 16))

    # Disclaimer — 18pt white, bottom-center, wrapped to stay inside the frame,
    # on screen for the whole ad.
    if disclaimer.strip():
        dlg("Disclaimer", 0.3, total, "{\\fad(250,0)}" + wrap_lines(disclaimer.strip(), 70))

    out.write_text(header + "\n".join(ev) + "\n", encoding="utf-8")


def ff_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", choices=["bullets", "text-vo"], required=True)
    ap.add_argument("--background", type=Path, required=True,
                    help="image/video file, or folder to pick randomly from")
    ap.add_argument("--hook", required=True)
    ap.add_argument("--bullet", action="append", default=[], dest="bullets")
    ap.add_argument("--cta", default="")
    ap.add_argument("--music", type=Path, default=None,
                    help="music file or folder (random pick)")
    ap.add_argument("--voice-id", default=None, help="ElevenLabs voice (text-vo)")
    ap.add_argument("--duration", type=float, default=15.0,
                    help="target length in seconds (text-vo may extend to fit VO)")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--logo", type=Path, default=None,
                    help="optional PNG overlaid top-center (bullets format)")
    ap.add_argument("--brand-primary", default=None,
                    help="#RRGGBB — tints the CTA box and text-vo hook box")
    ap.add_argument("--brand-accent", default=None,
                    help="#RRGGBB — tints the bullet chips")
    ap.add_argument("--disclaimer", default="",
                    help="fine-print disclaimer — 18pt white, centered at the "
                         "bottom of the frame, on screen the whole ad")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=None,
                    help="random seed for background/music folder picks")
    ap.add_argument("--json", dest="emit_json", action="store_true")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
    if args.format == "bullets" and not args.bullets:
        die("bullets format needs at least one --bullet")
    if args.format == "text-vo" and not args.voice_id:
        die("text-vo format needs --voice-id")
    # validate colors up front — before any ElevenLabs spend
    for c in (args.brand_primary, args.brand_accent):
        if c:
            parse_hex(c)

    out_dir = args.output.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.output.stem

    background = pick_media(args.background, VIDEO_EXTS | IMAGE_EXTS, "background")
    bg_is_image = background.suffix.lower() in IMAGE_EXTS
    music = pick_media(args.music, AUDIO_EXTS, "music") if args.music else None

    # ── VO (text-vo): hook + CTA each synthesized; their lengths drive timing ──
    total = max(6.0, args.duration)
    hook_end: float | None = None
    cta_start = max(2.0, total - 3.5)
    vo_files: list[tuple[Path, float]] = []   # (file, start offset)
    if args.format == "text-vo":
        from tts_voice import load_api_key, synthesize
        api_key = load_api_key()
        hook_vo = out_dir / f"{stem}_hook_vo.mp3"
        synthesize(api_key, args.voice_id, args.hook, "eleven_multilingual_v2",
                   hook_vo, 0.5, 0.75, 0.0, True)
        hook_dur = probe_duration(hook_vo)
        vo_files.append((hook_vo, 0.2))
        cta_start = 0.2 + hook_dur + 0.6
        hook_end = cta_start - 0.25
        cta_dur = 0.0
        if args.cta:
            cta_vo = out_dir / f"{stem}_cta_vo.mp3"
            synthesize(api_key, args.voice_id, args.cta, "eleven_multilingual_v2",
                       cta_vo, 0.5, 0.75, 0.0, True)
            cta_dur = probe_duration(cta_vo)
            vo_files.append((cta_vo, cta_start))
        total = max(total, cta_start + cta_dur + 1.2)

    # ── text overlay ASS ──
    ass_path = out_dir / f"{stem}.ass"
    build_ass(args.format, args.width, args.height, total, args.hook,
              args.bullets, args.cta, ass_path, hook_end, cta_start,
              args.brand_primary, args.brand_accent, args.disclaimer)

    # ── assemble the ffmpeg graph ──
    cmd: list[str] = ["ffmpeg", "-y", "-hide_banner"]
    if bg_is_image:
        cmd += ["-loop", "1", "-t", f"{total:.3f}", "-i", str(background)]
    else:
        # loop short backgrounds so any clip covers the full ad
        cmd += ["-stream_loop", "-1", "-t", f"{total:.3f}", "-i", str(background)]

    audio_inputs = 0
    filter_a: list[str] = []
    amix_srcs: list[str] = []
    if music is not None:
        cmd += ["-i", str(music)]
        audio_inputs += 1
        mean = mean_volume_db(music)
        # house rule: music ≤ -20 dBFS under VO; a bit hotter when solo
        target = -22.0 if vo_files else -16.0
        gain = (target - mean) if mean is not None else -14.0
        filter_a.append(
            f"[1:a]atrim=0:{total:.3f},volume={gain:.1f}dB,"
            f"afade=t=out:st={max(0.0, total - 1.2):.2f}:d=1.2[mus]")
        amix_srcs.append("[mus]")
    for i, (vf_path, offset) in enumerate(vo_files):
        cmd += ["-i", str(vf_path)]
        audio_inputs += 1
        idx = 1 + (1 if music is not None else 0) + i
        filter_a.append(f"[{idx}:a]adelay={int(offset * 1000)}|{int(offset * 1000)}[vo{i}]")
        amix_srcs.append(f"[vo{i}]")

    vf = (f"scale={args.width}:{args.height}:force_original_aspect_ratio=increase,"
          f"crop={args.width}:{args.height},fps={args.fps},setsar=1")
    if args.logo and args.logo.exists():
        cmd += ["-i", str(args.logo)]
        logo_idx = 1 + audio_inputs
        fc_video = (f"[0:v]{vf}[bg];"
                    f"[{logo_idx}:v]scale={int(args.width * 0.45)}:-1[lg];"
                    f"[bg][lg]overlay=(W-w)/2:{int(args.height * 0.045)}[vid];"
                    f"[vid]ass='{ff_path(ass_path)}'[outv]")
    else:
        fc_video = f"[0:v]{vf},ass='{ff_path(ass_path)}'[outv]"

    fc = fc_video
    if amix_srcs:
        fc += ";" + ";".join(filter_a)
        if len(amix_srcs) == 1:
            fc += f";{amix_srcs[0]}anull[outa]"
        else:
            fc += (";" + "".join(amix_srcs)
                   + f"amix=inputs={len(amix_srcs)}:duration=longest:normalize=0[outa]")

    cmd += ["-filter_complex", fc, "-map", "[outv]"]
    if amix_srcs:
        cmd += ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    # Pick a video encoder adaptively so ads render on ANY machine — NVENC on
    # NVIDIA, else libx264 CPU (Mac, AMD/Intel, or GPU-less). 8-bit 4:2:0 High@4.2
    # is pinned for universal playback (same as variant_factory).
    try:
        from render import select_encoder  # type: ignore
        _enc = select_encoder()
    except Exception:
        _enc = "x264"
    if _enc.startswith("nvenc"):
        _venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19",
                 "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2"]
    else:
        _venc = ["-c:v", "libx264", "-preset", "medium", "-crf", "19",
                 "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2"]
    cmd += ["-t", f"{total:.3f}"] + _venc + [str(args.output)]

    print(f"[render] {args.format} ad · {total:.1f}s · bg={background.name}"
          + (f" · music={music.name}" if music else ""), file=sys.stderr)
    run_ffmpeg(cmd)

    # ── timeline sidecar so the studio dock can show + re-edit this ad ──
    sidecar = {
        "run": out_dir.name,
        "format": f"streamlined-{args.format}",
        "vo_duration_s": round(total, 3),
        "hook": args.hook,
        "bullets": args.bullets,
        "cta": args.cta,
        "disclaimer": args.disclaimer or "",
        "broll_clips": [{"source": str(background), "in": 0.0,
                         "out": round(total, 3), "note": "background"}],
        "music": str(music) if music else None,
        "voice_id": args.voice_id if args.format == "text-vo" else None,
        "resolution": f"{args.width}x{args.height}",
        "fps": args.fps,
        "ass_file": str(ass_path),
        "brand": {"primary": args.brand_primary, "accent": args.brand_accent,
                  "logo": str(args.logo) if args.logo else None},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if vo_files:
        sidecar["vo_file"] = str(vo_files[0][0])
    (out_dir / (args.output.name + ".timeline.json")).write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8")

    result = {"output": str(args.output), "duration_s": round(total, 2),
              "background": str(background), "music": str(music) if music else None,
              "format": args.format}
    print(f"wrote: {args.output}", file=sys.stderr)
    if args.emit_json:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
