"""
caption_render.py

1. Builds master.srt from EDL + transcripts (2-word UPPERCASE chunks)
2. Renders each cue as a transparent RGBA PNG:
     - font: Voltchu (or fallback bold), FONT_SIZE
     - background: #FFE600, rounded corners, NO shadow
     - text: #000000
3. Final FFmpeg pass: transpose=2 (90deg CCW) + PIL overlay per cue → final.mp4

Drop Voltchu.ttf in D:\\Claude Veditor\\ to activate it.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FFMPEG = (
    r"C:\Users\seanh\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
)
FFPROBE = FFMPEG.replace("ffmpeg.exe", "ffprobe.exe")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
EDIT_DIR = _PROJECT_ROOT / "videos" / "edit"
BASE_MP4 = EDIT_DIR / "base.mp4"
FINAL_MP4 = EDIT_DIR / "final.mp4"
SRT_PATH = EDIT_DIR / "master.srt"
EDL_PATH = EDIT_DIR / "edl.json"

# ── Caption style ─────────────────────────────────────────────────────────────
FONT_SIZE = 56
PAD_H, PAD_V = 30, 18
RADIUS = 18
BG = (255, 230, 0, 255)   # #FFE600
FG = (0, 0, 0, 255)        # #000000
CAP_BOTTOM_MARGIN = 140    # px from bottom edge of rotated frame

FONT_CANDIDATES = [
    str(_PROJECT_ROOT / "Voltchu.ttf"),
    r"C:\Windows\Fonts\Voltchu.ttf",
    r"C:\Users\seanh\AppData\Local\Microsoft\Windows\Fonts\Voltchu.ttf",
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]

PUNCT_BREAK = set(".,!?;:")


# ── Font ──────────────────────────────────────────────────────────────────────

def resolve_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            print(f"  font: {path}")
            return font
        except Exception:
            continue
    print("  font: PIL default (drop Voltchu.ttf in D:\\Claude Veditor\\ to activate)")
    return ImageFont.load_default()


# ── SRT builder ───────────────────────────────────────────────────────────────

def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, r = divmod(ms, 3_600_000)
    m, r = divmod(r, 60_000)
    s, ms = divmod(r, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t0: float, t1: float) -> list[dict]:
    return [
        w for w in transcript.get("words", [])
        if w.get("type") == "word"
        and w.get("start") is not None
        and w.get("end") is not None
        and w["end"] > t0
        and w["start"] < t1
    ]


def build_srt(edl: dict, edit_dir: Path, out_path: Path) -> list[tuple[float, float, str]]:
    transcripts_dir = edit_dir / "transcripts"
    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src = r["source"]
        seg_start, seg_end = float(r["start"]), float(r["end"])
        seg_dur = seg_end - seg_start
        tr_path = transcripts_dir / f"{src}.json"
        if not tr_path.exists():
            seg_offset += seg_dur
            continue

        transcript = json.loads(tr_path.read_text(encoding="utf-8"))
        words = _words_in_range(transcript, seg_start, seg_end)

        chunks: list[list[dict]] = []
        current: list[dict] = []
        for w in words:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            current.append(w)
            if len(current) >= 2 or (text and text[-1] in PUNCT_BREAK):
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        for chunk in chunks:
            a = max(0.0, chunk[0]["start"] - seg_start) + seg_offset
            b = max(0.0, chunk[-1]["end"] - seg_start) + seg_offset
            if b <= a:
                b = a + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip().rstrip(",;:").upper()
            entries.append((a, b, text))

        seg_offset += seg_dur

    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, 1):
        lines += [str(i), f"{_ts(a)} --> {_ts(b)}", t, ""]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  master.srt written: {len(entries)} cues")
    return entries


# ── Caption PNG ───────────────────────────────────────────────────────────────

def render_caption_png(
    text: str, font: ImageFont.FreeTypeFont, out_path: Path
) -> tuple[int, int]:
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    w, h = tw + PAD_H * 2, th + PAD_V * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=RADIUS, fill=BG)
    draw.text((PAD_H, PAD_V - bbox[1]), text, font=font, fill=FG)
    img.save(out_path)
    return w, h


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BASE_MP4.exists():
        sys.exit(f"base.mp4 not found: {BASE_MP4}")

    edl = json.loads(EDL_PATH.read_text(encoding="utf-8"))

    print("1/4  building master.srt …")
    entries = build_srt(edl, EDIT_DIR, SRT_PATH)

    print("2/4  probing base.mp4 …")
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,duration", "-of", "csv=p=0", str(BASE_MP4)],
        capture_output=True, text=True, check=True,
    )
    parts = probe.stdout.strip().split(",")
    src_w, src_h, src_dur = int(parts[0]), int(parts[1]), float(parts[2])
    rot_w, rot_h = src_h, src_w  # after transpose=2
    print(f"     base {src_w}×{src_h}  {src_dur:.3f}s  →  rotated {rot_w}×{rot_h}")

    print("3/4  rendering caption PNGs …")
    font = resolve_font(FONT_SIZE)
    cap_dir = EDIT_DIR / "_captions"
    cap_dir.mkdir(exist_ok=True)

    unique_texts = list(dict.fromkeys(t for _, _, t in entries))  # order-preserving dedup
    png_for: dict[str, tuple[Path, int, int]] = {}
    for text in unique_texts:
        slug = re.sub(r"[^a-z0-9]", "_", text.lower())[:40]
        p = cap_dir / f"{slug}.png"
        cw, ch = render_caption_png(text, font, p)
        png_for[text] = (p, cw, ch)
        print(f"     {text!r}  →  {cw}×{ch}px")

    print("4/4  compositing rotation + captions → final.mp4 …")
    inputs: list[str] = [FFMPEG, "-y", "-i", str(BASE_MP4)]
    for text in unique_texts:
        inputs += ["-loop", "1", "-i", str(png_for[text][0])]

    filter_parts = ["[0:v]transpose=2[base]"]
    current = "[base]"
    for idx, text in enumerate(unique_texts, start=1):
        _, cw, ch = png_for[text]
        x = max(0, (rot_w - cw) // 2)
        y = max(0, rot_h - ch - CAP_BOTTOM_MARGIN)
        windows = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b, t in entries if t == text)
        out_lbl = f"[v{idx}]"
        filter_parts.append(
            f"{current}[{idx}:v]overlay=x={x}:y={y}:enable='{windows}'{out_lbl}"
        )
        current = out_lbl

    cmd = inputs + [
        "-filter_complex", ";".join(filter_parts),
        "-map", current,
        "-map", "0:a",
        "-t", f"{src_dur:.6f}",   # cap output to base.mp4 duration; -loop 1 PNGs are infinite
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(FINAL_MP4),
    ]
    subprocess.run(cmd, check=True)

    size_mb = FINAL_MP4.stat().st_size / 1024 / 1024
    print(f"\ndone → {FINAL_MP4}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
