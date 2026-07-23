"""Sync a secondary audio recording to a video's on-board audio.

Use case: you recorded a clap-or-talk on a better mic alongside the camera. The
camera's on-board audio is rough but matches the picture; the external mic is
clean but starts at a different time. This helper finds the offset between
them by RMS-envelope cross-correlation (the same technique PluralEyes uses)
and optionally bakes a new MP4 with the offset applied + the original audio
muted.

Algorithm:
  1. Extract mono audio at 8 kHz from both inputs (low SR is plenty for
     envelope alignment, fast to compute).
  2. Compute short-window RMS envelopes — robust to EQ / noise / level
     differences between the two recordings.
  3. Cross-correlate the envelopes. Peak position = offset in frames →
     convert to seconds.
  4. Confidence = ratio of peak to second-best correlation. >2.0 is a
     confident sync; <1.3 means the two recordings probably don't match.

Usage:
    # Detect only, print offset + confidence:
    python helpers/sync_audio.py <video> <audio>

    # Detect AND build a synced MP4 (video + audio swap, original audio muted):
    python helpers/sync_audio.py <video> <audio> --apply --out <synced.mp4>

    # JSON output for programmatic consumption:
    python helpers/sync_audio.py <video> <audio> --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    import librosa
except ImportError:
    sys.exit("librosa is required: install via `uv sync` in video-use/")


def _extract_to_wav(src: Path, dst: Path, sr: int) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le",
        str(dst),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# ─── Filename / mediainfo timestamp extraction ────────────────────────────
# A bad pairing can sneak past peak/runner-up AND pearson when both signals
# are "talking interleaved with silence" — envelope coincidences are not rare.
# The strongest defense is asking: were these even recorded around the same
# time? Most files we deal with bake the recording time into their filename
# (`PXL_20260513_144150293…`, `DJI_NN_YYYYMMDD_HHMMSS`, `2026_05_13_10_50_57`),
# and `ffprobe` can read it for everything else. This catches the
# DJI-from-the-night-before-vs-video-today failure mode definitively.
import re as _re
_FILENAME_TS_PATTERNS = [
    # PXL_YYYYMMDD_HHMMSSmmm (Pixel)
    (_re.compile(r"PXL_(\d{8})_(\d{6})\d*", _re.IGNORECASE), "ymdhms"),
    # DJI_NN_YYYYMMDD_HHMMSS (DJI Mic recorder)
    (_re.compile(r"DJI_\d+_(\d{8})_(\d{6})", _re.IGNORECASE), "ymdhms"),
    # YYYY_MM_DD_HH_MM_SS (Canon-style underscores)
    (_re.compile(r"(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})"), "split6"),
    # Generic YYYYMMDD_HHMMSS
    (_re.compile(r"(\d{8})_(\d{6})"), "ymdhms"),
]


def _extract_recording_time(path: Path) -> tuple[datetime | None, str]:
    """Return (recording_time_utc, source_tag) for a media file.

    Tries filename patterns first (cheap, deterministic), then ffprobe's
    format_tags=creation_time (correct for iPhone/MOV, sometimes lying on
    Pixel/Canon depending on the workflow). Falls back to file mtime as a
    last resort (lowest trust — represents copy-time, not recording-time).
    Returns (None, "unknown") if everything fails.

    source_tag is one of: "filename", "ffprobe", "mtime", "unknown" — callers
    can decide whether to trust the timestamp based on its source.
    """
    name = path.name
    for pattern, kind in _FILENAME_TS_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        try:
            if kind == "split6":
                y, mo, d, h, mi, s = m.groups()
                return (datetime(int(y), int(mo), int(d), int(h), int(mi), int(s),
                                 tzinfo=timezone.utc), "filename")
            ymd, hms = m.groups()
            dt = datetime.strptime(f"{ymd}{hms}", "%Y%m%d%H%M%S")
            return (dt.replace(tzinfo=timezone.utc), "filename")
        except (ValueError, TypeError):
            continue

    # ffprobe creation_time
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format_tags=creation_time",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            check=False, capture_output=True, text=True,
        )
        out = (proc.stdout or "").strip()
        if out:
            # ISO 8601 with optional fractional seconds and trailing Z
            dt = datetime.fromisoformat(out.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt, "ffprobe")
    except Exception:
        pass

    # Last resort: filesystem mtime. Treat with suspicion — file copy will
    # bump this. Callers should NOT use this for the time-overlap gate
    # without an explicit opt-in.
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return (mtime, "mtime")
    except Exception:
        return (None, "unknown")


def extract_envelope(
    src: Path,
    sr: int = 8000,
    frame_length: int = 1024,
    hop_length: int = 256,
) -> dict:
    """Extract a z-normalized RMS envelope from a media file.

    Factored out of detect_offset so that batch tools (match_pairs.py) can
    extract each file ONCE and reuse the envelope across many comparisons.
    """
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        _extract_to_wav(src, wav, sr)
        x, _ = librosa.load(str(wav), sr=sr, mono=True)
    env = librosa.feature.rms(y=x, frame_length=frame_length, hop_length=hop_length)[0]
    s = env.std()
    env = (env - env.mean()) / (s if s > 1e-9 else 1.0)
    rec_time, rec_source = _extract_recording_time(src)
    return {
        "env": env,
        "sr": sr,
        "hop_length": hop_length,
        "n_frames": int(len(env)),
        "duration_s": float(len(x) / sr),
        "recording_time": rec_time,           # datetime | None
        "recording_time_source": rec_source,  # "filename" | "ffprobe" | "mtime" | "unknown"
        "path": str(src),
    }


def _pearson_at_lag(v: np.ndarray, a: np.ndarray, lag_frames: int) -> tuple[float, int]:
    """Return (pearson_coefficient, overlap_frames) for v and a aligned at `lag`.

    Computed on the overlapping slices only — uses local means/stds, which gives
    a proper Pearson correlation coefficient in [-1, 1] for the aligned regions.
    A real audio match scores 0.3–0.9; noise-against-noise scores near 0.
    """
    if lag_frames >= 0:
        v_slice = v[: max(0, len(a) - lag_frames)]
        a_slice = a[lag_frames : lag_frames + len(v_slice)]
    else:
        v_slice = v[-lag_frames:]
        a_slice = a[: len(v_slice)]
    n = min(len(v_slice), len(a_slice))
    if n < 50:  # not enough overlap for a meaningful coefficient
        return 0.0, int(n)
    v_slice = v_slice[:n]
    a_slice = a_slice[:n]
    v_mean = float(v_slice.mean()); a_mean = float(a_slice.mean())
    v_std = float(v_slice.std());   a_std = float(a_slice.std())
    if v_std < 1e-9 or a_std < 1e-9:
        return 0.0, int(n)
    cov = float(((v_slice - v_mean) * (a_slice - a_mean)).mean())
    return cov / (v_std * a_std), int(n)


def correlate_envelopes(
    env_v: dict,
    env_a: dict,
    max_offset_s: float | None = None,
) -> dict:
    """Cross-correlate two pre-extracted envelopes.

    Returns offset, two confidence metrics, and the source durations.

    Confidence metrics:
      - `confidence` (peak / runner-up ratio): how dominant the peak is in the
        cross-correlation. Catches "is there a single clear lag." Threshold-y
        but easily fooled by random noise — pure noise often has a 1.5–2.0
        ratio just by chance, which is why we also compute…
      - `pearson_confidence`: Pearson correlation coefficient of the aligned
        envelope slices at the detected lag. A proper [-1, 1] statistical
        measure of "do the two signals actually look alike." Real audio
        matches: 0.3–0.9. Noise: ~0. THIS is the metric that catches
        DJI-doesn't-match-video failure modes.

    Also surfaces `video_duration_s` and `audio_duration_s` so callers can
    enforce the physical constraint that |offset| < min(durations) + slack.

    `offset_seconds` is how far the audio (env_a) is delayed relative to the
    video (env_v). Positive = audio starts AFTER video.
    """
    sr = env_v["sr"]
    hop_length = env_v["hop_length"]
    v = env_v["env"]
    a = env_a["env"]
    if not len(v) or not len(a):
        return {
            "offset_seconds": 0.0,
            "confidence": 0.0,
            "pearson_confidence": 0.0,
            "overlap_frames": 0,
            "peak_idx": 0,
            "n_frames": int(len(v)),
            "video_duration_s": float(env_v.get("duration_s", 0.0)),
            "audio_duration_s": float(env_a.get("duration_s", 0.0)),
            "sr": sr,
            "hop_length": hop_length,
        }
    corr = np.correlate(a, v, mode="full")
    lag_frames = int(np.argmax(corr) - (len(v) - 1))
    offset_seconds = lag_frames * hop_length / float(sr)

    if max_offset_s is not None and abs(offset_seconds) > max_offset_s:
        max_lag = int(max_offset_s * sr / hop_length)
        center = len(v) - 1
        lo, hi = max(0, center - max_lag), min(len(corr), center + max_lag + 1)
        local_peak = int(np.argmax(corr[lo:hi])) + lo
        lag_frames = local_peak - center
        offset_seconds = lag_frames * hop_length / float(sr)

    peak_val = float(corr[lag_frames + len(v) - 1])
    masked = corr.copy()
    pk_idx = lag_frames + len(v) - 1
    masked[max(0, pk_idx - 50): min(len(masked), pk_idx + 50)] = -np.inf
    runner_up_val = float(np.max(masked)) if np.isfinite(np.max(masked)) else 1.0
    confidence = peak_val / max(runner_up_val, 1e-9)

    pearson, overlap_n = _pearson_at_lag(v, a, lag_frames)

    # Recording-time gap (None if either side lacks a reliable timestamp).
    # Caller decides whether to gate on this — it depends on the source tag.
    v_time = env_v.get("recording_time")
    a_time = env_a.get("recording_time")
    if v_time is not None and a_time is not None:
        rec_gap_s = abs((v_time - a_time).total_seconds())
    else:
        rec_gap_s = None

    return {
        "offset_seconds": round(offset_seconds, 4),
        "confidence": round(confidence, 3),
        "pearson_confidence": round(pearson, 3),
        "overlap_frames": int(overlap_n),
        "peak_idx": pk_idx,
        "n_frames": len(v),
        "video_duration_s": round(float(env_v.get("duration_s", 0.0)), 3),
        "audio_duration_s": round(float(env_a.get("duration_s", 0.0)), 3),
        "video_recording_time": v_time.isoformat() if v_time else None,
        "audio_recording_time": a_time.isoformat() if a_time else None,
        "video_time_source": env_v.get("recording_time_source", "unknown"),
        "audio_time_source": env_a.get("recording_time_source", "unknown"),
        "recording_gap_seconds": round(rec_gap_s, 1) if rec_gap_s is not None else None,
        "sr": sr,
        "hop_length": hop_length,
    }


def detect_offset(
    video_path: Path,
    audio_path: Path,
    sr: int = 8000,
    frame_length: int = 1024,
    hop_length: int = 256,
    max_offset_s: float | None = None,
) -> dict:
    """Single-pair convenience wrapper. Extracts both envelopes then correlates."""
    env_v = extract_envelope(video_path, sr, frame_length, hop_length)
    env_a = extract_envelope(audio_path, sr, frame_length, hop_length)
    return correlate_envelopes(env_v, env_a, max_offset_s=max_offset_s)


def _video_codec_args() -> list[str]:
    """Pick NVENC if available (high-quality intermediate), fall back to
    libx264 medium otherwise. Imported from render.py if possible.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from render import select_encoder  # type: ignore
        enc = select_encoder()
    except Exception:
        enc = "x264"
    if enc.startswith("nvenc"):
        # p6 + cq 19 — quality-leaning preset, basically transparent at 1080p.
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p6",
            "-rc", "vbr", "-cq", "19", "-b:v", "0",
            "-tune", "hq", "-spatial_aq", "1",
        ]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


