"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Lossless -c copy concat into base.mp4
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=35).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}


# -------- Hardware encoder selection --------------------------------------
#
# Software libx264 is ~10× slower than NVENC on a modern RTX. For 1080p
# talking-head / social content the visual quality difference at matched bit
# rates is invisible. We default to NVENC if the host has it.
#
# Override priority: --encoder CLI flag > VEDITOR_VIDEO_ENCODER env > auto.

_ENCODER_CACHE: str | None = None


def _detect_nvenc_available() -> bool:
    """Return True if ffmpeg on PATH advertises h264_nvenc."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
        return b"h264_nvenc" in out.stdout
    except Exception:
        return False


def select_encoder(override: str | None = None) -> str:
    """Resolve the active video encoder. Returns one of:
    'nvenc', 'nvenc-hq', 'x264'. Caches once per process.
    """
    global _ENCODER_CACHE
    if override:
        _ENCODER_CACHE = override
        return override
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE
    env = os.environ.get("VEDITOR_VIDEO_ENCODER", "auto").lower()
    if env in ("nvenc", "nvenc-hq", "x264"):
        _ENCODER_CACHE = env
        return env
    # auto
    _ENCODER_CACHE = "nvenc" if _detect_nvenc_available() else "x264"
    return _ENCODER_CACHE


def video_codec_args(tier: str, encoder: str | None = None) -> list[str]:
    """Return ffmpeg `-c:v ...` args for the requested quality tier.

    Tiers: 'draft' (cut-point check), 'preview' (QC), 'final' (segment
    encode), 'composite' (final composite — slightly higher quality).
    """
    enc = encoder or select_encoder()
    # Quality knobs are matched so output PSNR is roughly equivalent to the
    # original libx264 settings: ultrafast/cf28, medium/cf22, fast/cf20,
    # fast/cf18.
    if enc == "x264":
        m = {
            "draft":     ("ultrafast", "28"),
            "preview":   ("medium",    "22"),
            "final":     ("fast",      "20"),
            "composite": ("fast",      "18"),
        }
        preset, crf = m[tier]
        return ["-c:v", "libx264", "-preset", preset, "-crf", crf]

    # NVENC: use VBR with CQ as the quality target. p5/p6 are the
    # quality-leaning presets that still encode at multi-realtime on RTX.
    if enc == "nvenc":
        m = {
            "draft":     ("p1", "30"),
            "preview":   ("p4", "23"),
            "final":     ("p5", "21"),
            "composite": ("p5", "19"),
        }
    else:  # nvenc-hq
        m = {
            "draft":     ("p4", "27"),
            "preview":   ("p6", "21"),
            "final":     ("p7", "19"),
            "composite": ("p7", "17"),
        }
    preset, cq = m[tier]
    return [
        "-c:v", "h264_nvenc",
        "-preset", preset,
        "-rc", "vbr",
        "-cq", cq,
        "-b:v", "0",
        "-tune", "hq",
        "-spatial_aq", "1",
    ]


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=90"
)

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if draft:
        scale = "scale=1280:-2"
    else:
        scale = "scale=1920:-2"

    vf_parts: list[str] = []
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts)

    # 30ms audio fades at both edges (Rule 3) — prevent pops
    fade_out_start = max(0.0, duration - 0.03)
    af = f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03"

    tier = "draft" if draft else ("preview" if preview else "final")
    codec_args = video_codec_args(tier)

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-af", af,
        *codec_args,
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        extract_segment(src_path, start, duration, seg_filter, out_path, preview=preview, draft=draft)
        seg_paths.append(out_path)

    return seg_paths


# -------- Lossless concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def _apply_case(text: str, mode: str) -> str:
    if mode == "upper":
        return text.upper()
    if mode == "sentence":
        return text[:1].upper() + text[1:].lower() if text else text
    return text  # natural


def build_master_srt(
    edl: dict,
    edit_dir: Path,
    out_path: Path,
    *,
    max_chars: int = 30,
    max_lines: int = 1,
    min_duration: float = 1.5,
    gap_frames: int = 0,
    tail_pad_ms: int = 250,
    fps: int = 24,
    case_style: str = "natural",
    time_offset: float = 0.0,
) -> None:
    """Build an output-timeline SRT, Premiere-style.

    Chunking:
      - Greedy fill words into a "line" until next word would exceed `max_chars`.
      - Stack lines into a caption block of up to `max_lines` lines.
      - Soft-break at end-of-sentence punctuation even if the line isn't full.
    Timing:
      - Each block start = first word's onset (relative to segment + global offset).
      - Each block end   = last word's end + `tail_pad_ms` (so consonant releases
        and breath tails aren't cut off — the #1 cause of the "last line cut off"
        feel). The final block in the last kept segment may extend past
        seg_end by tail_pad_ms; intermediate blocks are clamped to seg_end.
      - If a block is shorter than `min_duration`, extend its end (or merge with
        the next block if total chars still fit).
      - Enforce a `gap_frames`-frame gap between consecutive blocks (at `fps`).
      - `case_style`: "natural" | "upper" | "sentence".
    """
    transcripts_dir = edit_dir / "transcripts"
    tail_pad = max(0.0, tail_pad_ms / 1000.0)
    gap_s = max(0.0, gap_frames / float(fps))

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0
    ranges = edl["ranges"]

    for r_idx, r in enumerate(ranges):
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start
        is_last_segment = (r_idx == len(ranges) - 1)

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        # ---- Step 1: pack words into lines (max_chars), then lines into blocks (max_lines) ----
        blocks: list[list[dict]] = []  # each block is a flat list of word dicts
        current_lines: list[list[dict]] = [[]]
        current_line_chars = 0

        def line_text(words: list[dict]) -> str:
            return " ".join((w.get("text") or "").strip() for w in words).strip()

        def flush_block():
            nonlocal current_lines, current_line_chars
            flat = [w for ln in current_lines for w in ln if w]
            if flat:
                blocks.append(flat)
            current_lines = [[]]
            current_line_chars = 0

        for w in words_in_seg:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            ends_in_terminal = bool(text) and text[-1] in ".!?"
            tentative_chars = current_line_chars + (1 if current_line_chars else 0) + len(text)

            if tentative_chars > max_chars and current_line_chars > 0:
                # Wrap to next line within the same block, OR flush block if at max_lines.
                if len(current_lines) < max_lines:
                    current_lines.append([w])
                    current_line_chars = len(text)
                else:
                    flush_block()
                    current_lines = [[w]]
                    current_line_chars = len(text)
            else:
                current_lines[-1].append(w)
                current_line_chars = tentative_chars

            if ends_in_terminal:
                # Soft break on sentence end: flush block.
                flush_block()

        flush_block()

        # ---- Step 2: compute timing for each block ----
        block_entries: list[list[float]] = []  # mutable [start, end, text]
        for b_idx, block in enumerate(blocks):
            if not block:
                continue
            local_start = max(seg_start, float(block[0].get("start", seg_start)))
            raw_end = float(block[-1].get("end", seg_end))
            # Tail-pad past the spoken word; clamp to seg_end except for the
            # final block of the final segment (which may bleed slightly past
            # the segment to give the closing consonant room).
            ceiling = seg_end + tail_pad if (is_last_segment and b_idx == len(blocks) - 1) else seg_end
            local_end = min(ceiling, raw_end + tail_pad)

            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4

            # Build text with line breaks reconstructed from packing logic.
            # Re-pack flat block into wrapped lines for display.
            display_lines: list[str] = []
            buf: list[str] = []
            buf_chars = 0
            for w in block:
                t = (w.get("text") or "").strip()
                if not t:
                    continue
                add = (1 if buf_chars else 0) + len(t)
                if buf_chars + add > max_chars and buf:
                    display_lines.append(" ".join(buf))
                    buf = [t]
                    buf_chars = len(t)
                else:
                    buf.append(t)
                    buf_chars += add
            if buf:
                display_lines.append(" ".join(buf))
            display_lines = display_lines[:max_lines]
            text = "\n".join(display_lines)
            text = re.sub(r"[ \t]+", " ", text).strip()
            text = _apply_case(text, case_style)

            block_entries.append([out_start, out_end, text])

        # ---- Step 3: enforce min_duration + inter-block gap (within segment) ----
        for i, e in enumerate(block_entries):
            dur = e[1] - e[0]
            if dur < min_duration:
                e[1] = e[0] + min_duration
        for i in range(len(block_entries) - 1):
            cur, nxt = block_entries[i], block_entries[i + 1]
            min_next_start = cur[1] + gap_s
            if nxt[0] < min_next_start:
                nxt[0] = min_next_start
                # Don't shorten the next block below min_duration.
                if nxt[1] - nxt[0] < min_duration:
                    nxt[1] = nxt[0] + min_duration

        for e in block_entries:
            entries.append((e[0], e[1], e[2]))

        seg_offset += seg_duration

    # ---- Step 4: cross-segment gap enforcement, sort, write ----
    entries.sort(key=lambda e: e[0])
    fixed: list[tuple[float, float, str]] = []
    for e in entries:
        if fixed:
            prev_end = fixed[-1][1]
            if e[0] < prev_end + gap_s:
                start = prev_end + gap_s
                end = max(e[1], start + min_duration)
                fixed.append((start, end, e[2]))
                continue
        fixed.append(e)

    # Apply global time offset (e.g. when source still has a silent head from
    # an unsynced audio offset that wasn't trimmed). Default 0.0 = no shift.
    if time_offset:
        fixed = [(a + time_offset, b + time_offset, t) for (a, b, t) in fixed]

    lines: list[str] = []
    for i, (a, b, t) in enumerate(fixed, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"master SRT → {out_path.name} ({len(fixed)} cues, "
          f"max-chars={max_chars}, lines≤{max_lines}, min-dur={min_duration}s, "
          f"tail-pad={tail_pad_ms}ms, case={case_style}, "
          f"offset={time_offset:+.3f}s)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':force_style='{SUB_FORCE_STYLE}'[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        *video_codec_args("composite"),
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--encoder",
        choices=["auto", "nvenc", "nvenc-hq", "x264"],
        default="auto",
        help="Video encoder. 'auto' (default) uses NVENC if the GPU supports it, "
             "else libx264. 'nvenc' is the speed default. 'nvenc-hq' is slower but "
             "closer to libx264 quality. 'x264' forces software encoding.",
    )
    # ---- Caption format (Premiere-style) ----
    ap.add_argument("--caption-max-chars", type=int, default=30,
                    help="Max characters per caption line (10–100). Default 30.")
    ap.add_argument("--caption-max-lines", type=int, choices=[1, 2], default=1,
                    help="1 = Single Line, 2 = Double Line. Default 1.")
    ap.add_argument("--caption-min-duration", type=float, default=1.5,
                    help="Minimum on-screen duration per caption block, in seconds. Default 1.5.")
    ap.add_argument("--caption-gap-frames", type=int, default=0,
                    help="Frames of gap between captions (at --fps). Default 0 (back-to-back).")
    ap.add_argument("--caption-tail-pad", type=int, default=250,
                    help="Tail padding per caption end, in milliseconds. Prevents the "
                         "'last line cut off' feel by outlasting final consonants. Default 250.")
    ap.add_argument("--caption-case", choices=["natural", "upper", "sentence"],
                    default="natural",
                    help="Caption text case. Default 'natural' (preserve transcript casing).")
    ap.add_argument("--caption-time-offset", type=float, default=0.0,
                    help="Shift every caption timestamp by N seconds. Use this when the "
                         "source has a silent head (e.g. you ran sync_audio.py with "
                         "--no-trim-head). Default 0.")
    ap.add_argument("--fps", type=int, default=24,
                    help="Output frame rate. Used by gap-frames calc and segment encode. Default 24.")
    args = ap.parse_args()
    if args.encoder != "auto":
        select_encoder(args.encoder)
    print(f"video encoder: {select_encoder()}")

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(
                edl, edit_dir, subs_path,
                max_chars=args.caption_max_chars,
                max_lines=args.caption_max_lines,
                min_duration=args.caption_min_duration,
                gap_frames=args.caption_gap_frames,
                tail_pad_ms=args.caption_tail_pad,
                fps=args.fps,
                case_style=args.caption_case,
                time_offset=args.caption_time_offset,
            )
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    # 4. Composite (overlays + subtitles LAST) → intermediate (pre-loudnorm) path
    overlays = edl.get("overlays") or []
    if args.no_loudnorm:
        # Composite directly to final output
        build_final_composite(base_path, overlays, subs_path, out_path, edit_dir)
    else:
        # Composite to a temp file, then run loudnorm → final output
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir)
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
