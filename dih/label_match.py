"""Turn a Gemini script into a timed b-roll EDL — label-first, CLIP fallback.

For each spoken segment:
  1. LABEL-FIRST  — if the segment's `label` exists in broll/ and has clips, pull
     a clip from that folder (no clip is reused within one video while unused
     ones remain).
  2. CLIP FALLBACK — segments with a blank/unmatched label are batched to the
     existing video-use CLIP matcher (broll_match.py), which picks by what the
     frames actually show. If that's unavailable (no torch), we degrade to a
     random unused clip and flag the segment for review.

Segment durations are distributed across the voiceover length in proportion to
each line's word count, so the visuals change in step with the narration and the
b-roll timeline exactly equals the VO length.

Output EDL (compose.py / broll_overlay.py compatible):
  [{"start","end","source","source_in","text","label","role","via"}]
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

import common

MIN_SEG = 1.6   # never hold a single clip for less than this many seconds


def probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def assign_durations(segments: list[dict], vo_duration: float) -> list[tuple[float, float]]:
    """Return [(start, end)] per segment, weighted by word count, summing to vo."""
    weights = [max(1, len((s.get("text") or "").split())) for s in segments]
    total_w = sum(weights)
    # enforce a floor, then renormalize so the sum still equals vo_duration
    raw = [vo_duration * w / total_w for w in weights]
    raw = [max(MIN_SEG, d) for d in raw]
    scale = vo_duration / sum(raw)
    raw = [d * scale for d in raw]
    spans, cursor = [], 0.0
    for i, d in enumerate(raw):
        start = cursor
        end = vo_duration if i == len(raw) - 1 else cursor + d
        spans.append((round(start, 3), round(end, 3)))
        cursor = end
    return spans


def _source_in(clip: Path, need: float) -> float:
    """Center the used window inside the clip when it's longer than needed."""
    cd = probe_duration(clip)
    return round(max(0.0, (cd - need) / 2), 3) if cd > need + 0.05 else 0.0


def _run_clip_fallback(cfg: dict, fb_segments: list[dict]) -> dict[int, Path]:
    """Call video-use/helpers/broll_match.py for the fallback segments.
    Returns {segment_index: chosen clip path}. Empty dict if unavailable."""
    if not fb_segments:
        return {}
    helper = common.HELPERS / "broll_match.py"
    if not helper.exists():
        return {}
    # Prefer the video-use venv interpreter — that's where torch/open_clip live.
    vu_py = common.VEDITOR_ROOT / "video-use" / ".venv" / "Scripts" / "python.exe"
    py = str(vu_py) if vu_py.exists() else sys.executable
    seg_payload = [{"start": s["start"], "end": s["end"], "text": s["text"]}
                   for s in fb_segments]
    tmp = common.output_dir(cfg) / "_clip_fallback_segments.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(seg_payload), encoding="utf-8")
    cmd = [py, str(helper), "--broll-folder", str(common.broll_dir(cfg)),
           "--segments", str(tmp), "--allow-reuse",
           "--min-score", str(cfg.get("broll", {}).get("clip_min_score", 0.18)),
           "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception as e:  # noqa: BLE001
        common.eprint(f"[clip-fallback] could not run broll_match.py: {e}")
        return {}
    if r.returncode != 0:
        common.eprint(f"[clip-fallback] broll_match.py exit {r.returncode}: {r.stderr[-400:]}")
        return {}
    try:
        edl = json.loads(r.stdout)
    except Exception:
        return {}
    return {fb_segments[i]["_idx"]: Path(e["source"]) for i, e in enumerate(edl)}


def build_edl(cfg: dict, script: dict, vo_duration: float,
              seed: int | None = None) -> tuple[list[dict], dict]:
    if seed is not None:
        random.seed(seed)
    segments = script["segments"]
    spans = assign_durations(segments, vo_duration)

    # pools of unused clips per label (shuffled for variety across the week)
    pools: dict[str, list[Path]] = {}
    for label in {s.get("label", "") for s in segments if s.get("label")}:
        clips = common.clips_for_label(cfg, label)
        random.shuffle(clips)
        pools[label] = clips

    edl: list[dict] = []
    fallback: list[dict] = []
    all_clips_cache: list[Path] | None = None

    for i, (seg, (start, end)) in enumerate(zip(segments, spans)):
        label = seg.get("label", "")
        entry = {"start": start, "end": end, "text": seg["text"],
                 "label": label, "role": seg.get("role", "body"),
                 "source": None, "source_in": 0.0, "via": None}
        pool = pools.get(label)
        if pool:
            clip = pool.pop(0)          # unused clip from this label
            entry["source"] = str(clip)
            entry["source_in"] = _source_in(clip, end - start)
            entry["via"] = "label"
        else:
            # needs the CLIP fallback pass (blank label, or label exhausted/empty)
            fallback.append({**entry, "_idx": i})
        edl.append(entry)

    # resolve fallbacks in one batched CLIP pass
    if fallback and cfg.get("broll", {}).get("clip_fallback", True):
        picked = _run_clip_fallback(cfg, fallback)
        for idx, clip in picked.items():
            edl[idx]["source"] = str(clip)
            edl[idx]["source_in"] = _source_in(clip, edl[idx]["end"] - edl[idx]["start"])
            edl[idx]["via"] = "clip"

    # last-resort fill for anything still unresolved: random clip, flagged
    needs_review = []
    for i, entry in enumerate(edl):
        if entry["source"]:
            continue
        if all_clips_cache is None:
            all_clips_cache = [
                f for f in common.broll_dir(cfg).rglob("*")
                if f.is_file() and f.suffix.lower() in (common.VIDEO_EXTS | common.IMAGE_EXTS)
                and not f.name.startswith("._")
            ]
        if all_clips_cache:
            clip = random.choice(all_clips_cache)
            entry["source"] = str(clip)
            entry["source_in"] = _source_in(clip, entry["end"] - entry["start"])
            entry["via"] = "random"
            needs_review.append(i)
        else:
            needs_review.append(i)

    report = {
        "segments": len(edl),
        "via_label": sum(1 for e in edl if e["via"] == "label"),
        "via_clip": sum(1 for e in edl if e["via"] == "clip"),
        "via_random": sum(1 for e in edl if e["via"] == "random"),
        "unresolved": [i for i, e in enumerate(edl) if not e["source"]],
        "needs_review": needs_review,
    }
    return edl, report


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Build a timed b-roll EDL from a DiH script.")
    ap.add_argument("--script", type=Path, required=True, help="script JSON from gemini_script.py")
    ap.add_argument("--vo-duration", type=float, required=True)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", type=Path, help="write EDL JSON here")
    args = ap.parse_args()

    cfg = common.load_config()
    script = json.loads(args.script.read_text(encoding="utf-8"))
    edl, report = build_edl(cfg, script, args.vo_duration, seed=args.seed)
    common.eprint(f"[edl] {report}")
    out_json = json.dumps(edl, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(out_json, encoding="utf-8")
        common.eprint(f"wrote {args.out}")
    else:
        print(out_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
