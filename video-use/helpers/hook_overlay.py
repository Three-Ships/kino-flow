"""Burn a static on-screen hook onto a video.

A hook is a short attention-grabbing line meant to stop the scroll in the
first few seconds. It lives in the "safe zone" — center or upper-middle
third of the frame — well clear of where TikTok/IG/Reels overlay their own
chrome (profile name, captions, CTA button). Default: upper-third, first 3s.

The text is rendered to a transparent PNG with PIL (so we get rounded
backgrounds, shadows, and arbitrary fonts without fighting ffmpeg drawtext
on Windows), then overlaid via ffmpeg with a precise enable= time gate.

Batch mode: pass --text multiple times with --output-prefix to produce
N variants of the same source with different hook copy. Style + position
stay constant across variants — A/B testing copy, not design.

Usage:
    # Single hook, upper-third, first 3s, defaults:
    python helpers/hook_overlay.py in.mp4 --output out.mp4 --text "This stops the scroll"

    # Stays for the whole video:
    python helpers/hook_overlay.py in.mp4 --output out.mp4 \
        --text "Brand recall hook" --duration full

    # Batch — 3 variants, same style, different copy:
    python helpers/hook_overlay.py in.mp4 --output-prefix variants \
        --text "Option A" --text "Option B" --text "Option C"

    # JSON output (machine-readable result):
    python helpers/hook_overlay.py in.mp4 --output out.mp4 --text "Hook" --json
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Font search order — same chain caption_render.py uses, with Voltchu first
# for brand consistency on the studio's shoots.
FONT_CANDIDATES = [
    str(Path(__file__).resolve().parent.parent.parent / "Voltchu.ttf"),
    r"C:\Windows\Fonts\Voltchu.ttf",
    r"C:\Users\seanh\AppData\Local\Microsoft\Windows\Fonts\Voltchu.ttf",
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\seguibl.ttf",       # Segoe UI Black
    r"C:\Windows\Fonts\seguibold.ttf",     # Segoe UI Semibold (fallback)
    r"C:\Windows\Fonts\arial.ttf",
]


def _resolve_font(family: str | None, size: int) -> ImageFont.FreeTypeFont:
    """Pick the first available TTF. If `family` is given, try direct lookup
    first under C:\\Windows\\Fonts before falling back to the candidate chain.
    """
    paths: list[str] = []
    if family:
        # Try a few common filename conventions for the requested family.
        fam = family.strip()
        for ext in (".ttf", ".otf"):
            for variant in (fam, fam.replace(" ", ""), fam.lower(), fam.title().replace(" ", "")):
                paths.append(rf"C:\Windows\Fonts\{variant}{ext}")
                paths.append(rf"C:\Users\seanh\AppData\Local\Microsoft\Windows\Fonts\{variant}{ext}")
    paths.extend(FONT_CANDIDATES)
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    # Absolute last resort — PIL's default bitmap font. Looks terrible at
    # large sizes but at least the pipeline doesn't crash.
    return ImageFont.load_default()


# ──────────────────────────────────────────────────────────────────────────
# PNG rendering
# ──────────────────────────────────────────────────────────────────────────

def _hex_to_rgba(hex_str: str, default_alpha: int = 255) -> tuple[int, int, int, int]:
    h = hex_str.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (r, g, b, default_alpha)
    if len(h) == 8:
        r, g, b, a = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
        return (r, g, b, a)
    raise ValueError(f"bad hex color: {hex_str!r}")


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Break `text` into lines that each fit within `max_width` pixels."""
    words = text.split()
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        trial = " ".join(cur + [w])
        bbox = font.getbbox(trial)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines


