"""variant_factory.py — one VO + B-roll variant per invocation.

This is the deterministic per-variant builder for the "increase output" flow.
The studio's Dice "VO + B-roll only" mode generates N rewritten scripts in
one Claude call (with brand-guidelines context + different angles), then
invokes this helper once per script. Each call produces one finished MP4.

Pipeline (no LLM calls — Claude rewrote upstream):
  1. TTS the rewritten script via ElevenLabs (tts_voice.synthesize)
  2. Transcribe the resulting VO with ElevenLabs Scribe to recover
     word-level timestamps — used for caption chunking. Re-transcribing
     the TTS output is cheaper + more accurate than estimating timings
     from character counts, and it reuses transcribe.py's caching path.
  3. Probe VO duration.
  4. Random-shuffle the b-roll folder, take clips until total duration
     exceeds VO duration, trim the last clip so the concat lands exactly
     on VO end. Each clip is normalized to target res/fps before concat
     (broll_overlay's same defensive `fps=`/`settb=` pattern — without
     this, concat boundaries silently drop frames and audio drifts).
  5. Mux VO audio over the silent b-roll concat (original clip audio is
     dropped — voiceover only).
  6. Build SRT + ASS from word timestamps using the studio's caption
     chunking rules (max chars, min duration, tail-pad). Burn captions
     last with the chosen Alignment + MarginV.

Usage:
    python helpers/variant_factory.py \
        --broll-folder PATH \
        --script-text "<rewritten hook+body+cta>" \
        --output OUT.mp4 \
        --voice-id <11l_voice_id> \
        [--width 1080 --height 1920 --fps 30] \
        [--caption-font Arial --caption-size 42 \
         --caption-bg "#FFDE59" --caption-fg "#1A0F40" \
         --caption-max-chars 20 --caption-min-duration 1.5 \
         --caption-tail-pad 0.25 \
         --caption-alignment 2 --caption-margin-v 540] \
        [--keep-temps]

JSON mode (`--json`) prints a machine-readable result the orchestrator can
parse — output path, VO duration, b-roll clips used, caption count.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# Reuse existing helpers — both live in this directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tts_voice import synthesize as tts_synthesize, load_api_key as load_11l_key  # type: ignore

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}


# --------------------------------------------------------------------------
# Probe + utility
# --------------------------------------------------------------------------

def _probe_duration(path: Path) -> float:
    """Return the format-level duration in seconds, 0.0 on failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        return float(out or 0.0)
    except Exception:
        return 0.0


def _probe_audio_meanvol(path: Path) -> tuple[float, float]:
    """Return (mean_dBFS, peak_dBFS) of the clip's audio. (-85, -85) on failure
    (effectively "silence" — won't be flagged as a talking head).

    Used by the talking-head heuristic. mean_volume is the average level in
    dBFS over the whole track; peak is the max sample peak. Talking heads
    with someone close to the mic average ~-29 dB or higher, while action
    b-roll with transient drill/hammer sounds averages ~-32 dB or lower
    even when peaks are similar.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-af", "volumedetect", "-vn", "-sn", "-dn", "-f", "null", "-"],
            capture_output=True, text=True, check=False,
        )
        out = (proc.stderr or "") + (proc.stdout or "")
        import re as _re
        mean = -85.0
        peak = -85.0
        m = _re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", out)
        if m: mean = float(m.group(1))
        m = _re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", out)
        if m: peak = float(m.group(1))
        return (mean, peak)
    except Exception:
        return (-85.0, -85.0)


def is_likely_talking_head(
    path: Path,
    *,
    mean_threshold_db: float = -32.0,
    min_duration_s: float = 3.0,
) -> tuple[bool, str]:
    """Heuristic: does this clip look like talking-head footage (continuous
    speech) rather than an action shot?

    Talking-head clips have sustained loud audio (someone close to the mic);
    action clips have transient peaks but a much lower mean. We threshold on
    mean_volume because peak alone misfires on loud drills/hammers.

    Skips very short clips (< min_duration_s) — they can't carry enough
    statistical signal to classify reliably.

    Returns (is_talking_head, reason). reason is empty when not flagged.
    """
    dur = _probe_duration(path)
    if dur <= 0:
        return (False, "")  # can't probe → don't reject blindly
    if dur < min_duration_s:
        return (False, "")
    mean, peak = _probe_audio_meanvol(path)
    if mean > mean_threshold_db:
        return (True, f"mean_volume={mean:.1f}dB > {mean_threshold_db:.1f}dB (sustained speech-like audio)")
    return (False, "")


def _encoder_args() -> list[str]:
    """NVENC when available (render.py decides), x264 fallback.

    ALWAYS forces 8-bit 4:2:0 output (`-pix_fmt yuv420p`) + baseline-safe
    profile/level. Some source b-roll is 10-bit (yuv420p10le / H.264 High 10);
    without this pin, NVENC inherits the input bit depth and emits a 10-bit
    file that Windows Media Player, QuickTime, browsers, and Meta all REFUSE
    to play (the "output rendered but won't open" bug, 2026-07-07). 8-bit High
    @ 4.2 is the universally-safe social-video baseline."""
    try:
        from render import select_encoder  # type: ignore
        enc = select_encoder()
    except Exception:
        enc = "x264"
    if enc.startswith("nvenc"):
        return [
            "-c:v", "h264_nvenc", "-preset", "p6",
            "-rc", "vbr", "-cq", "19", "-b:v", "0",
            "-tune", "hq", "-spatial_aq", "1",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2",
        ]
    return [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2",
    ]


# --------------------------------------------------------------------------
# Step 1: TTS
# --------------------------------------------------------------------------

def step_tts(
    script_text: str,
    voice_id: str,
    out_path: Path,
    api_key: str,
    *,
    model: str = "eleven_multilingual_v2",
    stability: float = 0.5,
    similarity: float = 0.75,
    style: float = 0.0,
    speaker_boost: bool = True,
    output_format_q: str | None = None,
) -> int:
    """Generate VO audio. Returns bytes written. Raises on API failure."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return tts_synthesize(
        api_key=api_key, voice_id=voice_id, text=script_text,
        model=model, output=out_path,
        stability=stability, similarity=similarity, style=style,
        speaker_boost=speaker_boost, output_format_q=output_format_q,
    )


# --------------------------------------------------------------------------
# Step 2: Transcribe VO for word timestamps
# --------------------------------------------------------------------------

