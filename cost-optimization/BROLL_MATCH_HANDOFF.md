# B-roll matching — CLIP matcher + free B-roll sources

Two things: (1) `broll_match.py`, the local zero-cost replacement for Gemini
B-roll↔script matching, ready to wire in; (2) free B-roll source options you can
offer as a selectable mode.

---

## 1. `broll_match.py` — what it does

Embeds every B-roll clip (SigLIP 2, sampled frames) and every script line into one
vision-language space, assigns each script segment its most-similar clip, and emits
a **broll_overlay.py-compatible EDL** (`{start, end, source, source_in}` + `score`).
Local, no API calls, frame embeddings cached per clip byte-hash so re-runs are instant.

**Every match carries a `score`.** Below `--min-score` (default 0.18) a segment is
flagged `needs_fallback: true` — that's your signal to send just that line to Gemini
or a stock/generated clip. Literal lines match great; abstract lines ("financial
freedom") score low and get flagged. Typically ~90% match for free.

Run it standalone today:

```
python broll_match.py --broll-folder BROLL/ --segments segs.json --explain
python broll_match.py --broll-folder BROLL/ \
    --line "old windows waste money" --line "our crew installs in a day" \
    --vo-duration 24 --json > edl.json
```

`segs.json` = `[{"start":0.0,"end":3.2,"text":".."}, ...]` — you already have these
timings from the VO word timestamps that `variant_factory.py` builds.

## 2. Wiring it in (Claude Code — touches the engine + live studio)

1. **Install deps** into the engine venv:
   `video-use/.venv/Scripts/python.exe -m pip install open_clip_torch torch pillow`
   (GPU torch build if the NVENC box has CUDA — big speedup, but CPU works.)
2. **Move** `cost-optimization/broll_match.py` → `video-use/helpers/broll_match.py`
   (staged outside per the "never write inside video-use/" rule).
3. **Replace the manual Gemini match path**: wherever the studio currently asks
   Gemini "which clip fits this line" to build an EDL, call `broll_match.py` with the
   VO segments instead, then feed its EDL straight to `broll_overlay.py`. Route only
   `needs_fallback` segments to the existing Gemini/stock path.
4. **Optional — upgrade the autonomous variant path.** `step_plan_broll_sequence`
   in `variant_factory.py` is currently **random-window** (shuffled clips, no matching).
   Add a `--match semantic` mode that calls `broll_match.py` so autonomous variants can
   be script-relevant, keeping `random` as the default/fast option. Expose it as a UI
   toggle ("Match B-roll to script: Random / Semantic").
5. **Warm the cache**: first run per folder embeds all clips (seconds each on GPU).
   The `.clip_cache/` folder makes every later run instant. Safe to commit-ignore.

Acceptance: `broll_match.py --explain` on a real BROLL folder prints a ranked table;
the resulting EDL composites correctly through `broll_overlay.py`; low-score lines are
flagged and only those hit Gemini.

## 3. Free B-roll source options (selectable mode)

When the local library has no good clip for a line (or you just want fresh footage),
two free routes — I'd add both as a `broll_fetch.py` helper and a UI source picker:

**A. Free stock footage APIs (recommended — real 4K, no watermark, instant).**
- **Pexels Video API** — completely free, no cost tier, keyword search, 4K clips.
- **Pixabay Video API** — free, royalty-free, no attribution required.
- Fit: perfect for the `needs_fallback` gap-fill. A `broll_fetch.py --query "window
  installation" --dur 4` downloads a matching clip into the BROLL folder, which
  `broll_match.py` then re-embeds and uses. Often better than generative for real-world
  product/lifestyle B-roll, and truly $0.

**B. Open-source generative video (when you need a shot that doesn't exist as stock).**
- **LTX-2** (Lightricks, open source) — text/image-to-video, up to 4K/50fps/20s, native
  audio, runs locally / ComfyUI. The free-and-local counterpart to your paid Veo path.
- Keep **Veo** as the premium option; offer LTX-2 as the "$0, run it locally" alternative
  in the same generate-clip UI you already have for Veo/Nano Banana.
- Note: local video gen is GPU-heavy and slower than a stock download — position it as
  "generate a custom shot," not the default gap-filler.

Suggested source precedence for a weak/missing match:
**local library (CLIP) → free stock (Pexels/Pixabay) → generate (LTX-2 local, or Veo if premium).**

## Sources
- [open_clip (SigLIP 2 support)](https://github.com/mlfoundations/open_clip) · [SigLIP 2 is the strongest open image-text model (2026)](https://www.spheron.network/blog/multimodal-embedding-models-gpu-cloud-siglip2-jinaclip-cohere/)
- [Semantic video frame search with CLIP + vectors](https://docs.vultr.com/semantic-video-frame-search-using-openai-clip-and-vector-database)
- [Pexels free image & video API](https://www.pexels.com/api/) · [Pixabay free B-roll](https://pixabay.com/videos/search/b-roll/)
- [LTX-2 open-source 4K text/image-to-video](https://ltx-2ai.com/)