def render_hook_png(
    text: str,
    out_path: Path,
    *,
    video_width: int,
    video_height: int,
    font_family: str | None = None,
    font_size: int = 96,
    text_color: str = "#FFFFFF",
    bg_color: str = "#000000",
    bg_alpha: int = 160,        # 0–255
    padding_h: int = 36,
    padding_v: int = 22,
    line_spacing: int = 10,
    rounded: bool = True,
    radius: int = 22,
    shadow: bool = False,
) -> tuple[int, int]:
    """Render the hook text to a transparent PNG sized to fit the video width
    with a comfortable side margin. Returns (png_width, png_height).
    """
    # Cap text width to ~85% of video width so the hook breathes inside the
    # safe zone. Wrap on word boundaries; respect explicit newlines first.
    target_w = int(video_width * 0.85)
    font = _resolve_font(font_family, font_size)

    raw_lines: list[str] = []
    for chunk in re.split(r"\n+", text.strip()):
        raw_lines.extend(_wrap_text(chunk, font, target_w - 2 * padding_h))

    if not raw_lines:
        raw_lines = [""]

    # Measure
    line_metrics: list[tuple[str, int, int]] = []
    for ln in raw_lines:
        bbox = font.getbbox(ln)
        line_metrics.append((ln, bbox[2] - bbox[0], bbox[3] - bbox[1]))
    text_w = max(m[1] for m in line_metrics)
    text_h = sum(m[2] for m in line_metrics) + line_spacing * (len(line_metrics) - 1)

    box_w = text_w + 2 * padding_h
    box_h = text_h + 2 * padding_v
    # Leave room for shadow if requested.
    shadow_offset = 4 if shadow else 0
    canvas_w = box_w + shadow_offset
    canvas_h = box_h + shadow_offset

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    fg_rgba = _hex_to_rgba(text_color, 255)
    bg_rgba = (*_hex_to_rgba(bg_color, 255)[:3], int(max(0, min(255, bg_alpha))))

    # Optional drop shadow on the background card.
    if shadow:
        shadow_img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow_img)
        sb = (0, 0, 0, 120)
        rect = (shadow_offset, shadow_offset, box_w + shadow_offset, box_h + shadow_offset)
        if rounded:
            sd.rounded_rectangle(rect, radius=radius, fill=sb)
        else:
            sd.rectangle(rect, fill=sb)
        shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(radius=3))
        img = Image.alpha_composite(img, shadow_img)
        draw = ImageDraw.Draw(img)

    # Background card — only if non-transparent.
    if bg_rgba[3] > 0:
        rect = (0, 0, box_w, box_h)
        if rounded:
            draw.rounded_rectangle(rect, radius=radius, fill=bg_rgba)
        else:
            draw.rectangle(rect, fill=bg_rgba)

    # Center each line inside the box.
    y = padding_v
    for ln, w, h in line_metrics:
        x = padding_h + (text_w - w) // 2
        draw.text((x, y), ln, font=font, fill=fg_rgba)
        y += h + line_spacing

    img.save(out_path, "PNG")
    return canvas_w, canvas_h


# ──────────────────────────────────────────────────────────────────────────
# ffmpeg overlay
# ──────────────────────────────────────────────────────────────────────────

def _probe_video(path: Path) -> tuple[int, int, float]:
    """Return (width, height, duration_s)."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    return (
        int(stream.get("width", 1920)),
        int(stream.get("height", 1080)),
        float(fmt.get("duration", 0.0) or 0.0),
    )


def _encoder_args() -> list[str]:
    """Mirror render.py's encoder selection. NVENC when available, x264 fallback."""
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