def step_transcribe_vo(vo_path: Path, edit_dir: Path) -> dict:
    """Run ElevenLabs Scribe on the VO to recover word-level timestamps.

    CRITICAL: transcribe_one keys its on-disk cache by `<vo_path.stem>.json`.
    The caller MUST ensure `vo_path` has a UNIQUE stem per invocation — if
    multiple variant_factory runs reused stem "vo", the second one onward
    would silently read the first variant's transcript and burn the wrong
    captions onto its video. That bit us once 2026-06-24. `run()` now
    names the VO file `vo_<tmp_dir_name>.mp3` to enforce uniqueness.
    """
    from transcribe import transcribe_one, load_api_key  # type: ignore
    transcripts_dir = edit_dir / "transcripts"
    out_json = transcripts_dir / f"{vo_path.stem}.json"
    if not out_json.exists():
        transcribe_one(vo_path, edit_dir, load_api_key(), verbose=False)
    return json.loads(out_json.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Step 3-4: B-roll random concat sized to VO
# --------------------------------------------------------------------------

PROXY_DIRNAME = "_proxies"       # cached 1080p downscales of heavy (4K) source clips
PROXY_MAX_EDGE = 1920            # longest edge of a proxy; 4K (3840×2160) → 1920×1080


def _discover_broll(folder: Path) -> list[Path]:
    """Return all video files directly under `folder` (one level deep, plus
    common B-Roll subfolders like Install/Final/evergreen). Skips obvious
    helper outputs (`*_synced.mp4`) and the `_proxies` cache dir."""
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"b-roll folder not found: {folder}")
    out: list[Path] = []
    for root, dirs, files in os.walk(folder):
        # Don't recurse into 'synced' (sync outputs) or '_proxies' (our cache).
        dirs[:] = [d for d in dirs if d.lower() != "synced" and d != PROXY_DIRNAME]
        for fname in files:
            if fname.endswith("_synced.mp4"):
                continue
            p = Path(root) / fname
            if p.suffix.lower() in VIDEO_EXTS:
                out.append(p)
    return out


def _proxy_path(clip: Path, broll_root: Path) -> Path:
    """Stable cache path for a clip's 1080p proxy: <root>/_proxies/<stem>_<hash>.mp4.
    The hash of the resolved source path avoids collisions across subfolders."""
    import hashlib
    h = hashlib.md5(str(clip.resolve()).encode("utf-8")).hexdigest()[:8]
    return broll_root / PROXY_DIRNAME / f"{clip.stem}_{h}.mp4"


def _needs_proxy(clip: Path) -> bool:
    """True if the clip is larger than 1080p on either edge (worth downscaling)."""
    w = h = 0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(clip)],
            capture_output=True, text=True, check=True).stdout.strip()
        parts = out.split(",")
        w, h = int(parts[0]), int(parts[1])
    except Exception:
        return False
    return max(w, h) > PROXY_MAX_EDGE


def build_proxies(broll_folder: Path, *, log=lambda m: None) -> dict:
    """Downscale every >1080p source clip to a cached 1080p proxy, ONE AT A
    TIME (sequential — never overloads a VRAM-limited GPU, so it's safe to run
    while editing in another app). Idempotent: skips clips whose proxy already
    exists and is newer than the source. Returns a small summary dict.

    This is the resource fix for heavy 4K b-roll: decode/scale each clip once
    here, then every variant render (and re-run) works off the light proxies."""
    root = broll_folder.resolve()
    clips = _discover_broll(root)
    built = skipped = small = failed = 0
    (root / PROXY_DIRNAME).mkdir(parents=True, exist_ok=True)
    for i, clip in enumerate(clips, 1):
        if not _needs_proxy(clip):
            small += 1
            continue
        proxy = _proxy_path(clip, root)
        if proxy.exists() and proxy.stat().st_mtime >= clip.stat().st_mtime:
            skipped += 1
            continue
        tmp = proxy.with_suffix(".tmp.mp4")
        # Scale to fit within 1920×1920 preserving aspect (4K→1080p), 8-bit,
        # drop audio (b-roll audio is unused). Sequential = gentle on the GPU.
        # Proxies are throwaway intermediates (the final render re-encodes at
        # cq19), so use a FAST, lighter encode: fastest NVENC preset + higher
        # cq → quicker one-time build and smaller/faster-to-decode files.
        try:
            from render import select_encoder  # type: ignore
            _enc = select_encoder()
        except Exception:
            _enc = "x264"
        # cq/crf 32 = heavier compression → proxies land ~half the size of a
        # cq26 build (disk is tight on this box). Proxies are throwaway
        # intermediates behind text overlays, so the extra compression is
        # invisible in practice; the final deliverable is a separate cq19 pass.
        if _enc.startswith("nvenc"):
            enc_args = ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr",
                        "-cq", "32", "-b:v", "0", "-pix_fmt", "yuv420p"]
        else:
            enc_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
                        "-pix_fmt", "yuv420p"]
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-i", str(clip),
            "-vf", f"scale=w={PROXY_MAX_EDGE}:h={PROXY_MAX_EDGE}:"
                   f"force_original_aspect_ratio=decrease",
            *enc_args, "-an", "-movflags", "+faststart", str(tmp),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and tmp.exists():
            os.replace(tmp, proxy)
            built += 1
            log(f"[proxy {i}/{len(clips)}] {clip.name}")
        else:
            failed += 1
            try: tmp.unlink()
            except OSError: pass
            sys.stderr.write(proc.stderr[-1500:])
    summary = {"total": len(clips), "built": built, "reused": skipped,
               "already_1080p": small, "failed": failed,
               "proxy_dir": str(root / PROXY_DIRNAME)}
    log(f"proxies: built={built} reused={skipped} native1080p={small} failed={failed}")
    return summary


def _source_for(clip: Path, broll_root: Path) -> Path:
    """Return the cached proxy for a clip if one exists, else the original."""
    proxy = _proxy_path(clip, broll_root.resolve())
    return proxy if proxy.exists() else clip