def _probe_fps(video: Path) -> str:
    """Return the source's r_frame_rate as 'NUM/DEN' for use with -r."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of",
             "default=noprint_wrappers=1:nokey=1", str(video)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, text=True,
        )
        rate = (out.stdout or "30000/1001").strip()
        return rate or "30000/1001"
    except Exception:
        return "30000/1001"


def _probe_audio_peak_dbfs(media: Path) -> float:
    """Return the source's max sample peak in dBFS via ffmpeg's volumedetect.
    Returns -85.0 (silence floor) if the value can't be parsed.
    """
    import re as _re
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(media),
         "-af", "volumedetect", "-vn", "-sn", "-dn", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = _re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr)
    return float(m.group(1)) if m else -85.0


def apply_sync(
    video_path: Path,
    audio_path: Path,
    out_path: Path,
    offset_seconds: float,
    trim_head: bool = True,
    audio_continuous: bool = False,
    target_peak_min_db: float | None = None,
    target_peak_max_db: float | None = None,
    match_confidence: float | None = None,
    match_pearson: float | None = None,
    audio_duration_s: float | None = None,
    video_duration_s: float | None = None,
) -> dict:
    """Build a new MP4 with audio synced and the original audio dropped.

    Critically: this version RE-ENCODES the video with constant framerate
    (CFR) — `-c:v copy` preserved the Canon's variable / long-GOP H.264
    structure and caused freeze frames in downstream NVENC encodes. The
    NVENC re-encode here costs ~1 minute on RTX-class hardware but
    eliminates the issue at the source.

    `trim_head` (default True): when |offset| > ~0.5s, automatically
    chop the head of whichever stream started earlier so the output
    begins at content-start with audio at t=0. The default behavior is
    what you want for ad work — the silent-head dead air is gone, and
    downstream tools (transcribe.py, SRT-builder, etc.) don't need to
    know about any offset.

    `audio_continuous` (default False): set this to True for dual-system
    shoots where the external audio recorder rolled continuously while
    the camera was started/stopped per take. In that mode the audio is
    the longer source and the video is a sub-segment of it; the offset
    tells us where in the audio the video's content begins, so we trim
    the AUDIO head (not the video head) when offset > 0. Without this
    flag, an offset > video_duration would try to skip past the end of
    the video and produce empty output.

    Returns a dict describing what was done; also appends one record to
    videos/edit/sync_log.jsonl (shared audit log, no per-file sidecars).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fps = _probe_fps(video_path)
    codec_args = _video_codec_args()

    # ─── Idempotency guard (added 2026-05-13 after the parallel-encode race) ──
    # If a previous successful sync already produced this exact output for
    # this video/audio/offset combination, skip the re-encode. This prevents:
    #   - Wasted GPU time on redundant work.
    #   - Race conditions when the studio kills+restarts the agent mid-run
    #     (orphan ffmpegs would otherwise collide with the retry on the same
    #     output path, corrupting the mux).
    #
    # Skips when ALL of the following are true:
    #   1. The output MP4 exists on disk.
    #   2. The most recent sync_log entry for that output path matches the
    #      same source_video, source_audio, offset (±10 ms), and gain target.
    #   3. That entry's output_in_target_window is True (or audio leveling
    #      was disabled for that run).
    #   4. The output's peak still falls in the target window when re-probed.
    if _existing_sync_is_current(
        out_path,
        video_path=video_path,
        audio_path=audio_path,
        offset_seconds=offset_seconds,
        target_peak_min_db=target_peak_min_db,
        target_peak_max_db=target_peak_max_db,
    ):
        print(f"  [skip] {out_path.name} already synced & in-window — reusing existing",
              file=sys.stderr, flush=True)
        existing_peak = _probe_audio_peak_dbfs(out_path)
        meta = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "output": str(out_path),
            "source_video": str(video_path),
            "source_audio": str(audio_path),
            "detected_offset_seconds": round(offset_seconds, 4),
            "trim_head_applied": bool(trim_head),
            "audio_continuous": bool(audio_continuous),
            "fps": fps,
            "encoder": "reused",
            "audio_leveled": target_peak_min_db is not None and target_peak_max_db is not None,
            "output_audio_peak_dbfs": round(existing_peak, 2),
            "output_in_target_window": (
                (target_peak_min_db is not None and target_peak_max_db is not None)
                and (min(target_peak_min_db, target_peak_max_db) <= existing_peak
                     <= max(target_peak_min_db, target_peak_max_db))
            ) if (target_peak_min_db is not None and target_peak_max_db is not None) else None,
            "match_confidence": match_confidence,
            "match_pearson": match_pearson,
            "audio_duration_s": audio_duration_s,
            "video_duration_s": video_duration_s,
            "skipped_reused_existing": True,
        }
        _append_sync_log(meta)
        return meta

    # Atomic-write target (added 2026-05-13). ffmpeg writes to this PID-suffixed
    # temp path first; on success we os.replace() to the final out_path. This
    # ensures partial encodes never appear at out_path, and prevents two
    # concurrent sync processes from corrupting each other's mux output (the
    # bug that bit us on the 2026-05-13 RbA Street Shoot batch).
    tmp_out = out_path.with_name(f"._sync_tmp_{os.getpid()}_{out_path.name}")

    # The trim threshold — under 500 ms it's not worth chopping content.
    do_trim = bool(trim_head) and abs(offset_seconds) >= 0.5
    trimmed_video_head = 0.0
    trimmed_audio_head = 0.0

    # Audio leveling — peak-normalize the external audio so the output's
    # dialogue lands inside [target_peak_min_db, target_peak_max_db]. Both
    # bounds must be supplied to enable; otherwise pass-through.
    audio_leveled = False
    src_peak_db: float | None = None
    gain_db: float = 0.0
    if target_peak_min_db is not None and target_peak_max_db is not None:
        lo, hi = sorted([float(target_peak_min_db), float(target_peak_max_db)])
        src_peak_db = _probe_audio_peak_dbfs(audio_path)
        target_mid = (lo + hi) / 2.0
        gain_db = target_mid - src_peak_db
        audio_leveled = True
    audio_filter_args: list[str] = (["-af", f"volume={gain_db:.3f}dB"]
                                    if audio_leveled else [])

    if do_trim and audio_continuous and offset_seconds > 0:
        # Continuous-mic case: audio rolled before video. Skip into the audio
        # to land on the segment where the video's content begins.
        trimmed_audio_head = offset_seconds
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-ss", f"{offset_seconds:.4f}", "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            *codec_args,
            "-pix_fmt", "yuv420p",
            "-r", fps, "-vsync", "cfr",
            *audio_filter_args, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart",
            str(tmp_out),
        ]
    elif do_trim and audio_continuous and offset_seconds < 0:
        # Continuous-mic case but video started before audio (rare). Skip
        # ahead in the video.
        trimmed_video_head = abs(offset_seconds)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{abs(offset_seconds):.4f}", "-i", str(video_path),
            "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            *codec_args,
            "-pix_fmt", "yuv420p",
            "-r", fps, "-vsync", "cfr",
            *audio_filter_args, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart",
            str(tmp_out),
        ]
    elif do_trim and offset_seconds > 0:
        # Default: audio started later than video → skip ahead in the video.
        trimmed_video_head = offset_seconds
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{offset_seconds:.4f}", "-i", str(video_path),
            "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            *codec_args,
            "-pix_fmt", "yuv420p",
            "-r", fps, "-vsync", "cfr",
            *audio_filter_args, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart",
            str(tmp_out),
        ]
    elif do_trim and offset_seconds < 0:
        # Default: audio started earlier → seek into the audio.
        trimmed_audio_head = abs(offset_seconds)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-ss", f"{abs(offset_seconds):.4f}", "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            *codec_args,
            "-pix_fmt", "yuv420p",
            "-r", fps, "-vsync", "cfr",
            *audio_filter_args, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart",
            str(tmp_out),
        ]
    else:
        # No trim — preserve dead air. Audio is offset within the video timeline.
        if offset_seconds >= 0:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-itsoffset", f"{offset_seconds:.4f}", "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0",
                *codec_args,
                "-pix_fmt", "yuv420p",
                "-r", fps, "-vsync", "cfr",
                *audio_filter_args, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-shortest", "-movflags", "+faststart",
                str(tmp_out),
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-itsoffset", f"{abs(offset_seconds):.4f}", "-i", str(video_path),
                "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0",
                *codec_args,
                "-pix_fmt", "yuv420p",
                "-r", fps, "-vsync", "cfr",
                *audio_filter_args, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-shortest", "-movflags", "+faststart",
                str(tmp_out),
            ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception:
        # Clean up the partial temp file so it doesn't pile up as orphaned junk.
        try:
            if tmp_out.exists(): tmp_out.unlink()
        except Exception:
            pass
        raise

    # Atomic rename — the encode succeeded into tmp_out, now publish to the
    # final path. os.replace() is atomic on POSIX and near-atomic on Windows
    # (NTFS rename-with-replace at the directory entry level). If two
    # concurrent encodes both wrote temp files and rename, the last one wins
    # — never a partially-written file at out_path.
    os.replace(tmp_out, out_path)

    # Post-encode verification of the audio peak. Always probe (cheap) so the
    # log records what actually shipped — not just what we asked for.
    out_peak_db = _probe_audio_peak_dbfs(out_path)

    meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "output": str(out_path),
        "source_video": str(video_path),
        "source_audio": str(audio_path),
        "detected_offset_seconds": round(offset_seconds, 4),
        "trimmed_video_head_seconds": round(trimmed_video_head, 4),
        "trimmed_audio_head_seconds": round(trimmed_audio_head, 4),
        "trim_head_applied": do_trim,
        "audio_continuous": bool(audio_continuous),
        "output_starts_at_content": do_trim,
        "fps": fps,
        "encoder": codec_args[1] if len(codec_args) > 1 else "unknown",
        "audio_leveled": audio_leveled,
        "source_audio_peak_dbfs": (round(src_peak_db, 2) if src_peak_db is not None else None),
        "gain_applied_db": (round(gain_db, 2) if audio_leveled else 0.0),
        "target_peak_min_dbfs": (float(target_peak_min_db) if audio_leveled else None),
        "target_peak_max_dbfs": (float(target_peak_max_db) if audio_leveled else None),
        "output_audio_peak_dbfs": round(out_peak_db, 2),
        "output_in_target_window": (
            audio_leveled
            and (min(target_peak_min_db, target_peak_max_db) <= out_peak_db
                 <= max(target_peak_min_db, target_peak_max_db))
        ) if audio_leveled else None,
        "match_confidence": match_confidence,
        "match_pearson": match_pearson,
        "audio_duration_s": audio_duration_s,
        "video_duration_s": video_duration_s,
    }
    _append_sync_log(meta)
    return meta


