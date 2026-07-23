"""Compose the final 9:16 ad: b-roll (scaled to fill) + ElevenLabs VO + captions.

Pipeline (deterministic, ffmpeg only):
  1. Render each EDL segment to a normalized silent clip — scaled + center-cropped
     to fill WxH (no letterbox), locked to the target fps.
  2. Concat the segments into one base video (exactly the VO length).
  3. Final pass: burn captions LAST (studio hard-rule) over the base video, and
     mux the VO plus an optional ducked music bed.

Encoder: tries h264_nvenc, falls back to libx264 automatically so it runs on any
machine. Images and videos are both accepted as b-roll sources.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import common

IMAGE_EXTS = common.IMAGE_EXTS


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _encode(cmd_head: list[str], filters: list[str], out: Path,
            audio_tail: list[str] | None = None) -> None:
    """Run ffmpeg trying nvenc then libx264. `cmd_head` is everything up to the
    filter/codec section (inputs + -filter_complex/-vf already included except
    the -c:v). `filters` are the args right before codec (e.g. map args)."""
    for enc, extra in (("h264_nvenc", ["-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0"]),
                       ("libx264", ["-preset", "medium", "-crf", "20"])):
        cmd = list(cmd_head) + list(filters) + ["-c:v", enc, *extra,
              "-pix_fmt", "yuv420p"]
        if audio_tail:
            cmd += audio_tail
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(out)]
        r = _run(cmd)
        if r.returncode == 0:
            return
        if enc == "libx264":
            raise SystemExit("ffmpeg failed:\n" + (r.stderr or "")[-1200:])
        common.eprint(f"[compose] {enc} failed, retrying with libx264 …")


# ── captions (.ass) ──────────────────────────────────────────────────────────
def ass_ts(t: float) -> str:
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _wrap(text: str, max_chars: int) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\\N".join(lines)


def build_captions_ass(cfg: dict, edl: list[dict], out: Path) -> None:
    v = cfg.get("video", {})
    w, h = v.get("width", 1080), v.get("height", 1920)
    font = cfg.get("captions", {}).get("font", "Arial")
    size = int(h * 0.030)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HB0000000,-1,0,0,0,100,100,0,0,3,{int(h*0.010)},0,2,80,80,{int(h*0.16)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    for seg in edl:
        text = _wrap(seg["text"].replace("\n", " ").strip(), 30)
        ev.append(f"Dialogue: 0,{ass_ts(seg['start'])},{ass_ts(seg['end'])},Cap,,0,0,0,,"
                  f"{{\\fad(120,80)}}{text}")
    out.write_text(header + "\n".join(ev) + "\n", encoding="utf-8")


def _ff_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


# ── music leveling ───────────────────────────────────────────────────────────
def _mean_db(path: Path) -> float | None:
    try:
        r = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
                  "-f", "null", "-"])
        m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", r.stderr)
        return float(m.group(1)) if m else None
    except Exception:
        return None


# ── segment rendering ────────────────────────────────────────────────────────
def _render_segment(cfg: dict, seg: dict, out: Path) -> None:
    v = cfg.get("video", {})
    w, h, fps = v.get("width", 1080), v.get("height", 1920), v.get("fps", 30)
    src = Path(seg["source"])
    dur = seg["end"] - seg["start"]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},setsar=1,fps={fps}")
    is_image = src.suffix.lower() in IMAGE_EXTS
    if is_image:
        head = ["ffmpeg", "-y", "-hide_banner", "-loop", "1", "-t", f"{dur:.3f}",
                "-i", str(src), "-vf", vf]
    else:
        head = ["ffmpeg", "-y", "-hide_banner",
                "-ss", f"{seg.get('source_in', 0.0):.3f}", "-t", f"{dur:.3f}",
                "-i", str(src), "-vf", vf]
    _encode(head, [], out)


def render_final(cfg: dict, vo_path: Path, edl: list[dict], out_path: Path,
                 music_path: Path | None = None) -> dict:
    v = cfg.get("video", {})
    fps = v.get("fps", 30)
    total = max(seg["end"] for seg in edl)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 1. per-segment normalized clips
        seg_files = []
        for i, seg in enumerate(edl):
            sf = tmp / f"seg_{i:03d}.mp4"
            _render_segment(cfg, seg, sf)
            seg_files.append(sf)
        # 2. concat
        concat_list = tmp / "list.txt"
        concat_list.write_text(
            "".join(f"file '{sf.as_posix()}'\n" for sf in seg_files), encoding="utf-8")
        base = tmp / "base.mp4"
        r = _run(["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0",
                  "-i", str(concat_list), "-c", "copy", str(base)])
        if r.returncode != 0:   # fall back to re-encode concat
            _encode(["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0",
                     "-i", str(concat_list)], [], base)

        # 3. captions + audio mux (captions burned LAST)
        cap_on = cfg.get("captions", {}).get("enabled", True)
        cap_ass = tmp / "caps.ass"
        if cap_on:
            build_captions_ass(cfg, edl, cap_ass)

        inputs = ["ffmpeg", "-y", "-hide_banner", "-i", str(base), "-i", str(vo_path)]
        filt = []
        vlabel = "0:v"
        if cap_on:
            filt.append(f"[0:v]ass='{_ff_path(cap_ass)}'[vid]")
            vlabel = "vid"

        amix = ["[1:a]"]
        if music_path and cfg.get("music", {}).get("enabled", True):
            inputs += ["-i", str(music_path)]
            duck = cfg.get("music", {}).get("vo_duck_db", -22)
            mean = _mean_db(music_path)
            gain = (duck - mean) if mean is not None else -14.0
            filt.append(
                f"[2:a]atrim=0:{total:.3f},volume={gain:.1f}dB,"
                f"afade=t=out:st={max(0.0, total-1.0):.2f}:d=1.0[mus]")
            filt.append("[1:a][mus]amix=inputs=2:duration=first:normalize=0[aout]")
            alabel = "aout"
        else:
            alabel = "1:a"

        fc = ";".join(filt) if filt else ""
        head = list(inputs)
        if fc:
            head += ["-filter_complex", fc]
        maps = ["-map", f"[{vlabel}]" if vlabel != "0:v" else "0:v",
                "-map", f"[{alabel}]" if alabel not in ("1:a",) else "1:a"]
        audio_tail = ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        _encode(head, maps, out_path, audio_tail=audio_tail)

    return {"output": str(out_path), "duration_s": round(total, 2),
            "segments": len(edl), "captions": cap_on,
            "music": str(music_path) if music_path else None}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Compose final DiH ad from VO + EDL.")
    ap.add_argument("--vo", type=Path, required=True)
    ap.add_argument("--edl", type=Path, required=True)
    ap.add_argument("--music", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    cfg = common.load_config()
    edl = json.loads(args.edl.read_text(encoding="utf-8"))
    res = render_final(cfg, args.vo, edl, args.out, args.music)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