def step_plan_broll_sequence(
    broll_folder: Path,
    vo_duration: float,
    *,
    seed: int | None = None,
    min_clip_use: float = 1.2,
    exclude_talking_heads: bool = False,
    exclude_paths: list[Path] | None = None,
    talking_head_mean_threshold_db: float = -32.0,
) -> tuple[list[tuple[Path, float, float]], list[dict]]:
    """Return (plan, rejections) covering vo_duration.

    plan: list of (clip_path, in_seconds, out_seconds).
    rejections: list of {"source", "reason"} for clips skipped by the filters
      (talking-head heuristic OR explicit exclude list). Surfaced to the
      caller so the orchestrator can show "why these clips were skipped."

    `exclude_talking_heads`: when True, probe each clip's mean_volume; reject
      clips with mean > talking_head_mean_threshold_db AND duration > 3s
      (continuous loud audio = likely speech to camera).
    `exclude_paths`: explicit blacklist. Each entry can be a file (exact match)
      OR a folder (prefix match — anything under that folder is excluded).
    """
    clips = _discover_broll(broll_folder)
    if not clips:
        raise SystemExit(f"no b-roll clips found under {broll_folder}")
    rng = random.Random(seed)
    rng.shuffle(clips)

    rejections: list[dict] = []

    # Apply explicit-exclude filter first (cheap path check).
    if exclude_paths:
        ex_resolved = []
        for e in exclude_paths:
            try: ex_resolved.append(Path(e).resolve())
            except Exception: pass
        kept: list[Path] = []
        for p in clips:
            pr = p.resolve()
            hit = False
            for e in ex_resolved:
                if e == pr or (e.is_dir() and str(pr).startswith(str(e) + os.sep)):
                    rejections.append({"source": str(p), "reason": f"excluded by --exclude-paths ({e.name})"})
                    hit = True
                    break
            if not hit:
                kept.append(p)
        clips = kept

    # Talking-head filter (more expensive — runs ffmpeg per clip).
    if exclude_talking_heads:
        kept = []
        for p in clips:
            is_th, reason = is_likely_talking_head(
                p, mean_threshold_db=talking_head_mean_threshold_db,
            )
            if is_th:
                rejections.append({"source": str(p), "reason": reason})
            else:
                kept.append(p)
        clips = kept

    if not clips:
        raise SystemExit(
            f"every b-roll clip was filtered out (talking-head + exclude-list). "
            f"Loosen --talking-head-threshold or remove --exclude-talking-heads."
        )

    durations = {p: _probe_duration(p) for p in clips}
    total_avail = sum(d for d in durations.values() if d > 0)
    if total_avail < vo_duration:
        raise SystemExit(
            f"b-roll folder has only {total_avail:.1f}s of material AFTER filtering "
            f"but VO is {vo_duration:.1f}s. Add more action clips, loosen the "
            f"talking-head threshold, or shorten the script."
        )

    # RANDOM-WINDOW planning (2026-07-07): instead of playing each shuffled
    # clip from its head, pull a random 1.8–5.0s window from a random offset
    # WITHIN each clip. A 6s clip might contribute 1.0–4.2 in one run and
    # 2.7–6.0 in the next — so the same folder yields effectively unlimited
    # distinct sequences, and short punchy cuts pace better for Meta ads.
    SEG_MIN, SEG_MAX = 1.8, 5.0

    def _random_window(d: float, remaining: float) -> tuple[float, float]:
        """Pick (start, take) for a clip of duration d, owing `remaining`s."""
        seg_hi = min(SEG_MAX, d, max(remaining, 0.1))
        seg_lo = min(SEG_MIN, seg_hi)
        take = seg_hi if seg_hi <= seg_lo else rng.uniform(seg_lo, seg_hi)
        # Tail of the video: fill exactly what's owed if the clip can cover it.
        if remaining < seg_hi:
            take = min(d, remaining)
        start_max = max(0.0, d - take)
        start = rng.uniform(0.0, start_max) if start_max > 0 else 0.0
        return round(start, 3), round(take, 3)

    plan: list[tuple[Path, float, float]] = []
    used = 0.0
    for p in clips:
        d = durations.get(p, 0.0)
        if d < min_clip_use:
            continue
        remaining = vo_duration - used
        if remaining <= 0.0:
            break
        start, take = _random_window(d, remaining)
        plan.append((p, start, round(start + take, 3)))
        used += take
    # If we exhausted the filtered list but still owe duration, loop back and
    # reuse clips — each pass draws a FRESH random window, so even reuse
    # doesn't repeat footage ranges.
    while used < vo_duration - 0.01:
        progressed = False
        for p in clips:
            d = durations.get(p, 0.0)
            if d < 0.5:
                continue
            remaining = vo_duration - used
            if remaining <= 0.01:
                break
            start, take = _random_window(d, remaining)
            plan.append((p, start, round(start + take, 3)))
            used += take
            progressed = True
        if not progressed:
            break
    return plan, rejections


# ── Semantic planning (2026-07-20): route through broll_match.py (local CLIP)
# so autonomous variants can be script-relevant instead of random-window. Keeps
# the same (plan, rejections) contract as step_plan_broll_sequence so the call
# site swaps cleanly. Falls back to random if the matcher can't run.

def _segments_from_transcript(transcript: dict, vo_duration: float,
                              seg_len: float = 3.5) -> list[dict]:
    """Contiguous [0, vo_duration] windows (~seg_len each), text = the VO words
    whose midpoint lands in each window. Guarantees full coverage with no gaps
    so the concat sums to the VO length."""
    words = [w for w in (transcript.get("words") or []) if w.get("type") == "word"]
    n = max(1, round(vo_duration / max(1.5, seg_len)))
    step = vo_duration / n
    segs: list[dict] = []
    for i in range(n):
        s = i * step
        e = (i + 1) * step if i < n - 1 else vo_duration
        txt = " ".join(
            str(w.get("text", "")) for w in words
            if s <= (float(w.get("start", 0)) + float(w.get("end", 0))) / 2 < e
        ).strip()
        segs.append({"start": round(s, 3), "end": round(e, 3),
                     "text": txt or "product b-roll"})
    return segs


def _run_broll_match(broll_folder: Path, segs: list[dict], min_score: float) -> list[dict]:
    helper = Path(__file__).resolve().parent / "broll_match.py"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tf:
        json.dump(segs, tf)
        segs_file = tf.name
    try:
        cmd = [sys.executable, str(helper), "--broll-folder", str(broll_folder),
               "--segments", segs_file, "--json", "--min-score", str(min_score)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-400:] or f"exit {r.returncode}")
        return json.loads(r.stdout)
    finally:
        try:
            os.unlink(segs_file)
        except OSError:
            pass


