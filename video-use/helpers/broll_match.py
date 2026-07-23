"""Semantic B-roll ↔ script matcher — the local, zero-API-cost replacement for
asking Gemini "which clip fits this line?".

How it works
------------
Uses a local CLIP-family model (SigLIP 2 by default) to embed every B-roll clip
(by sampling frames) and every script line into the same vision-language space,
then assigns each script segment the clip whose visuals are most similar to what
the line says. No network calls, no per-run cost. Frame embeddings are cached per
clip byte-hash (mirrors the transcript cache), so re-runs on the same folder are
instant.

Output is a broll_overlay.py-compatible EDL, so this drops straight into the
existing compositing step:

    [{"start": 0.0, "end": 3.2, "source": ".../clip_004.mp4", "source_in": 1.1,
      "score": 0.28, "text": "we replace your drafty old windows"}]

Honest limits vs. Gemini
------------------------
CLIP matches what is VISIBLE in a clip, not narrative intent. Literal lines
("installing a new window", "a happy family at home") match great. Abstract lines
("financial freedom", "peace of mind") match poorly — those come back with a LOW
score. Anything below --min-score is emitted with "low_confidence": true and a
"needs_fallback" flag so the orchestrator can route just those lines to Gemini or
a stock/generated clip. ~90% of a normal script matches for free; you only spend
on the hard 10%.

Usage
-----
    # segments with timings (preferred — e.g. from the VO word timestamps)
    python helpers/broll_match.py --broll-folder BROLL/ \\
        --segments segments.json --json > edl.json

    # or plain lines + a VO duration to distribute evenly
    python helpers/broll_match.py --broll-folder BROLL/ \\
        --line "old windows waste money" --line "our crew installs in a day" \\
        --vo-duration 24 --json > edl.json

    # inspect the ranking without writing an EDL
    python helpers/broll_match.py --broll-folder BROLL/ --segments segs.json --explain

segments.json = [{"start": 0.0, "end": 3.2, "text": ".."}, ...]

Environment
-----------
    BROLL_CLIP_MODEL   open_clip spec. Default a SigLIP2 checkpoint; falls back to
                       ViT-B-32/laion2b if the primary can't be loaded.
    BROLL_MATCH_DEVICE cuda | cpu (default: auto-detect)

Dependencies (one-time):
    pip install open_clip_torch torch pillow
FFmpeg must be on PATH (already required by the studio).

Exit codes: 0 ok · 2 usage · 3 missing deps · 4 no clips.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
CACHE_DIRNAME = ".clip_cache"
PRIMARY_MODEL = os.environ.get(
    "BROLL_CLIP_MODEL", "hf-hub:timm/ViT-SO400M-16-SigLIP2-384")
FALLBACK_MODEL = ("ViT-B-32", "laion2b_s34b_b79k")
FRAMES_PER_CLIP = 3


def die(msg: str, code: int = 2) -> None:
    sys.stderr.write(f"broll_match: {msg}\n")
    sys.exit(code)


# ── model loading (lazy so --help works without torch) ──────────────────────

def load_model():
    try:
        import torch  # noqa: F401
        import open_clip
    except ImportError:
        die("missing deps. Run: pip install open_clip_torch torch pillow", 3)
    import torch

    device = os.environ.get("BROLL_MATCH_DEVICE") or (
        "cuda" if torch.cuda.is_available() else "cpu")

    def _try(spec):
        if isinstance(spec, str) and spec.startswith("hf-hub:"):
            model, _, preprocess = open_clip.create_model_and_transforms(spec)
            tokenizer = open_clip.get_tokenizer(spec)
        else:
            name, pretrained = spec if isinstance(spec, tuple) else (spec, None)
            model, _, preprocess = open_clip.create_model_and_transforms(
                name, pretrained=pretrained)
            tokenizer = open_clip.get_tokenizer(name)
        return model, preprocess, tokenizer

    try:
        model, preprocess, tokenizer = _try(PRIMARY_MODEL)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"broll_match: primary model '{PRIMARY_MODEL}' failed "
                         f"({e}); falling back to {FALLBACK_MODEL[0]}.\n")
        model, preprocess, tokenizer = _try(FALLBACK_MODEL)

    model = model.to(device).eval()
    return model, preprocess, tokenizer, device


# ── frame extraction + caching ──────────────────────────────────────────────

def _byte_hash(path: Path) -> str:
    h = hashlib.sha1()
    h.update(str(path.stat().st_size).encode())
    with path.open("rb") as f:
        h.update(f.read(1 << 20))  # first 1MB is plenty to disambiguate
    return h.hexdigest()[:16]


def _probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _extract_frames(clip: Path, n: int, tmp: Path) -> list[Path]:
    """Grab n frames spread across the clip via ffmpeg."""
    dur = _probe_duration(clip)
    if dur <= 0:
        return []
    stamps = [dur * (i + 1) / (n + 1) for i in range(n)]
    out = []
    for i, t in enumerate(stamps):
        fp = tmp / f"{clip.stem}_{i}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(clip),
             "-frames:v", "1", "-q:v", "3", str(fp)],
            capture_output=True)
        if r.returncode == 0 and fp.exists():
            out.append(fp)
    return out


def embed_clips(clips: list[Path], model, preprocess, device, broll_root: Path):
    """Return {clip_path: normalized mean image embedding (list[float])}, cached."""
    import torch
    from PIL import Image

    cache_dir = broll_root / CACHE_DIRNAME
    cache_dir.mkdir(exist_ok=True)
    embeddings: dict[Path, list] = {}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for clip in clips:
            key = _byte_hash(clip)
            cache_file = cache_dir / f"{clip.stem}_{key}.json"
            if cache_file.exists():
                try:
                    embeddings[clip] = json.loads(cache_file.read_text())["emb"]
                    continue
                except Exception:
                    pass
            frames = _extract_frames(clip, FRAMES_PER_CLIP, tmp)
            if not frames:
                sys.stderr.write(f"broll_match: could not read frames from {clip.name}\n")
                continue
            imgs = torch.stack([preprocess(Image.open(f).convert("RGB")) for f in frames]).to(device)
            with torch.no_grad():
                feats = model.encode_image(imgs)
                feats = feats / feats.norm(dim=-1, keepdim=True)
                mean = feats.mean(dim=0)
                mean = mean / mean.norm()
            emb = mean.cpu().tolist()
            embeddings[clip] = emb
            cache_file.write_text(json.dumps({"emb": emb, "clip": clip.name}))
    return embeddings


def embed_texts(texts: list[str], model, tokenizer, device):
    import torch
    toks = tokenizer(texts).to(device)
    with torch.no_grad():
        feats = model.encode_text(toks)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().tolist()


# ── matching ─────────────────────────────────────────────────────────────────

def _cos(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))


def match(segments, clip_emb, text_emb, *, min_score, allow_reuse, reuse_penalty):
    """Greedy: each segment (in time order) takes its best available clip."""
    clips = list(clip_emb.keys())
    used: dict[Path, int] = {}
    edl = []
    for seg, temb in zip(segments, text_emb):
        ranked = sorted(
            clips,
            key=lambda c: _cos(temb, clip_emb[c]) - reuse_penalty * used.get(c, 0),
            reverse=True,
        )
        alternatives = [
            {"source": str(c), "score": round(_cos(temb, clip_emb[c]), 4)}
            for c in ranked[:3]
        ]
        pick = None
        for c in ranked:
            if allow_reuse or used.get(c, 0) == 0:
                pick = c
                break
        if pick is None:  # everything used and reuse disallowed → reuse best anyway
            pick = ranked[0]
        used[pick] = used.get(pick, 0) + 1
        raw_score = _cos(temb, clip_emb[pick])
        dur = seg["end"] - seg["start"]
        clip_dur = _probe_duration(pick)
        source_in = round(max(0.0, (clip_dur - dur) / 2), 3) if clip_dur > dur else 0.0
        edl.append({
            "start": round(seg["start"], 3),
            "end": round(seg["end"], 3),
            "source": str(pick),
            "source_in": source_in,
            "score": round(raw_score, 4),
            "text": seg["text"],
            "low_confidence": raw_score < min_score,
            "needs_fallback": raw_score < min_score,
            "alternatives": alternatives,
        })
    return edl


def load_segments(args) -> list[dict]:
    if args.segments:
        data = json.loads(Path(args.segments).read_text(encoding="utf-8"))
        segs = [{"start": float(s["start"]), "end": float(s["end"]),
                 "text": str(s["text"])} for s in data]
        if not segs:
            die("segments file was empty")
        return segs
    if args.line:
        n = len(args.line)
        total = args.vo_duration or float(n * 3)
        step = total / n
        return [{"start": round(i * step, 3), "end": round((i + 1) * step, 3),
                 "text": t} for i, t in enumerate(args.line)]
    die("provide --segments FILE or one/more --line TEXT")


def main() -> None:
    ap = argparse.ArgumentParser(description="Semantic B-roll↔script matcher (local CLIP).")
    ap.add_argument("--broll-folder", required=True, type=Path)
    ap.add_argument("--segments", help="JSON [{start,end,text}] with VO timings")
    ap.add_argument("--line", action="append", default=[], help="script line (repeatable)")
    ap.add_argument("--vo-duration", type=float, default=0.0,
                    help="total VO seconds to distribute --line segments across")
    ap.add_argument("--min-score", type=float, default=0.18,
                    help="below this a match is flagged low_confidence/needs_fallback")
    ap.add_argument("--allow-reuse", action="store_true",
                    help="let a clip be used for multiple segments")
    ap.add_argument("--reuse-penalty", type=float, default=0.05,
                    help="similarity penalty per prior use (encourages variety)")
    ap.add_argument("--explain", action="store_true",
                    help="print a human ranking table to stderr")
    ap.add_argument("--json", action="store_true", help="print EDL JSON to stdout")
    args = ap.parse_args()

    broll_root = args.broll_folder.resolve()
    if not broll_root.is_dir():
        die(f"not a folder: {broll_root}")
    clips = sorted(p for p in broll_root.rglob("*")
                   if p.suffix.lower() in (VIDEO_EXTS | IMAGE_EXTS)
                   and CACHE_DIRNAME not in p.parts)
    if not clips:
        die(f"no b-roll clips found under {broll_root}", 4)

    segments = load_segments(args)
    model, preprocess, tokenizer, device = load_model()
    sys.stderr.write(f"broll_match: {len(clips)} clips, {len(segments)} segments, device={device}\n")

    clip_emb = embed_clips(clips, model, preprocess, device, broll_root)
    if not clip_emb:
        die("failed to embed any clips", 4)
    text_emb = embed_texts([s["text"] for s in segments], model, tokenizer, device)

    edl = match(segments, clip_emb, text_emb,
                min_score=args.min_score, allow_reuse=args.allow_reuse,
                reuse_penalty=args.reuse_penalty)

    if args.explain:
        for e in edl:
            flag = "  ⚠ LOW — needs fallback" if e["needs_fallback"] else ""
            sys.stderr.write(
                f'[{e["start"]:>5.1f}-{e["end"]:<5.1f}] {e["score"]:.3f}  '
                f'{Path(e["source"]).name:<32} “{e["text"][:48]}”{flag}\n')
        weak = [e for e in edl if e["needs_fallback"]]
        sys.stderr.write(f"\n{len(weak)}/{len(edl)} segment(s) below --min-score "
                         f"({args.min_score}) — route those to Gemini/stock.\n")

    if args.json or not args.explain:
        print(json.dumps(edl, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
