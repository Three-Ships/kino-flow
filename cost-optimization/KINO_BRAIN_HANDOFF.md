# Kino Brain — wire-in + roadmap

A per-team learning memory Kino reads before writing. The model doesn't retrain; it
consults an accumulating store each run, so output improves as the user feeds it. Core
helper `kino_brain.py` is built + tested (staged in `cost-optimization/`).

Store per team:  `<asset_root>/<team>/kino_brain/`  → `results.jsonl` + `notes.md`.
All three feedback signals share one record shape: ratings/verdicts, freeform notes, and
(later) real metrics — see the helper's docstring.

## Phase 1 — the brain, read every run (build now)

1. Copy `kino_brain.py` → `video-use/helpers/kino_brain.py`.
2. **Inject the briefing into the Kino prompt.** In `app.js`, where the Variants prompt
   currently builds its "WINNING SCRIPTS / brand" section, add the compiled brain:
   - Call a new `POST /api/kino_brain/compile` with the team's brain dir; take the
     returned `briefing` string and inject it into the prompt (right after the BRAND
     GUIDELINES block). This *replaces/augments* the soft PASS-2 "study winning scripts"
     step with a concrete, ranked WINNERS / AVOID / NOTES block.
   - Brain dir = `<resolved team folder>/kino_brain` (reuse the same team-folder resolver
     the Brand Guidelines lookup uses).
3. **Add endpoints in `server.py`** (thin passthroughs to the helper, like `/api/copy_gen`):
   - `POST /api/kino_brain/compile` → runs `kino_brain.py compile --brain <dir> --json`.
   - `POST /api/kino_brain/add-result` → `add-result` with the posted fields.
   - `POST /api/kino_brain/add-note` → `add-note`.
4. **Add a tiny "log result" UI.** After a variants run (or in a small panel), let the user
   rate an ad (1–5 or winner/dud) + a note; POST to `/api/kino_brain/add-result` with the
   hook/angle/script from that run. This is how the brain grows.

Cost note: `compile` output is size-bounded (`--max-winners`, `--max-notes-chars`), so the
prompt stays flat-cost as the brain grows — it rides the Workstream-A savings.

Acceptance: log two results + a note, run Variants, confirm the prompt now contains a
"KINO BRAIN" block with the ranked winners and notes; generated scripts reflect them.

## Phase 2 — real ad metrics (Meta Ads connector)

The registry has a **Meta Ads** connector (`mcp.facebook.com/ads`, not yet connected). Once
connected: a small sync pulls CTR/ROAS/spend per ad and writes them onto matching
`results.jsonl` records (match by hook text or a run id stored on the ad). The helper already
ranks metric-backed records above rating-only ones (`roas` > `ctr` > `rating` > `verdict`), so
no compile changes needed — data just starts flowing in. Have the user connect Meta Ads when
ready and I'll build the sync.

## Phase 3 — retrieval (only once the brain is large)

Swap the top-N ranking in `kino_brain.compile()` for semantic retrieval: embed each
result/note (reuse the `open_clip` text encoder already installed, or an Ollama embed model),
embed the current brief/angle, and pull the most RELEVANT lessons rather than just the
top-scoring ones. Keeps the briefing sharp and bounded when there are hundreds of entries.

## Honest framing for the user
This is memory + retrieval, not model self-training. Kino gets better because its reference
material accumulates and the best of it is surfaced each run — reliable, inspectable, and you
own the data (plain files you can edit).