def step_plan_broll_semantic(
    broll_folder: Path,
    vo_duration: float,
    transcript: dict,
    *,
    seed: int | None = None,
    min_score: float = 0.18,
    seg_len: float = 3.5,
    fallback: str = "none",
    orientation: str = "portrait",
    exclude_talking_heads: bool = False,
    exclude_paths: list[Path] | None = None,
    talking_head_mean_threshold_db: float = -32.0,
    log=lambda m: None,
) -> tuple[list[tuple[Path, float, float]], list[dict]]:
    """Semantic variant of step_plan_broll_sequence. Same return contract.

    Builds VO segments from the transcript, matches each to its best local clip
    via broll_match.py, optionally fetches free stock B-roll for low-confidence
    lines (fallback="stock"), and returns a (clip, in, out) plan. If the matcher
    can't run (missing deps / no clips), falls back to the random planner so a
    batch never hard-fails."""
    segs = _segments_from_transcript(transcript, vo_duration, seg_len)
    try:
        edl = _run_broll_match(broll_folder, segs, min_score)
    except Exception as e:  # noqa: BLE001
        log(f"semantic match failed ({e}); falling back to random-window planning")
        return step_plan_broll_sequence(
            broll_folder, vo_duration, seed=seed,
            exclude_talking_heads=exclude_talking_heads,
            exclude_paths=exclude_paths,
            talking_head_mean_threshold_db=talking_head_mean_threshold_db,
        )

    # Optional free-stock gap-fill for low-confidence lines, then one re-match
    # (cache makes existing clips instant; only new stock clips embed).
    weak = [e for e in edl if e.get("needs_fallback")]
    if fallback == "stock" and weak:
        fetch = Path(__file__).resolve().parent / "broll_fetch.py"
        fetched = 0
        for e in weak:
            query = " ".join((e.get("text") or "").split()[:6]) or "lifestyle b-roll"
            r = subprocess.run(
                [sys.executable, str(fetch), "--query", query,
                 "--orientation", orientation, "--dur", f"{seg_len:.1f}",
                 "--output", str(broll_folder), "--count", "1"],
                capture_output=True, text=True)
            if r.returncode == 0:
                fetched += 1
            else:
                log(f"stock fetch failed for '{query}': {r.stderr.strip()[-160:]}")
        if fetched:
            log(f"fetched {fetched} stock clip(s) for weak lines; re-matching")
            try:
                edl = _run_broll_match(broll_folder, segs, min_score)
            except Exception as e:  # noqa: BLE001
                log(f"re-match after fetch failed ({e}); using first-pass EDL")

    weak_after = sum(1 for e in edl if e.get("needs_fallback"))
    log(f"semantic: {len(edl)} segments matched, {weak_after} still low-confidence "
        f"(min_score={min_score})")

    plan: list[tuple[Path, float, float]] = []
    for e in edl:
        take = float(e["end"]) - float(e["start"])
        if take <= 0.05:
            continue
        s_in = float(e.get("source_in", 0.0))
        plan.append((Path(e["source"]), round(s_in, 3), round(s_in + take, 3)))
    # rejections stay empty (semantic mode does no talking-head filtering); the
    # low-confidence count is logged above.
    return plan, []


def step_render_broll_concat(
    plan: list[tuple[Path, float, float]],
    output: Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: str = "30/1",
) -> None:
    """Concat the planned clips to a silent video at target res/fps.

    Each clip gets trimmed → setpts=PTS-STARTPTS → scale-pad → setsar → fps
    → settb. The same fps-normalize-then-concat pattern broll_overlay uses
    after the 2026-05-13 desync fix. Audio is dropped entirely — the VO mux
    step adds audio in the next pass.
    """
    if not plan:
        raise SystemExit("empty b-roll plan")
    output.parent.mkdir(parents=True, exist_ok=True)

    inputs: list[str] = []
    parts: list[str] = []
    labels: list[str] = []
    norm = f"fps={fps},settb=AVTB"
    for i, (clip, t_in, t_out) in enumerate(plan):
        inputs.extend(["-i", str(clip)])
        lbl = f"v{i}"
        parts.append(
            f"[{i}:v]trim=start={t_in:.4f}:end={t_out:.4f},"
            f"setpts=PTS-STARTPTS,"
            f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,{norm}[{lbl}]"
        )
        labels.append(lbl)
    concat = "".join(f"[{l}]" for l in labels)
    parts.append(f"{concat}concat=n={len(labels)}:v=1:a=0[outv]")
    fc = ";".join(parts)

    # Write the filter graph to a file and pass it via -filter_complex_script
    # instead of inline. With the random-window planner producing ~18-20
    # segments AND long Windows asset paths, the inline command line blew the
    # ~32K Windows limit (WinError 206 → a variant silently failing to render,
    # 2026-07-07). A script file keeps the command line short regardless of
    # segment count.
    fc_file = output.parent / f"{output.stem}_concat_filter.txt"
    fc_file.write_text(fc, encoding="utf-8")

    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        *inputs,
        "-filter_complex_script", str(fc_file),
        "-map", "[outv]",
        "-vsync", "cfr", "-r", fps,
        *_encoder_args(),
        "-an",
        "-movflags", "+faststart",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        fc_file.unlink()
    except OSError:
        pass
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit(f"b-roll concat failed (exit {proc.returncode})")


# --------------------------------------------------------------------------
# Step 5: Mux VO onto silent b-roll
# --------------------------------------------------------------------------

def step_mux_vo(broll_silent: Path, vo_audio: Path, output: Path) -> None:
    """Stream-copy the b-roll video + encode the VO audio. Output length is
    the video's — the planner already sized the b-roll to the VO."""
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-i", str(broll_silent),
        "-i", str(vo_audio),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit(f"VO mux failed (exit {proc.returncode})")


# --------------------------------------------------------------------------
# Step 6-7: Build + burn captions from word timestamps
# --------------------------------------------------------------------------

