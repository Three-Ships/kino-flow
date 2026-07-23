"""Composite motion-graphic overlays onto a video timeline.

Reads a graphics EDL (one entry per overlay) and renders each entry as a
transparent PNG, then ffmpeg-overlays them onto the source video with
fade-in/out and a small upward animation.

EDL format (JSON file or stdin):

    [
      {"at": 4.20, "duration": 2.5, "kind": "big_stat",
       "text": "47% faster", "preset": "neon_pop"},
      {"at": 12.80, "duration": 3.0, "kind": "callout",
       "text": "Real-time sync", "anchor": "top-right",
       "preset": "crisp_callout"},
      {"at": 22.40, "duration": 3.5, "kind": "lower_third",
       "text": "Trusted by 5,000+ teams", "preset": "crisp_callout"}
    ]

Required keys: `at` (seconds into source), `duration` (seconds), `kind`
(see KINDS), `text` (the display text), `preset` (see PRESETS).

Optional: `anchor` (top-left | top-right | center | bottom-left | bottom-right)
— overrides the kind's default anchor.

Style presets and kinds are fixed dicts at the top of this file. They are
intentionally simple: text card with rounded background, 200 ms ease-out
fade-in, 200 ms ease-out fade-out, slight upward Y-offset on entry. No
HyperFrames dependency; just PIL renders + ffmpeg overlay.

Usage:
    python helpers/graphics_overlay.py master.mp4 --edl gfx.json --output out.mp4
    python helpers/graphics_overlay.py master.mp4 --edl - < gfx.json --output out.mp4 --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# --- Style presets (must match the studio Graphics tab) -----------------------

PRESETS = {
    "crisp_callout":    {"bg": (255, 255, 255, 240), "fg": (15, 23, 42),    "font_scale": 1.00, "radius": 14, "border": None},
    "neon_pop":         {"bg": (15, 23, 42, 235),    "fg": (34, 211, 238),  "font_scale": 1.05, "radius": 12, "border": (34, 211, 238, 200)},
    "pastel_soft":      {"bg": (254, 243, 199, 240), "fg": (124, 45, 18),   "font_scale": 1.00, "radius": 18, "border": None},
    "mono_stark":       {"bg": (0, 0, 0, 240),       "fg": (255, 255, 255), "font_scale": 1.10, "radius": 4,  "border": None},
    "corp_navy":        {"bg": (30, 64, 175, 240),   "fg": (248, 250, 252), "font_scale": 1.00, "radius": 8,  "border": None},
    "highlight_yellow": {"bg": (250, 204, 21, 245),  "fg": (26, 26, 26),    "font_scale": 1.05, "radius": 6,  "border": None},
}

# Kinds drive size, anchor, and font weight relative to frame width.
KINDS = {
    # text_w_pct = max card width as fraction of frame width
    # font_h_pct = font size as fraction of frame height
    "callout":     {"text_w_pct": 0.35, "font_h_pct": 0.030, "anchor": "top-right",     "weight": "bold"},
    "lower_third": {"text_w_pct": 0.85, "font_h_pct": 0.038, "anchor": "lower-third",   "weight": "bold"},
    "big_stat":    {"text_w_pct": 0.70, "font_h_pct": 0.090, "anchor": "center",        "weight": "extrabold"},
    "bullet_list": {"text_w_pct": 0.55, "font_h_pct": 0.028, "anchor": "center-right",  "weight": "regular"},
    "end_card":    {"text_w_pct": 0.80, "font_h_pct": 0.060, "anchor": "center",        "weight": "bold"},
}

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\impact.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

PAD_H_PCT = 0.018   # horizontal padding inside the card (frame-w fraction)
PAD_V_PCT = 0.012   # vertical padding inside the card
FADE_S = 0.20       # fade in / fade out duration
ENTRY_Y_PX = 16     # upward translate amount on entry


# --- Helpers ------------------------------------------------------------------

def resolve_font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def ffprobe_size(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = (int(x) for x in out.split(","))
    out2 = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return w, h, float(out2)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w_px: int) -> list[str]:
    """Greedy word-wrap so each line fits within max_w_px."""
    words = text.split()
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        cand = (" ".join(cur + [w])).strip()
        bbox = draw.textbbox((0, 0), cand, font=font)
        if bbox[2] - bbox[0] > max_w_px and cur:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines or [text]


def render_overlay_png(
    text: str,
    preset_id: str,
    kind_id: str,
    frame_w: int,
    frame_h: int,
    out_path: Path,
) -> tuple[int, int]:
    preset = PRESETS.get(preset_id, PRESETS["crisp_callout"])
    kind = KINDS.get(kind_id, KINDS["callout"])

    # Font sizing
    font_h = max(14, int(round(frame_h * kind["font_h_pct"] * preset["font_scale"])))
    font = resolve_font(font_h)

    # Determine wrapping width
    max_card_w = int(frame_w * kind["text_w_pct"])
    pad_h = int(frame_w * PAD_H_PCT)
    pad_v = int(frame_h * PAD_V_PCT)
    inner_max_w = max(40, max_card_w - 2 * pad_h)

    # Sketch text on a throwaway canvas to measure it
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = wrap_text(dummy, text, font, inner_max_w)
    line_widths = []
    line_height = 0
    for ln in lines:
        bbox = dummy.textbbox((0, 0), ln, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        line_widths.append(w)
        line_height = max(line_height, h)
    text_w = max(line_widths) if line_widths else 0
    text_h = (line_height + int(line_height * 0.25)) * len(lines) - int(line_height * 0.25)

    card_w = min(max_card_w, text_w + 2 * pad_h)
    card_h = text_h + 2 * pad_v

    img = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, card_w - 1, card_h - 1],
                           radius=preset["radius"], fill=preset["bg"])
    if preset["border"]:
        draw.rounded_rectangle([0, 0, card_w - 1, card_h - 1],
                               radius=preset["radius"], outline=preset["border"], width=2)

    # Center each line horizontally inside the card
    y = pad_v
    for i, ln in enumerate(lines):
        bbox = dummy.textbbox((0, 0), ln, font=font)
        lw = bbox[2] - bbox[0]
        x = (card_w - lw) // 2
        # PIL's text origin is bbox-relative; subtract bbox top to align baseline.
        draw.text((x, y - bbox[1]), ln, font=font, fill=preset["fg"])
        y += line_height + int(line_height * 0.25)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return card_w, card_h


def anchor_xy(anchor: str, frame_w: int, frame_h: int, card_w: int, card_h: int) -> tuple[int, int]:
    margin_x = int(frame_w * 0.04)
    margin_y = int(frame_h * 0.04)
    if anchor == "top-left":
        return (margin_x, margin_y)
    if anchor == "top-right":
        return (frame_w - card_w - margin_x, margin_y)
    if anchor == "center":
        return ((frame_w - card_w) // 2, (frame_h - card_h) // 2)
    if anchor == "center-right":
        return (frame_w - card_w - margin_x, (frame_h - card_h) // 2)
    if anchor == "lower-third":
        return ((frame_w - card_w) // 2, int(frame_h * 0.72) - card_h // 2)
    if anchor == "bottom-left":
        return (margin_x, frame_h - card_h - margin_y)
    if anchor == "bottom-right":
        return (frame_w - card_w - margin_x, frame_h - card_h - margin_y)
    return ((frame_w - card_w) // 2, int(frame_h * 0.72) - card_h // 2)


def slugify(s: str, n: int = 24) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower())[:n].strip("_") or "gfx"


# --- Main ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Composite motion-graphic overlays onto a video.")
    ap.add_argument("master", help="source video")
    ap.add_argument("--edl", required=True, help="graphics EDL JSON file path or '-' for stdin")
    ap.add_argument("--output", required=True, help="output mp4 path")
    ap.add_argument("--encoder", default="h264_nvenc", help="ffmpeg encoder (default h264_nvenc)")
    ap.add_argument("--png-dir", default=None,
                    help="directory for the rendered card PNGs (default: <output_parent>/_gfx_pngs)")
    ap.add_argument("--json", dest="emit_json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    master = Path(args.master).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not master.exists():
        raise SystemExit(f"master not found: {master}")

    raw = sys.stdin.read() if args.edl == "-" else Path(args.edl).read_text(encoding="utf-8")
    edl = json.loads(raw)
    if not isinstance(edl, list):
        raise SystemExit("EDL must be a JSON array")
    if not edl:
        raise SystemExit("EDL is empty — nothing to overlay")

    fw, fh, fdur = ffprobe_size(master)

    png_dir = Path(args.png_dir).resolve() if args.png_dir else (output.parent / "_gfx_pngs")
    png_dir.mkdir(parents=True, exist_ok=True)

    # Render PNGs
    rendered: list[dict] = []
    for i, e in enumerate(edl):
        text = (e.get("text") or "").strip()
        if not text:
            raise SystemExit(f"EDL entry {i}: empty text")
        kind = e.get("kind") or "callout"
        if kind not in KINDS:
            raise SystemExit(f"EDL entry {i}: unknown kind '{kind}'. Valid: {sorted(KINDS)}")
        preset = e.get("preset") or "crisp_callout"
        if preset not in PRESETS:
            raise SystemExit(f"EDL entry {i}: unknown preset '{preset}'. Valid: {sorted(PRESETS)}")
        at = float(e.get("at"))
        dur = float(e.get("duration"))
        if dur < 0.5 or dur > 30:
            raise SystemExit(f"EDL entry {i}: duration {dur} out of range [0.5, 30]")
        if at < 0 or at > fdur + 0.05:
            raise SystemExit(f"EDL entry {i}: at={at} outside source duration {fdur}")

        slug = slugify(text)
        png_path = png_dir / f"{i:02d}_{slug}.png"
        cw, ch = render_overlay_png(text, preset, kind, fw, fh, png_path)
        anchor = e.get("anchor") or KINDS[kind]["anchor"]
        x, y = anchor_xy(anchor, fw, fh, cw, ch)

        rendered.append({
            "index":    i,
            "text":     text,
            "kind":     kind,
            "preset":   preset,
            "at":       at,
            "duration": dur,
            "anchor":   anchor,
            "png":      str(png_path),
            "card_w":   cw,
            "card_h":   ch,
            "x":        x,
            "y":        y,
        })

    # Build ffmpeg filter_complex with per-overlay enable + fade alpha.
    inputs: list[str] = ["ffmpeg", "-hide_banner", "-y", "-i", str(master)]
    for r in rendered:
        inputs.extend(["-loop", "1", "-i", r["png"]])

    filter_parts = ["[0:v]format=yuv420p[v0]"]
    current = "[v0]"
    for i, r in enumerate(rendered, start=1):
        # PNG input is loaded with `-loop 1`, so its stream timeline matches
        # the master video timeline (t=0 == master t=0). Fade timestamps are
        # therefore in MASTER OUTPUT seconds — fade in at `at`, fade out
        # `FADE_S` before `at + duration`.
        fade_in_st  = r["at"]
        fade_out_st = max(fade_in_st, r["at"] + r["duration"] - FADE_S)
        gfx_lbl = f"[g{i}]"
        filter_parts.append(
            f"[{i}:v]format=rgba,"
            f"fade=t=in:st={fade_in_st:.3f}:d={FADE_S:.3f}:alpha=1,"
            f"fade=t=out:st={fade_out_st:.3f}:d={FADE_S:.3f}:alpha=1"
            f"{gfx_lbl}"
        )
        out_lbl = f"[v{i}]"
        # `enable` gates visibility to [at, at+duration]; the alpha fades
        # above ensure clean ease-in/out at the boundaries.
        filter_parts.append(
            f"{current}{gfx_lbl}overlay="
            f"x={r['x']}:y={r['y']}:eval=init:"
            f"enable='between(t,{r['at']:.3f},{r['at'] + r['duration']:.3f})'"
            f"{out_lbl}"
        )
        current = out_lbl

    cmd = inputs + [
        "-filter_complex", ";".join(filter_parts),
        "-map", current,
        "-map", "0:a?",
        # Cap to source duration so the -loop 1 PNG inputs don't run forever.
        "-t", f"{fdur:.6f}",
        "-c:v", args.encoder,
    ]
    if args.encoder.startswith("h264_nvenc"):
        cmd.extend(["-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"])
    else:
        cmd.extend(["-preset", "medium", "-crf", "20"])
    cmd.extend([
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output),
    ])

    if args.dry_run:
        import shlex
        print(" ".join(shlex.quote(c) for c in cmd))
        return 0

    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg failed (exit {proc.returncode})")

    result = {
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "master":         str(master),
        "output":         str(output),
        "master_size":    f"{fw}x{fh}",
        "master_dur":     round(fdur, 3),
        "overlay_count":  len(rendered),
        "png_dir":        str(png_dir),
        "encoder":        args.encoder,
        "overlays":       rendered,
    }
    if args.emit_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"OK — {len(rendered)} graphics overlaid → {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