def burn_hook(
    video_in: Path,
    video_out: Path,
    text: str,
    *,
    position: str = "upper-third",   # "center" or "upper-third"
    duration_s: float | None = 3.0,  # None / "full" → whole video
    font_family: str | None = None,
    font_size: int = 96,
    text_color: str = "#FFFFFF",
    bg_color: str = "#000000",
    bg_alpha: int = 160,
    rounded: bool = True,
    shadow: bool = False,
) -> dict:
    """Render `text` to a PNG and overlay it on `video_in`.

    `duration_s` None = show for the whole video; otherwise show from t=0 to
    t=duration_s. The intent is "first N seconds of the scroll-stop window";
    we don't currently support a different start time — file an issue if you
    need fade-in or a delayed appearance.
    """
    if not video_in.exists():
        raise FileNotFoundError(f"input video not found: {video_in}")
    if not text or not text.strip():
        raise ValueError("hook text is empty")

    w, h, vid_dur = _probe_video(video_in)
    video_out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        png_path = Path(td) / "hook.png"
        png_w, png_h = render_hook_png(
            text, png_path,
            video_width=w, video_height=h,
            font_family=font_family, font_size=font_size,
            text_color=text_color, bg_color=bg_color, bg_alpha=bg_alpha,
            rounded=rounded, shadow=shadow,
        )

        # Position: horizontally centered. Vertically:
        #   center      → midpoint of the safe zone (50% vertical)
        #   upper-third → 25% vertical (top of the upper-third band)
        x_expr = "(W-w)/2"
        if position == "center":
            y_expr = "(H-h)/2"
        else:
            y_expr = "H/4 - h/2"

        # `enable=` filter expression — present from 0 to duration_s, or all-the-time.
        if duration_s is None or duration_s <= 0:
            enable_expr = "1"
        else:
            enable_expr = f"between(t,0,{float(duration_s):.3f})"

        # Filter graph: overlay PNG on top of the video stream.
        filter_complex = (
            f"[0:v][1:v]overlay=x={x_expr}:y={y_expr}:enable='{enable_expr}'[v]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_in),
            "-i", str(png_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "0:a?",
            *_encoder_args(),
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(video_out),
        ]
        subprocess.run(cmd, check=True)

    return {
        "input": str(video_in),
        "output": str(video_out),
        "text": text,
        "position": position,
        "duration_s": (None if duration_s is None or duration_s <= 0 else float(duration_s)),
        "video_width": w,
        "video_height": h,
        "video_duration_s": round(vid_dur, 3),
        "png_size": [png_w, png_h],
        "style": {
            "font_family": font_family,
            "font_size": font_size,
            "text_color": text_color,
            "bg_color": bg_color,
            "bg_alpha": bg_alpha,
            "rounded": bool(rounded),
            "shadow": bool(shadow),
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────

def _parse_duration(arg: str | None) -> float | None:
    """Accept '3', '3.5', or 'full'/'whole' — return float seconds or None."""
    if arg is None:
        return 3.0
    s = str(arg).strip().lower()
    if s in ("full", "whole", "all", "0", ""):
        return None
    try:
        return float(s)
    except ValueError:
        raise SystemExit(f"--duration must be a number or 'full', got {arg!r}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("video", type=Path, help="input MP4")
    ap.add_argument("--output", type=Path, default=None,
                    help="output MP4 (single-variant mode). Mutually exclusive with --output-prefix.")
    ap.add_argument("--output-prefix", type=Path, default=None,
                    help="output prefix for batch mode. Writes <prefix>_1.mp4, <prefix>_2.mp4, …")
    ap.add_argument("--text", action="append", default=[],
                    help="hook text. Repeat for batch mode (3 calls = 3 outputs).")
    ap.add_argument("--position", choices=("center", "upper-third"), default="upper-third")
    ap.add_argument("--duration", default="3",
                    help="seconds the hook stays on screen, or 'full' for the whole video. Default 3.")
    ap.add_argument("--font-family", default=None,
                    help="font family name (looked up under C:\\Windows\\Fonts). Falls back to Voltchu/Impact/Arial.")
    ap.add_argument("--font-size", type=int, default=96)
    ap.add_argument("--text-color", default="#FFFFFF")
    ap.add_argument("--bg-color", default="#000000")
    ap.add_argument("--bg-alpha", type=int, default=160,
                    help="background opacity 0-255 (0=transparent, 255=opaque). Default 160 (~63%%).")
    ap.add_argument("--rounded", dest="rounded", action="store_true", default=True)
    ap.add_argument("--no-rounded", dest="rounded", action="store_false")
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON result after rendering")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    if not args.text:
        sys.exit("must provide --text at least once")
    if args.output and args.output_prefix:
        sys.exit("--output and --output-prefix are mutually exclusive")
    if not args.output and not args.output_prefix:
        sys.exit("provide --output (single) or --output-prefix (batch)")
    if args.output and len(args.text) > 1:
        sys.exit("multiple --text values require --output-prefix, not --output")

    duration = _parse_duration(args.duration)
    video_in = args.video.resolve()

    results = []
    if args.output:
        out = args.output.resolve()
        r = burn_hook(
            video_in, out, args.text[0],
            position=args.position, duration_s=duration,
            font_family=args.font_family, font_size=args.font_size,
            text_color=args.text_color, bg_color=args.bg_color,
            bg_alpha=args.bg_alpha, rounded=args.rounded, shadow=args.shadow,
        )
        results.append(r)
        print(f"wrote: {out}")
    else:
        prefix = args.output_prefix.resolve()
        for i, txt in enumerate(args.text, start=1):
            out = prefix.with_name(f"{prefix.name}_{i}.mp4")
            r = burn_hook(
                video_in, out, txt,
                position=args.position, duration_s=duration,
                font_family=args.font_family, font_size=args.font_size,
                text_color=args.text_color, bg_color=args.bg_color,
                bg_alpha=args.bg_alpha, rounded=args.rounded, shadow=args.shadow,
            )
            results.append(r)
            print(f"wrote: {out}")

    if args.json:
        print(json.dumps({"variants": results, "count": len(results)}, indent=2))


if __name__ == "__main__":
    main()