def _append_sync_log(entry: dict) -> None:
    """Append one JSONL record to videos/edit/sync_log.jsonl (project-root log)."""
    log_path = Path(__file__).resolve().parent.parent.parent / "videos" / "edit" / "sync_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _existing_sync_is_current(
    out_path: Path,
    *,
    video_path: Path,
    audio_path: Path,
    offset_seconds: float,
    target_peak_min_db: float | None,
    target_peak_max_db: float | None,
) -> bool:
    """Return True if `out_path` is already the result of a successful sync
    for these exact inputs (video + audio + offset ±10 ms + same leveling
    targets), AND its current audio peak still lands inside the target window.

    The idempotency guard. Prevents:
      - Re-doing 1–3 min of NVENC work after an aborted-and-restarted job.
      - Two parallel match_pairs processes racing on the same output and
        producing a corrupt mux (the 2026-05-13 failure mode).
    """
    if not out_path.exists():
        return False

    log_path = Path(__file__).resolve().parent.parent.parent / "videos" / "edit" / "sync_log.jsonl"
    if not log_path.exists():
        return False

    # Find the most recent log entry for this output path. We read the file
    # backwards-ish (slurp + reverse scan) — it's small enough.
    target_out_str = str(out_path)
    matching: dict | None = None
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("output") == target_out_str:
                    matching = e   # keep overwriting so the LAST one wins
    except Exception:
        return False

    if matching is None:
        return False

    # Sources must match.
    if matching.get("source_video") != str(video_path): return False
    if matching.get("source_audio") != str(audio_path): return False

    # Offset must match within 10 ms.
    prev_off = matching.get("detected_offset_seconds")
    if prev_off is None or abs(float(prev_off) - float(offset_seconds)) > 0.01:
        return False

    # Leveling targets must match (both must be set the same way).
    prev_min = matching.get("target_peak_min_dbfs")
    prev_max = matching.get("target_peak_max_dbfs")
    if (target_peak_min_db is None) != (prev_min is None): return False
    if (target_peak_max_db is None) != (prev_max is None): return False
    if target_peak_min_db is not None:
        if abs(float(prev_min) - float(target_peak_min_db)) > 0.05: return False
        if abs(float(prev_max) - float(target_peak_max_db)) > 0.05: return False

    # If leveling was on, the previous log entry must have been in-window.
    if target_peak_min_db is not None and not matching.get("output_in_target_window"):
        return False

    # Final guard: re-probe the file on disk RIGHT NOW. If something corrupted
    # it between then and now (or someone manually replaced it), don't trust
    # the cached log entry.
    try:
        cur_peak = _probe_audio_peak_dbfs(out_path)
    except Exception:
        return False
    if target_peak_min_db is not None:
        lo, hi = sorted([float(target_peak_min_db), float(target_peak_max_db)])
        if not (lo <= cur_peak <= hi):
            return False
    elif cur_peak < -60.0:
        # Even without explicit leveling, anything below -60 dBFS is almost
        # certainly broken — force a re-encode.
        return False

    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path, help="Video file (uses its on-board audio for alignment)")
    ap.add_argument("audio", type=Path, help="External audio file to sync")
    ap.add_argument("--max-offset", type=float, default=120.0,
                    help="Clamp the search to ±N seconds. Default 120.")
    ap.add_argument("--apply", action="store_true",
                    help="After detecting, build a synced MP4 (video + new audio, original muted).")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output path for --apply. Default: <video_stem>_synced.mp4 next to source.")
    ap.add_argument("--no-trim-head", action="store_true",
                    help="By default the silent head (where one source had no content yet) is "
                         "trimmed so the output begins at content-start. Pass --no-trim-head to "
                         "preserve the dead air with audio offset within the video timeline.")
    ap.add_argument("--audio-continuous", action="store_true",
                    help="The external audio recorder rolled continuously while the camera was "
                         "started/stopped per take (dual-system shoot). In this mode the audio is "
                         "the longer source and the video is a sub-segment; trim the AUDIO head "
                         "(not the video head) when offset > 0. Use this when offset exceeds the "
                         "video duration.")
    ap.add_argument("--target-peak-db", nargs=2, type=float, metavar=("MIN", "MAX"),
                    default=None,
                    help="Peak-level the external audio so the output's max sample peak "
                         "lands at the midpoint of [MIN, MAX] dBFS. Linear gain — never "
                         "drops levels below the requested window. Verified post-encode "
                         "and recorded in sync_log.jsonl. Example: --target-peak-db -6 -3 "
                         "for dialogue, --target-peak-db -24 -20 for music bed.")
    ap.add_argument("--level-dialogue", action="store_true",
                    help="Shortcut for --target-peak-db -6 -3.")
    ap.add_argument("--level-music", action="store_true",
                    help="Shortcut for --target-peak-db -24 -20.")
    ap.add_argument("--json", action="store_true",
                    help="Print result as JSON.")
    args = ap.parse_args()

    # Resolve the dialogue/music shortcuts.
    if args.level_dialogue and args.level_music:
        sys.exit("--level-dialogue and --level-music are mutually exclusive")
    if args.target_peak_db is None:
        if args.level_dialogue:
            args.target_peak_db = [-6.0, -3.0]
        elif args.level_music:
            args.target_peak_db = [-24.0, -20.0]

    if not args.video.exists():
        sys.exit(f"video not found: {args.video}")
    if not args.audio.exists():
        sys.exit(f"audio not found: {args.audio}")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")

    print(f"detecting offset between:\n  video: {args.video}\n  audio: {args.audio}",
          file=sys.stderr)
    result = detect_offset(args.video, args.audio, max_offset_s=args.max_offset)

    if result["confidence"] < 1.3:
        result["warning"] = (
            f"low confidence ({result['confidence']:.2f}). The two recordings "
            f"may not contain the same speech. Re-check or pick a longer overlap."
        )

    if args.apply:
        out = args.out or args.video.with_name(f"{args.video.stem}_synced.mp4")
        print(f"baking synced MP4 → {out}", file=sys.stderr)
        target_min, target_max = (args.target_peak_db or [None, None])
        sync_meta = apply_sync(
            args.video, args.audio, out,
            result["offset_seconds"],
            trim_head=not args.no_trim_head,
            audio_continuous=args.audio_continuous,
            target_peak_min_db=target_min,
            target_peak_max_db=target_max,
        )
        result["synced_path"] = str(out)
        result["sync_metadata"] = sync_meta

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"offset      : {result['offset_seconds']:+.4f}s")
        print(f"confidence  : {result['confidence']:.2f} "
              f"({'high' if result['confidence'] >= 2.0 else 'medium' if result['confidence'] >= 1.3 else 'LOW'})")
        if "synced_path" in result:
            print(f"synced file : {result['synced_path']}")
        if "warning" in result:
            print(f"WARNING     : {result['warning']}", file=sys.stderr)


if __name__ == "__main__":
    main()