def _ts_srt(s: float) -> str:
    h = int(s // 3600); m = int((s % 3600) // 60); sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}".replace(".", ",")


def _ts_ass(s: float) -> str:
    h = int(s // 3600); m = int((s % 3600) // 60); sec = s % 60
    return f"{h:01d}:{m:02d}:{sec:05.2f}"


def _hex_to_ass_bgr(hex_str: str, alpha: int = 0) -> str:
    """Convert #RRGGBB → ASS &HAABBGGRR (alpha 0 = opaque, 255 = transparent)."""
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _apply_case(text: str, case: str) -> str:
    """Transform caption text per the Captions tab's caseStyle setting.

    'natural' is identity. 'upper' uppercases. 'lower' lowercases. Any other
    value falls back to natural so a typo in the prompt body doesn't blow up
    the whole render.
    """
    c = (case or "natural").lower()
    if c == "upper": return text.upper()
    if c == "lower": return text.lower()
    return text


def step_build_captions(
    transcript: dict,
    *,
    out_srt: Path,
    out_ass: Path,
    max_chars: int = 20,
    min_duration: float = 1.5,
    tail_pad: float = 0.25,
    font: str = "Arial",
    font_size: int = 42,
    bg_color: str = "#FFDE59",
    fg_color: str = "#1A0F40",
    play_res_x: int = 1080,
    play_res_y: int = 1920,
    alignment: int = 2,
    margin_v: int = 540,
    case: str = "natural",        # NEW — from Captions tab caseStyle
    gap_frames: int = 0,           # NEW — from Captions tab gapFrames (24fps)
    shadow: bool = True,           # NEW — from Captions tab shadow toggle
    max_lines: int = 1,            # NEW — from Captions tab layout (single/double)
) -> int:
    """Chunk transcript words into caption blocks and write SRT + ASS.

    Honors every visual setting from the Captions tab so variants match what
    the user picked there:
      - `case` rewrites cue text (natural/upper/lower).
      - `gap_frames` subtracts gap_frames/24s from each cue's end so the next
        cue starts visibly later. 0 = back-to-back.
      - `shadow` toggles the drop shadow in the ASS style.
      - `max_lines` allows wrapping into 2 lines (still capped by max_chars
        per line).
    Returns the chunk count.
    """
    words = [w for w in (transcript.get("words") or []) if w.get("type") == "word"]
    # Effective per-block character budget = max_chars × max_lines so a
    # double-line layout can hold roughly twice the text.
    block_chars = max(1, max_chars * max(1, max_lines))
    # Chunk: greedy pack up to block_chars per cue (word boundaries).
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for w in words:
        tok = (w.get("text") or "").strip()
        if not tok:
            continue
        extra = (1 if cur else 0) + len(tok)
        if cur_chars + extra > block_chars and cur:
            chunks.append(cur)
            cur, cur_chars = [w], len(tok)
        else:
            cur.append(w); cur_chars += extra
    if cur: chunks.append(cur)

    if not chunks:
        out_srt.write_text("", encoding="utf-8")
        return 0

    out_srt.parent.mkdir(parents=True, exist_ok=True)
    srt_lines: list[str] = []
    cue_times: list[tuple[float, float, str]] = []
    gap_s = max(0, int(gap_frames)) / 24.0  # captions tab assumes 24fps for gap math
    for i, ch in enumerate(chunks, start=1):
        start = float(ch[0]["start"])
        end = max(float(ch[-1]["end"]) + tail_pad, start + min_duration)
        if i < len(chunks):
            next_start = float(chunks[i][0]["start"])
            end = min(end, max(next_start - gap_s, start + 0.5))
        # Build the displayed text — apply case + wrap if max_lines > 1.
        raw = " ".join((w.get("text") or "").strip() for w in ch).strip()
        raw = _apply_case(raw, case)
        # If we asked for a 2-line layout, do a single word-boundary split
        # near the middle so ASS WrapStyle=2 + max_chars stays predictable.
        text = raw
        if max_lines >= 2 and len(raw) > max_chars:
            toks = raw.split(" ")
            mid = max(1, len(toks) // 2)
            text = " ".join(toks[:mid]) + r"\N" + " ".join(toks[mid:])
        cue_times.append((start, end, text))
        # SRT can't render the ASS \N — strip back to space for the .srt sidecar.
        srt_lines += [str(i), f"{_ts_srt(start)} --> {_ts_srt(end)}", text.replace(r"\N", " "), ""]
    out_srt.write_text("\n".join(srt_lines), encoding="utf-8")

    primary = _hex_to_ass_bgr(fg_color)
    outline = _hex_to_ass_bgr(bg_color)  # BorderStyle=3 uses OutlineColour as the box bg
    back    = "&H8C000000"               # semi-transparent black shadow color
    # Shadow=0 disables the drop shadow entirely (BackColour ignored). Shadow=4
    # is the default subtle drop. Captions tab's `shadow` checkbox flips this.
    shadow_amt = 4 if shadow else 0
    style_line = (
        f"Style: Psyglow,{font},{font_size},{primary},&H00000000,"
        f"{outline},{back},1,0,0,0,100,100,0,0,3,12,{shadow_amt},{alignment},40,40,{margin_v},1"
    )

    ass_parts = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
         "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
         "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
         "Alignment, MarginL, MarginR, MarginV, Encoding"),
        style_line,
        "",
        "[Events]",
        ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
         "MarginV, Effect, Text"),
    ]
    for start, end, text in cue_times:
        ass_parts.append(
            f"Dialogue: 0,{_ts_ass(start)},{_ts_ass(end)},Psyglow,,0,0,0,,{text}"
        )
    out_ass.parent.mkdir(parents=True, exist_ok=True)
    out_ass.write_text("\n".join(ass_parts) + "\n", encoding="utf-8")
    return len(chunks)


def _hook_font_source() -> Path | None:
    """Locate a bold sans font on the system. Returns the source Path (or None).
    We copy this into the caption-burn cwd and reference it by relative name,
    because ffmpeg's argv-level filter parser doesn't reliably honor `\\:`
    escaping of Windows drive-letter colons in `fontfile=` on Windows."""
    for name in ("arialbd.ttf", "arial.ttf"):
        p = Path(r"C:\Windows\Fonts") / name
        if p.exists():
            return p
    return None


