"""Auto-pair every video in a folder with its matching audio file.

For multi-take dual-system shoots: drag in N videos and M audio files with
unrelated filenames, run this script, get a confidence-ranked assignment of
which video pairs with which audio plus the detected offset.

Algorithm:
  1. Extract a z-normalized RMS envelope from each file ONCE (the expensive
     step; this is what the brute-force pairwise approach would re-do N*M
     times).
  2. Build the N*M confidence matrix by cross-correlating each pre-extracted
     pair of envelopes.
  3. Greedy-assign: pick the highest-confidence pair, lock it in, remove
     those two files from the pool, repeat. (Equivalent to Hungarian for
     this objective when the matrix is non-degenerate.)
  4. Reject any pair whose confidence is below the threshold (default 1.5)
     — those probably aren't a real match.
  5. With --apply, sync each accepted pair via sync_audio.apply_sync,
     producing <video_stem>_synced.mp4 alongside each source.

Usage:
    python helpers/match_pairs.py --folder videos/
    python helpers/match_pairs.py --videos videos/A.mp4 videos/B.mp4 \\
                                  --audios videos/take1.wav videos/take2.wav
    python helpers/match_pairs.py --folder videos/ --apply --json
    python helpers/match_pairs.py --folder videos/ --threshold 1.8
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Ensure the helpers/ directory is on sys.path so we can import sync_audio.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_audio import (  # noqa: E402
    extract_envelope,
    correlate_envelopes,
    apply_sync,
)


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".MP4", ".MOV"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
              ".WAV", ".MP3", ".M4A"}


def discover(folder: Path) -> tuple[list[Path], list[Path]]:
    """Return (videos, audios) found directly inside `folder` (non-recursive).

    Files inside the `synced/` subfolder are intentionally NOT discovered —
    those are this helper's own outputs, and treating them as fresh source
    videos on a re-run would produce `*_synced_synced.mp4` garbage and
    waste GPU time encoding outputs into outputs.

    Also explicitly skips legacy `*_synced.mp4` files that pre-existed at
    the folder root from earlier versions of this helper, plus the
    `._sync_tmp_*` partial-encode files the atomic-write path may leave
    behind if a sync crashed mid-encode.
    """
    videos, audios = [], []
    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        # Skip helper-produced artifacts at the root.
        if f.name.endswith("_synced.mp4"):
            continue
        if f.name.startswith("._sync_tmp_"):
            continue
        ext = f.suffix
        if ext.lower() in {e.lower() for e in VIDEO_EXTS}:
            videos.append(f)
        elif ext.lower() in {e.lower() for e in AUDIO_EXTS}:
            audios.append(f)
    return videos, audios


def build_matrix(
    videos: list[Path],
    audios: list[Path],
    max_offset_s: float = 600.0,
    progress: bool = True,
) -> list[list[dict]]:
    """Return a 2D list `matrix[v_idx][a_idx]` of correlation results."""
    if progress:
        print(f"extracting envelopes for {len(videos)} videos + {len(audios)} audios…",
              file=sys.stderr, flush=True)
    env_videos = []
    for i, v in enumerate(videos):
        if progress:
            print(f"  [{i+1}/{len(videos)}] {v.name}", file=sys.stderr, flush=True)
        env_videos.append(extract_envelope(v))
    env_audios = []
    for i, a in enumerate(audios):
        if progress:
            print(f"  [{i+1}/{len(audios)}] {a.name}", file=sys.stderr, flush=True)
        env_audios.append(extract_envelope(a))

    if progress:
        print(f"correlating {len(videos)}*{len(audios)} pairs…",
              file=sys.stderr, flush=True)
    matrix: list[list[dict]] = []
    for vi, ev in enumerate(env_videos):
        row: list[dict] = []
        for ai, ea in enumerate(env_audios):
            r = correlate_envelopes(ev, ea, max_offset_s=max_offset_s)
            row.append(r)
        matrix.append(row)
    return matrix


# Minimum genuine overlap required for a meaningful sync, in seconds. If
# |offset| pushes the alignment beyond `min(video, audio) - MIN_OVERLAP_S`,
# the pair is physically impossible regardless of how well the metrics scored.
MIN_OVERLAP_S = 5.0


def _check_pair(
    r: dict,
    threshold: float,
    min_pearson: float,
    min_overlap_fraction: float = 0.5,
    require_time_overlap: bool = False,
) -> tuple[bool, str]:
    """Decide whether a (video, audio) pair is a real match.

    Gates (ALL must pass):

      1. peak/runner_up ratio ≥ threshold (does any single lag dominate the
         correlation?).
      2. Pearson correlation at the chosen lag ≥ min_pearson (do the aligned
         envelopes actually look alike statistically?). Catches pure-noise
         pairs that sneak past gate 1.
      3. Chosen offset leaves ≥ MIN_OVERLAP_S of physical overlap between
         the recordings (catches "offset bigger than the audio file" cases).
      4. Overlap covers ≥ min_overlap_fraction of the shorter recording
         (default 0.5). Catches Pearson coefficients computed over
         silence-vs-silence tail sections — a high coefficient over 5% of
         the duration is statistically meaningless.
      5. (Opt-in via require_time_overlap) Recording times overlap when both
         sides have a trusted timestamp. Catches "DJI from a different day"
         IF your equipment clocks are reliable — OFF by default because in
         practice DJI Mic clocks are often hours off the cameras and we'd
         reject otherwise-good syncs.

    Returns (accepted, reject_reason). reject_reason is "" on accept.
    """
    conf = float(r.get("confidence", 0.0))
    pearson = float(r.get("pearson_confidence", 0.0))
    offset = float(r.get("offset_seconds", 0.0))
    v_dur = float(r.get("video_duration_s", 0.0))
    a_dur = float(r.get("audio_duration_s", 0.0))

    if conf < threshold:
        return False, f"peak/runner-up {conf:.2f} < {threshold:.2f}"
    if pearson < min_pearson:
        return False, f"pearson {pearson:.2f} < {min_pearson:.2f} (likely noise)"

    overlap = None
    if v_dur > 0 and a_dur > 0:
        if offset >= 0:
            overlap = min(v_dur, a_dur - offset)
        else:
            overlap = min(v_dur + offset, a_dur)
        if overlap < MIN_OVERLAP_S:
            return False, (f"offset {offset:+.2f}s leaves only {overlap:.1f}s overlap "
                           f"(video {v_dur:.1f}s, audio {a_dur:.1f}s)")
        # Overlap-fraction gate. The Pearson coefficient at a lag where the
        # aligned region covers only a small slice of the recordings can be
        # statistically meaningless — silence-tail-to-silence-tail matches
        # routinely score Pearson 0.8+. Require the overlap to cover most of
        # the shorter recording before trusting the metric.
        shorter = min(v_dur, a_dur)
        fraction = overlap / shorter if shorter > 0 else 0.0
        if fraction < min_overlap_fraction:
            return False, (f"overlap {overlap:.1f}s is only {fraction*100:.0f}% of "
                           f"the shorter recording ({shorter:.1f}s); "
                           f"need ≥ {min_overlap_fraction*100:.0f}%")

    if require_time_overlap:
        v_src = r.get("video_time_source", "unknown")
        a_src = r.get("audio_time_source", "unknown")
        rec_gap = r.get("recording_gap_seconds")
        if (v_src in ("filename", "ffprobe") and a_src in ("filename", "ffprobe")
                and rec_gap is not None):
            max_plausible = max(v_dur, a_dur) + 60.0
            if rec_gap > max_plausible:
                return False, (f"recording times don't overlap: gap {rec_gap/3600:.1f}h "
                               f"between {v_src} {a_src} timestamps, "
                               f"max plausible {max_plausible/60:.1f}min")
    return True, ""


def greedy_assign(
    videos: list[Path],
    audios: list[Path],
    matrix: list[list[dict]],
    threshold: float,
    audio_continuous: bool = False,
    min_pearson: float = 0.30,
    min_overlap_fraction: float = 0.5,
    require_time_overlap: bool = False,
) -> dict:
    """Assign each video to an audio file using a dual-gate (peak/runner-up +
    pearson) confidence check plus a duration-overlap sanity test.

    Default (audio_continuous=False): greedy by highest confidence with the
    constraint that each audio file is used at most once. Right for shoots
    where each take has its own dedicated audio recording.

    audio_continuous=True: a single recorder rolled continuously while the
    camera was started/stopped per take. Each video gets its single best-match
    audio above the gates — multiple videos can (and usually will) share one
    audio file at different offsets.

    The result also returns `rejected_pairs` — every (video, audio) pair we
    considered and rejected, with the specific reason — so batch callers can
    surface "why" to the user instead of silently dropping bad matches.
    """
    rejected: list[dict] = []

    def _build(vi: int, ai: int, r: dict) -> dict:
        return {
            "video": str(videos[vi]),
            "audio": str(audios[ai]),
            "video_idx": vi,
            "audio_idx": ai,
            "offset_seconds": r["offset_seconds"],
            "confidence": r.get("confidence", 0.0),
            "pearson_confidence": r.get("pearson_confidence", 0.0),
            "video_duration_s": r.get("video_duration_s", 0.0),
            "audio_duration_s": r.get("audio_duration_s", 0.0),
        }

    if audio_continuous:
        used_a: set[int] = set()
        assignments = []
        unpaired_videos = []
        for vi in range(len(videos)):
            best_ai = -1
            best_r = None
            best_conf = -1.0
            for ai in range(len(audios)):
                r = matrix[vi][ai]
                if r.get("confidence", 0.0) > best_conf:
                    best_conf = r["confidence"]
                    best_ai = ai
                    best_r = r
            if best_ai < 0 or best_r is None:
                unpaired_videos.append(str(videos[vi]))
                continue
            ok, reason = _check_pair(best_r, threshold, min_pearson,
                                      min_overlap_fraction=min_overlap_fraction,
                                      require_time_overlap=require_time_overlap)
            if not ok:
                rejected.append({**_build(vi, best_ai, best_r), "reject_reason": reason})
                unpaired_videos.append(str(videos[vi]))
                continue
            assignments.append(_build(vi, best_ai, best_r))
            used_a.add(best_ai)
        unpaired_audios = [str(audios[i]) for i in range(len(audios)) if i not in used_a]
        return {
            "assignments": assignments,
            "unpaired_videos": unpaired_videos,
            "unpaired_audios": unpaired_audios,
            "rejected_pairs": rejected,
        }

    # Default: greedy with one-audio-per-video constraint.
    pairs = []
    for vi in range(len(videos)):
        for ai in range(len(audios)):
            r = matrix[vi][ai]
            pairs.append((r.get("confidence", 0.0), vi, ai, r))
    pairs.sort(key=lambda p: -p[0])

    used_v: set[int] = set()
    used_a = set()
    assignments = []
    for conf, vi, ai, r in pairs:
        if vi in used_v or ai in used_a:
            continue
        ok, reason = _check_pair(r, threshold, min_pearson,
                                  min_overlap_fraction=min_overlap_fraction,
                                  require_time_overlap=require_time_overlap)
        if not ok:
            rejected.append({**_build(vi, ai, r), "reject_reason": reason})
            continue
        assignments.append(_build(vi, ai, r))
        used_v.add(vi)
        used_a.add(ai)

    unpaired_videos = [str(videos[i]) for i in range(len(videos)) if i not in used_v]
    unpaired_audios = [str(audios[i]) for i in range(len(audios)) if i not in used_a]
    return {
        "assignments": assignments,
        "unpaired_videos": unpaired_videos,
        "unpaired_audios": unpaired_audios,
        "rejected_pairs": rejected,
    }


SYNCED_SUBDIR = "synced"  # subfolder under the source root for clean batch outputs


def apply_assignments(
    assignments: list[dict],
    trim_head: bool = True,
    audio_continuous: bool = False,
    target_peak_min_db: float | None = None,
    target_peak_max_db: float | None = None,
) -> list[dict]:
    """For each accepted pair, build `<source_dir>/synced/<video_stem>_synced.mp4`.

    Outputs land in a `synced/` subfolder beside the raw files (created on
    demand) so the source directory stays uncluttered. The subfolder name is
    controlled by `SYNCED_SUBDIR` at module level.
    """
    out = []
    for a in assignments:
        v = Path(a["video"])
        ad = Path(a["audio"])
        synced_dir = v.parent / SYNCED_SUBDIR
        synced_dir.mkdir(parents=True, exist_ok=True)
        synced_path = synced_dir / f"{v.stem}_synced.mp4"
        print(f"syncing → {SYNCED_SUBDIR}/{synced_path.name}  (offset {a['offset_seconds']:+.3f}s, "
              f"conf {a['confidence']:.2f})", file=sys.stderr, flush=True)
        meta = apply_sync(
            v, ad, synced_path,
            a["offset_seconds"],
            trim_head=trim_head,
            audio_continuous=audio_continuous,
            target_peak_min_db=target_peak_min_db,
            target_peak_max_db=target_peak_max_db,
            match_confidence=a.get("confidence"),
            match_pearson=a.get("pearson_confidence"),
            audio_duration_s=a.get("audio_duration_s"),
            video_duration_s=a.get("video_duration_s"),
        )
        out.append({
            **a,
            "synced_path": str(synced_path),
            "sync_metadata": meta,
        })
    return out


def render_table(videos, audios, matrix, assignments_set):
    """Pretty matrix printout for console output."""
    name_w = max(len(v.name) for v in videos) if videos else 4
    aud_names = [a.name for a in audios]
    aud_w = max((len(n) for n in aud_names), default=4)
    cell_w = max(8, aud_w)

    header = f"{'video':<{name_w}}  " + "  ".join(f"{n[:cell_w]:>{cell_w}}" for n in aud_names)
    print(header)
    print("-" * len(header))
    for vi, v in enumerate(videos):
        cells = []
        for ai, _ in enumerate(audios):
            r = matrix[vi][ai]
            tag = "*" if (vi, ai) in assignments_set else " "
            cells.append(f"{tag}{r['confidence']:>{cell_w-1}.2f}")
        print(f"{v.name:<{name_w}}  " + "  ".join(cells))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--folder", type=Path, default=None,
                    help="Folder containing both videos and audio files (non-recursive).")
    ap.add_argument("--videos", type=Path, nargs="*", default=[],
                    help="Explicit list of video paths (alternative to --folder).")
    ap.add_argument("--audios", type=Path, nargs="*", default=[],
                    help="Explicit list of audio paths (alternative to --folder).")
    ap.add_argument("--threshold", type=float, default=1.5,
                    help="Minimum peak/runner-up confidence ratio to accept a pair. "
                         "Default 1.5. Catches 'is there a single dominant lag.' Easily "
                         "fooled by pure noise on its own — also see --min-pearson.")
    ap.add_argument("--min-pearson", type=float, default=0.30,
                    help="Minimum Pearson correlation coefficient of the aligned envelope "
                         "slices at the detected lag. Real audio matches score 0.3–0.9; "
                         "noise scores ~0. Default 0.30 — the gate that catches the "
                         "DJI-mic-doesn't-match-video failure mode. Pass 0.0 to disable.")
    ap.add_argument("--min-overlap-fraction", type=float, default=0.5,
                    help="Reject pairs where the chosen offset leaves overlap covering less "
                         "than this fraction of the shorter recording. Default 0.5 — catches "
                         "high-Pearson coincidences computed over silence-tail slices. "
                         "Pass 0.0 to disable.")
    ap.add_argument("--require-time-overlap", action="store_true",
                    help="Require recording-time timestamps (from filename patterns or "
                         "ffprobe creation_time) to overlap. OFF by default because DJI Mic "
                         "clocks are often hours wrong; turn ON when you trust your "
                         "equipment clocks for the strictest possible filter.")
    ap.add_argument("--rematch-batch", action="store_true",
                    help="Audit-only: rebuild the confidence matrix for --folder against "
                         "the new dual-gate metric and print which pairs would be accepted "
                         "vs rejected. Writes nothing. Use this before re-running --apply "
                         "on a folder whose first sync looked wrong.")
    ap.add_argument("--max-offset", type=float, default=600.0,
                    help="Clamp offset search to ±N seconds. Default 600 (10 min).")
    ap.add_argument("--apply", action="store_true",
                    help="After matching, sync each accepted pair "
                         "(produces <video_stem>_synced.mp4 alongside each source).")
    ap.add_argument("--no-trim-head", action="store_true",
                    help="When --apply, preserve the silent head instead of trimming. "
                         "Default trims (matches sync_audio.py behavior).")
    ap.add_argument("--audio-continuous", action="store_true",
                    help="The external audio recorder rolled continuously while the camera was "
                         "started/stopped per take (dual-system shoot). In this mode each video "
                         "is matched to its single best audio above the threshold (audio files "
                         "are NOT consumed — multiple videos can share one audio at different "
                         "offsets), and apply_sync trims the audio head to land on each video's "
                         "content-start. Required when offset > video_duration.")
    ap.add_argument("--target-peak-db", nargs=2, type=float, metavar=("MIN", "MAX"),
                    default=None,
                    help="Peak-level the external audio on every accepted pair so each "
                         "synced output's peak lands at the midpoint of [MIN, MAX] dBFS. "
                         "Linear gain — never drops below the requested window. "
                         "Example: --target-peak-db -6 -3 (dialogue), -24 -20 (music).")
    ap.add_argument("--level-dialogue", action="store_true",
                    help="Shortcut for --target-peak-db -6 -3.")
    ap.add_argument("--level-music", action="store_true",
                    help="Shortcut for --target-peak-db -24 -20.")
    ap.add_argument("--json", action="store_true",
                    help="Print result as JSON.")
    args = ap.parse_args()

    # Resolve dialogue/music shortcuts.
    if args.level_dialogue and args.level_music:
        sys.exit("--level-dialogue and --level-music are mutually exclusive")
    if args.target_peak_db is None:
        if args.level_dialogue:
            args.target_peak_db = [-6.0, -3.0]
        elif args.level_music:
            args.target_peak_db = [-24.0, -20.0]

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")

    if args.folder:
        if not args.folder.exists() or not args.folder.is_dir():
            sys.exit(f"folder not found or not a directory: {args.folder}")
        videos, audios = discover(args.folder)
    else:
        videos = list(args.videos)
        audios = list(args.audios)

    if not videos:
        sys.exit("no videos found")
    if not audios:
        sys.exit("no audio files found")

    matrix = build_matrix(videos, audios, max_offset_s=args.max_offset)
    result = greedy_assign(
        videos, audios, matrix,
        threshold=args.threshold,
        audio_continuous=args.audio_continuous,
        min_pearson=args.min_pearson,
        min_overlap_fraction=args.min_overlap_fraction,
        require_time_overlap=args.require_time_overlap,
    )

    # Audit mode — print the verdict and exit without touching anything.
    if args.rematch_batch:
        print()
        print(f"AUDIT  threshold={args.threshold:.2f}  min_pearson={args.min_pearson:.2f}  "
              f"audio_continuous={args.audio_continuous}")
        print(f"  accepted: {len(result['assignments'])}")
        for a in result["assignments"]:
            print(f"    {Path(a['video']).name}  <->  {Path(a['audio']).name}  "
                  f"offset {a['offset_seconds']:+.3f}s  conf {a['confidence']:.2f}  "
                  f"pearson {a['pearson_confidence']:.2f}")
        print(f"  rejected: {len(result['rejected_pairs'])}")
        for r in result["rejected_pairs"]:
            print(f"    {Path(r['video']).name}  X  {Path(r['audio']).name}  "
                  f"offset {r['offset_seconds']:+.3f}s  conf {r['confidence']:.2f}  "
                  f"pearson {r['pearson_confidence']:.2f}  -- {r['reject_reason']}")
        print(f"  unpaired videos: {len(result['unpaired_videos'])}")
        for v in result["unpaired_videos"]:
            print(f"    {Path(v).name}")
        return

    if args.apply:
        target_min, target_max = (args.target_peak_db or [None, None])
        result["assignments"] = apply_assignments(
            result["assignments"],
            trim_head=not args.no_trim_head,
            audio_continuous=args.audio_continuous,
            target_peak_min_db=target_min,
            target_peak_max_db=target_max,
        )

    if args.json:
        # Emit a fully-serializable form.
        out = {
            "videos": [str(v) for v in videos],
            "audios": [str(a) for a in audios],
            "threshold": args.threshold,
            "matrix": matrix,
            **result,
        }
        print(json.dumps(out, indent=2))
        return

    print()
    assigned_set = {(a["video_idx"], a["audio_idx"]) for a in result["assignments"]}
    render_table(videos, audios, matrix, assigned_set)
    print()
    print("ASSIGNED PAIRS:")
    for a in result["assignments"]:
        v = Path(a["video"]).name
        ad = Path(a["audio"]).name
        line = (f"  {v}  <->  {ad}   offset {a['offset_seconds']:+.3f}s   "
                f"conf {a['confidence']:.2f}   pearson {a['pearson_confidence']:.2f}")
        if "synced_path" in a:
            line += f"   -> {Path(a['synced_path']).name}"
        print(line)
    if result.get("rejected_pairs"):
        print(f"REJECTED PAIRS ({len(result['rejected_pairs'])}):")
        for r in result["rejected_pairs"]:
            print(f"  {Path(r['video']).name}  X  {Path(r['audio']).name}   "
                  f"offset {r['offset_seconds']:+.3f}s   conf {r['confidence']:.2f}   "
                  f"pearson {r['pearson_confidence']:.2f}   -- {r['reject_reason']}")
    if result["unpaired_videos"]:
        print(f"UNPAIRED VIDEOS:")
        for v in result["unpaired_videos"]:
            print(f"  {Path(v).name}")
    if result["unpaired_audios"]:
        print(f"UNPAIRED AUDIOS:")
        for a in result["unpaired_audios"]:
            print(f"  {Path(a).name}")


if __name__ == "__main__":
    main()