def step_burn_captions(
    input_mp4: Path, ass_path: Path, output: Path,
    hook_text: str | None = None, frame_height: int = 1920,
    frame_width: int = 1080, hook_font: str | None = None,
    cta_text: str | None = None, cta_font: str | None = None,
    cta_bg: str | None = None, cta_fg: str | None = None,
    cta_mode: str = "full", video_duration: float = 0.0,
    disclaimer_text: str | None = None,
) -> None:
    """Burn captions via the libass subtitles filter. Uses cwd-relative file
    name to sidestep the colon-in-Windows-path escape pain.

    When `hook_text` is given, a static scroll-stopper overlay is chained into
    the SAME encode (no extra pass) at the TOP of the frame. Placement honors
    the Meta ad safe zone: Instagram/Facebook UI covers the top 14% of a 9:16
    reel, so the hook lands at y≈300px on a 1920-tall frame (scaled for others),
    visible for the first 3 seconds. Captions stay center-frame (set via ASS)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    cwd = ass_path.parent
    vf = f"subtitles={ass_path.name}"

    def _fit_lines(text: str, base_size: int, min_size: int = 40) -> tuple[int, list[str]]:
        """KEEP OVERLAY TEXT IN FRAME. Neither drawtext nor libass shrinks to
        fit, so wrap to the usable width (~86% of the frame — a Meta-safe
        horizontal margin) and shrink the font if a single word is still too
        wide for one line. Returns (font_size, wrapped_lines)."""
        raw = " ".join(text.strip().split())
        usable_w = frame_width * 0.86
        size = base_size
        def cpl(fs: int) -> int:
            # bold sans average glyph advance ≈ 0.56 × fontsize (px)
            return max(6, int(usable_w / (0.56 * fs)))
        longest_word = max((len(w) for w in raw.split()), default=1)
        while size > min_size and longest_word > cpl(size):
            size -= 4
        return size, (textwrap.wrap(raw, width=cpl(size)) or [raw])

    def _overlay_ass(style_name: str, font: str, size: int, margin_v: int,
                     start_s: float, end_s: float, lines: list[str],
                     alignment: int = 8, primary: str = "&H00FFFFFF",
                     box: str = "&H00000000", bold: int = 1,
                     outline_w: int = 20) -> str:
        """One boxed overlay as a standalone ASS doc. libass (not drawtext)
        because it handles multi-line + per-line centering + boxing cleanly;
        drawtext+textfile renders a tofu box at every newline on this ffmpeg
        build. Box via BorderStyle=3 (OutlineColour = box fill).

        alignment: 8=top-center (hook/CTA), 2=bottom-center (disclaimer).
        primary/box are ASS &HAABBGGRR colors (alpha: 00=opaque, higher=trans).
        margin_v is measured from the top edge for Alignment 8, from the bottom
        edge for Alignment 2."""
        side_margin = round(frame_width * 0.07)
        body = r"\N".join(lines)
        return "\n".join([
            "[Script Info]", "ScriptType: v4.00+",
            f"PlayResX: {frame_width}", f"PlayResY: {frame_height}",
            "ScaledBorderAndShadow: yes", "WrapStyle: 2", "",
            "[V4+ Styles]",
            ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
             "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
             "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
             "Alignment, MarginL, MarginR, MarginV, Encoding"),
            (f"Style: {style_name},{font},{size},{primary},{primary},{box},"
             f"{box},{bold},0,0,0,100,100,0,0,3,{outline_w},0,{alignment},"
             f"{side_margin},{side_margin},{margin_v},1"),
            "", "[Events]",
            ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
             "MarginV, Effect, Text"),
            f"Dialogue: 0,{_ts_ass(start_s)},{_ts_ass(end_s)},{style_name},,0,0,0,,{body}",
        ]) + "\n"

    if hook_text and hook_text.strip():
        # Hook: top of frame inside the Meta safe zone (IG/FB UI covers the
        # top 14% of a 9:16 reel → land at y≈300px on 1920, scaled), first 3s.
        hook_y = max(60, round(frame_height * 300 / 1920))
        hook_size, hook_lines = _fit_lines(hook_text, max(40, round(frame_height * 72 / 1920)))
        (cwd / "hook.ass").write_bytes(
            _overlay_ass("Hook", hook_font or "Arial Black", hook_size, hook_y, 0.0, 3.0, hook_lines).encode("utf-8"))
        vf = f"{vf},subtitles=hook.ass"

    if cta_text and cta_text.strip():
        # CTA: ~200px below the center-aligned captions (captions sit at true
        # center = frame_height/2). Top-center alignment with MarginV putting
        # the CTA's top edge at center + 200px (scaled). Clamped so the block
        # stays above the Meta bottom-35% UI zone even when it wraps.
        cta_size, cta_lines = _fit_lines(cta_text, max(36, round(frame_height * 56 / 1920)), min_size=36)
        cta_top = round(frame_height / 2 + frame_height * 200 / 1920)
        est_block_h = len(cta_lines) * round(cta_size * 1.3) + 40  # lines + box padding
        max_top = round(frame_height * 0.65) - est_block_h         # bottom 35% = Meta UI zone
        cta_top = max(round(frame_height / 2 + 60), min(cta_top, max_top))
        # Timing: whole video, or only the last 30%.
        dur = max(0.5, float(video_duration or 0.0))
        cta_start = 0.0 if (cta_mode or "full") == "full" else round(dur * 0.70, 2)
        (cwd / "cta.ass").write_bytes(
            _overlay_ass("CTA", cta_font or "Arial Black", cta_size, cta_top,
                         cta_start, dur, cta_lines,
                         primary=_hex_to_ass_bgr(cta_fg) if cta_fg else "&H00FFFFFF",
                         box=_hex_to_ass_bgr(cta_bg) if cta_bg else "&H00000000").encode("utf-8"))
        vf = f"{vf},subtitles=cta.ass"

    if disclaimer_text and disclaimer_text.strip():
        # Legal disclaimer / fine print: BOTTOM-centered, small 18pt Arial,
        # BLACK text on a near-opaque white bar so it's legible over any
        # footage (a legal notice that can't be read is worthless). Shown for
        # the FIRST 3 SECONDS only. Wrapped/shrunk to stay entirely in frame
        # like every other overlay. Sits ~40px off the bottom edge (scaled).
        disc_size = max(12, round(frame_height * 18 / 1920))
        _, disc_lines = _fit_lines(disclaimer_text, disc_size, min_size=disc_size)
        disc_margin = max(24, round(frame_height * 40 / 1920))
        disc_end = min(3.0, max(0.5, float(video_duration or 3.0)))
        (cwd / "disc.ass").write_bytes(
            _overlay_ass(
                "Disc", "Arial", disc_size, disc_margin, 0.0, disc_end, disc_lines,
                alignment=2,                 # bottom-center
                primary="&H00000000",        # black text
                box="&H1AFFFFFF",            # ~90% opaque white bar (alpha 0x1A)
                bold=0, outline_w=8,
            ).encode("utf-8"))
        vf = f"{vf},subtitles=disc.ass"
    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-i", str(input_mp4),
        "-vf", vf,
        *_encoder_args(),
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd))
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit(f"caption burn failed (exit {proc.returncode})")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run(args: argparse.Namespace) -> dict:
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    broll_folder = args.broll_folder.resolve()
    output = args.output.resolve()
    edit_dir = args.edit_dir.resolve()
    if not args.script_text or not args.script_text.strip():
        sys.exit("--script-text is empty")

    tmp_root = edit_dir / "variant_tmp" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    tmp_root.mkdir(parents=True, exist_ok=True)
    # VO filename MUST be unique per invocation — transcribe_one keys its
    # on-disk cache by stem. Borrow the tmp dir's microsecond timestamp.
    vo_mp3 = tmp_root / f"vo_{tmp_root.name}.mp3"
    broll_silent = tmp_root / "broll_silent.mp4"
    composite_pre_caps = tmp_root / "composite.mp4"
    srt_path = tmp_root / "captions.srt"
    ass_path = tmp_root / "captions.ass"

    api_key = load_11l_key()

    print(f"[1/6] TTS  → {vo_mp3.name}", file=sys.stderr)
    step_tts(
        script_text=args.script_text, voice_id=args.voice_id,
        out_path=vo_mp3, api_key=api_key,
        model=args.tts_model, stability=args.tts_stability,
        similarity=args.tts_similarity, style=args.tts_style,
        speaker_boost=not args.no_speaker_boost,
    )
    vo_duration = _probe_duration(vo_mp3)
    if vo_duration <= 0:
        sys.exit("VO duration probe returned 0 — TTS likely failed silently")
    print(f"      VO duration: {vo_duration:.2f}s", file=sys.stderr)

    print(f"[2/6] Transcribing VO for word timestamps", file=sys.stderr)
    transcript = step_transcribe_vo(vo_mp3, edit_dir)

    print(f"[3/6] Planning b-roll sequence from {broll_folder.name}/ "
          f"(match={args.match})", file=sys.stderr)
    if args.match == "semantic":
        plan, rejections = step_plan_broll_semantic(
            broll_folder=broll_folder, vo_duration=vo_duration,
            transcript=transcript, seed=args.seed,
            min_score=args.match_min_score, fallback=args.broll_fallback,
            orientation=("portrait" if args.height >= args.width else "landscape"),
            exclude_talking_heads=args.exclude_talking_heads,
            exclude_paths=[Path(p) for p in (args.exclude_paths or [])],
            talking_head_mean_threshold_db=args.talking_head_threshold,
            log=lambda m: print(f"      {m}", file=sys.stderr),
        )
    else:
        plan, rejections = step_plan_broll_sequence(
            broll_folder=broll_folder, vo_duration=vo_duration,
            seed=args.seed,
            exclude_talking_heads=args.exclude_talking_heads,
            exclude_paths=[Path(p) for p in (args.exclude_paths or [])],
            talking_head_mean_threshold_db=args.talking_head_threshold,
        )
    print(f"      {len(plan)} clip(s) selected, {len(rejections)} rejected by filters",
          file=sys.stderr)
    for r in rejections[:5]:
        print(f"        skip: {Path(r['source']).name} — {r['reason']}", file=sys.stderr)
    if len(rejections) > 5:
        print(f"        … and {len(rejections) - 5} more", file=sys.stderr)

    # Swap each selected clip for its cached 1080p proxy when one exists
    # (built by build_proxies / --build-proxies). Massively cuts decode load
    # so renders don't overload a VRAM-limited GPU. In/out times are unchanged
    # (proxy has the same duration as the source). If a proxy is missing, the
    # original 4K clip is used as-is (still works, just heavier).
    _root = broll_folder.resolve()
    plan = [(_source_for(c, _root), t_in, t_out) for (c, t_in, t_out) in plan]

    print(f"[4/6] Concatting b-roll → {broll_silent.name}", file=sys.stderr)
    step_render_broll_concat(
        plan=plan, output=broll_silent,
        width=args.width, height=args.height, fps=args.fps,
    )

    print(f"[5/6] Muxing VO over b-roll → {composite_pre_caps.name}", file=sys.stderr)
    step_mux_vo(broll_silent, vo_mp3, composite_pre_caps)

    print(f"[6/6] Building + burning captions → {output.name}", file=sys.stderr)
    chunk_count = step_build_captions(
        transcript=transcript,
        out_srt=srt_path, out_ass=ass_path,
        max_chars=args.caption_max_chars,
        min_duration=args.caption_min_duration,
        tail_pad=args.caption_tail_pad,
        font=args.caption_font,
        font_size=args.caption_size,
        bg_color=args.caption_bg, fg_color=args.caption_fg,
        play_res_x=args.width, play_res_y=args.height,
        alignment=args.caption_alignment,
        margin_v=args.caption_margin_v,
        case=args.caption_case,
        gap_frames=args.caption_gap_frames,
        shadow=args.caption_shadow,
        max_lines=args.caption_max_lines,
    )
    step_burn_captions(
        composite_pre_caps, ass_path, output,
        hook_text=getattr(args, "hook_text", None),
        frame_height=args.height,
        frame_width=args.width,
        hook_font=getattr(args, "hook_font", None),
        cta_text=getattr(args, "cta_text", None),
        cta_font=getattr(args, "cta_font", None),
        cta_bg=getattr(args, "cta_bg", None),
        cta_fg=getattr(args, "cta_fg", None),
        cta_mode=getattr(args, "cta_mode", "full"),
        video_duration=vo_duration,
        disclaimer_text=getattr(args, "disclaimer_text", None),
    )

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "output": str(output),
        "vo_duration_s": round(vo_duration, 3),
        "voice_id": args.voice_id,
        "broll_folder": str(broll_folder),
        "broll_match": args.match,
        "broll_clips": [
            {"source": str(p), "in": round(t_in, 3), "out": round(t_out, 3)}
            for p, t_in, t_out in plan
        ],
        "broll_rejections": rejections,
        "caption_chunks": chunk_count,
        "resolution": f"{args.width}x{args.height}",
        "fps": args.fps,
        "script_text": args.script_text,
        "tmp_dir": str(tmp_root) if args.keep_temps else None,
    }

    # Persist the assembly plan next to the deliverable so the studio's
    # timeline can show AND re-edit how this variant was put together.
    # SRT (caption track), VO and ASS (re-render inputs for reassemble.py)
    # are copied out of tmp before cleanup deletes them.
    try:
        if srt_path.exists():
            shutil.copy2(srt_path, output.with_suffix(".srt"))
        if vo_mp3.exists():
            vo_keep = output.with_suffix(".vo.mp3")
            shutil.copy2(vo_mp3, vo_keep)
            result["vo_file"] = str(vo_keep)
        if ass_path.exists():
            ass_keep = output.with_suffix(".ass")
            shutil.copy2(ass_path, ass_keep)
            result["ass_file"] = str(ass_keep)
        sidecar = output.parent / (output.name + ".timeline.json")
        sidecar.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"warning: could not write timeline sidecar: {e}", file=sys.stderr)

    if not args.keep_temps:
        # Best-effort cleanup of intermediates; final.mp4 lives outside tmp_root.
        # Includes the hook overlay's temp files (hook.txt + copied font), which
        # step_burn_captions writes into tmp_root — omitting them left the dir
        # non-empty so rmdir failed and every successful run littered a tmp dir.
        for p in (vo_mp3, broll_silent, composite_pre_caps, srt_path, ass_path,
                  tmp_root / "hook.ass", tmp_root / "cta.ass", tmp_root / "disc.ass",
                  tmp_root / "hook.txt", tmp_root / "hook_font.ttf"):
            try: p.unlink()
            except OSError: pass
        try: tmp_root.rmdir()
        except OSError: pass

    print(f"wrote: {output}", file=sys.stderr)
    if args.emit_json:
        print(json.dumps(result, indent=2))
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--broll-folder", type=Path, required=True,
                    help="Folder of b-roll clips (recurses one level; skips synced/).")
    ap.add_argument("--build-proxies", action="store_true",
                    help="Proxy-cache mode: downscale every >1080p clip in "
                         "--broll-folder to a cached 1080p proxy (sequential, "
                         "GPU-gentle) and exit. Run once before a batch so "
                         "renders work off light proxies. Ignores script/voice.")
    ap.add_argument("--script-text", default=None,
                    help="Finalized script for THIS variant (Claude rewrote upstream).")
    ap.add_argument("--output", type=Path, default=None, help="Final MP4 path.")
    ap.add_argument("--voice-id", default=None, help="ElevenLabs voice ID.")
    ap.add_argument("--edit-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent.parent / "videos" / "edit",
                    help="Dir for transcript cache + variant_tmp/.")

    # Video target
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--fps", default="30/1")
    ap.add_argument("--seed", type=int, default=None,
                    help="Seed for the b-roll shuffle. Pass per-variant for diversity.")

    # B-roll matching (2026-07-20)
    ap.add_argument("--match", choices=["random", "semantic"], default="random",
                    help="B-roll selection: 'random' (shuffled windows, fast, default) "
                         "or 'semantic' (local CLIP match to the VO script via "
                         "broll_match.py — script-relevant, $0).")
    ap.add_argument("--match-min-score", type=float, default=0.18,
                    help="semantic: below this a line is low-confidence / needs fallback.")
    ap.add_argument("--broll-fallback", choices=["none", "stock"], default="none",
                    help="semantic: 'stock' fetches free Pexels/Pixabay B-roll for "
                         "low-confidence lines, then re-matches. Default 'none'.")

    # B-roll filtering
    ap.add_argument("--exclude-talking-heads", action="store_true",
                    help="Skip b-roll clips that look like talking-head footage (sustained loud audio). "
                         "Heuristic: mean_volume > --talking-head-threshold AND duration > 3s. "
                         "Default OFF — pass this flag in the variants modal to enforce action-only.")
    ap.add_argument("--talking-head-threshold", type=float, default=-32.0,
                    help="dBFS mean_volume threshold for the talking-head heuristic. "
                         "Clips louder than this on average are flagged. Default -32.0; "
                         "raise (e.g. -28) to be stricter, lower (e.g. -35) to be looser.")
    ap.add_argument("--exclude-paths", nargs="*", default=[],
                    help="Explicit file or folder paths to skip. Folders match by prefix "
                         "(anything inside is excluded).")

    # TTS
    ap.add_argument("--tts-model", default="eleven_multilingual_v2")
    ap.add_argument("--tts-stability", type=float, default=0.5)
    ap.add_argument("--tts-similarity", type=float, default=0.75)
    ap.add_argument("--tts-style", type=float, default=0.0)
    ap.add_argument("--no-speaker-boost", action="store_true")

    # Captions
    ap.add_argument("--caption-font", default="Arial")
    ap.add_argument("--caption-size", type=int, default=42)
    ap.add_argument("--caption-bg", default="#FFDE59")
    ap.add_argument("--caption-fg", default="#1A0F40")
    ap.add_argument("--caption-max-chars", type=int, default=20)
    ap.add_argument("--caption-min-duration", type=float, default=1.5)
    ap.add_argument("--caption-tail-pad", type=float, default=0.25)
    ap.add_argument("--caption-alignment", type=int, default=2,
                    help="ASS Alignment: 2=bottom-center, 5=true-center, 8=top-center. Default 2.")
    ap.add_argument("--caption-margin-v", type=int, default=540,
                    help="ASS MarginV (distance from the alignment-anchored edge).")
    ap.add_argument("--caption-case", choices=("natural", "upper", "lower"),
                    default="natural",
                    help="Cue text case (mirrors Captions tab caseStyle).")
    ap.add_argument("--caption-gap-frames", type=int, default=0,
                    help="Gap between cues in 24fps frames (Captions tab gapFrames). 0=back-to-back.")
    ap.add_argument("--caption-shadow", dest="caption_shadow",
                    action="store_true", default=True,
                    help="Render the subtle drop shadow (default ON).")
    ap.add_argument("--no-caption-shadow", dest="caption_shadow",
                    action="store_false",
                    help="Disable the drop shadow (Captions tab shadow unchecked).")
    ap.add_argument("--hook-text", default=None,
                    help="Optional scroll-stopper overlay burned at the top of "
                         "the frame (first 3s), inside the Meta ad safe zone "
                         "(top 14%% reserved for IG/FB UI). Distinct per variant.")
    ap.add_argument("--hook-font", default="Arial Black",
                    help="Font family for the top hook overlay. Should be a "
                         "preset font that differs from the caption font (may "
                         "match the CTA font). Default: Arial Black.")
    ap.add_argument("--cta-text", default=None,
                    help="Optional on-screen CTA overlay ~200px below the "
                         "center captions. Auto-wrapped/shrunk to stay in frame "
                         "and clamped above the Meta bottom-35%% UI zone.")
    ap.add_argument("--cta-font", default="Arial Black",
                    help="Font family for the CTA overlay (should differ from "
                         "the caption font). Default: Arial Black.")
    ap.add_argument("--cta-bg", default=None,
                    help="CTA box color #RRGGBB (default black).")
    ap.add_argument("--cta-fg", default=None,
                    help="CTA text color #RRGGBB (default white).")
    ap.add_argument("--cta-mode", choices=("full", "last30"), default="full",
                    help="CTA visibility: 'full' = entire video, "
                         "'last30' = final 30%% of the video only.")
    ap.add_argument("--disclaimer-text", default=None,
                    help="Legal fine-print disclaimer, burned bottom-center, "
                         "18pt Arial black on a white bar, full duration. "
                         "Auto-wrapped to stay in frame. Multiple disclaimers "
                         "should be joined into one string (separate with '  ·  ').")
    ap.add_argument("--caption-max-lines", type=int, default=1, choices=(1, 2),
                    help="Lines per cue: 1=single (default), 2=double (Captions tab layout).")

    ap.add_argument("--keep-temps", action="store_true",
                    help="Keep intermediates in variant_tmp/<run> for inspection.")
    ap.add_argument("--json", dest="emit_json", action="store_true",
                    help="Print machine-readable result to stdout.")
    args = ap.parse_args()

    if args.build_proxies:
        summary = build_proxies(args.broll_folder, log=lambda m: print(m, file=sys.stderr))
        if args.emit_json:
            print(json.dumps(summary, indent=2))
        return

    # Normal render mode: these are required.
    missing = [n for n, v in (("--script-text", args.script_text),
                              ("--output", args.output),
                              ("--voice-id", args.voice_id)) if not v]
    if missing:
        ap.error("missing required args for render: " + ", ".join(missing))
    run(args)


if __name__ == "__main__":
    main()
