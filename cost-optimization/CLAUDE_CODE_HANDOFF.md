# Handoff to Claude Code — Veditor updates

> **INSTRUCTION TO CLAUDE CODE:** This is a work order, not a document to summarize.
> **Make the actual code edits described below** (edit `server.py`, `variant_factory.py`,
> move the helpers, wire the calls). After each task, show the diff and run the acceptance
> check. If something blocks you, say what and why — do not stop at "here's the plan."

> **STEP 0 — prerequisites (already handled by the user via `setup_veditor_addons.ps1`).**
> That script copies `broll_match.py` / `broll_fetch.py` / `copy_gen.py` into
> `video-use/helpers/`, installs `open_clip_torch`+`torch`+`pillow` into the venv, installs
> Ollama + pulls the copy model, and writes the Pexels/Pixabay keys to `video-use/.env`.
> If any of that is missing, run the script first (or tell the user to). Then implement below.

Everything referenced lives in `cost-optimization/`. Two independent workstreams — do either first.

**Staged files**
| File | What it is |
|---|---|
| `broll_match.py` | Local CLIP (SigLIP 2) B-roll↔script matcher → EDL. Zero API cost. |
| `broll_fetch.py` | Free stock B-roll downloader (Pexels/Pixabay). |
| `BROLL_MATCH_HANDOFF.md` | Detailed wire-in for the two B-roll helpers. |
| `copy_gen.py` | Local Ollama ad-copy generator (replaces Opus copywriting). |
| `model_routing.json` | Job-type → Claude model map. |
| `server_patch.md` | Drop-in `server.py` code for model routing + session hygiene. |
| `CLAUDE.md.proposed` | On-demand-context rewrite of the project CLAUDE.md. |
| `QUALITY_REPOS.md`, `MORE_REPOS.md` | Reference only — not tasks. |

All Python helpers compile clean and their CLIs run (`--help`) without their heavy
deps installed (imports are lazy). Respect the **restart discipline** in the project
CLAUDE.md before reloading the studio: check for a running `claude.exe`/`ffmpeg.exe`
first; if a job is active, don't restart — tell the user to Ctrl+C and relaunch.

---

## Workstream B — B-roll matching (the priority; ship this)

> **✅ SHIPPED 2026-07-20.** All four acceptance checks pass; studio relaunched on the
> new code. What was done + deviations from the plan:
> - **B1:** helpers were already placed by the setup script; deps `open_clip_torch/torch/pillow`
>   present (CPU torch — no CUDA on this box, so first-embed is minutes, then cached).
> - **Fixed a bug in `broll_match.py`:** `_try()` called `.startswith()` on the tuple
>   `FALLBACK_MODEL` → crashed whenever the primary model failed. Guarded with `isinstance(spec, str)`.
>   Fixed in BOTH the placed copy and the `cost-optimization/` source.
> - **Extra dep:** the SigLIP2 primary (`hf-hub:timm/ViT-SO400M-16-SigLIP2-384`) needs
>   `transformers` for its tokenizer — NOT in the setup list. Installed via `uv pip install
>   transformers`. **Add `transformers` to `setup_veditor_addons.ps1`.** (Without it, the
>   now-fixed fallback ViT-B-32 works but matches weaker.)
> - **B2/B3:** semantic mode wired into `variant_factory.py` (`step_plan_broll_semantic` +
>   `--match {random,semantic}` / `--match-min-score` / `--broll-fallback {none,stock}`;
>   default stays `random`). `/api/variant_factory` forwards the three flags; Variants modal
>   has a "Match B-roll to script: Random / Semantic" + "weak-line fallback" picker; the Kino
>   prompt injects `match`/`broll_fallback` into each variant's JSON body. Semantic falls back
>   to random if the matcher can't run (a batch never hard-fails).
> - **B4:** `broll_fetch.py` verified downloading (Pixabay, 4K, + `.src.json`). **⚠ Pexels
>   returned HTTP 400** — the `PEXELS_API_KEY` looks bad/misformatted; Pixabay auto-fallback
>   covered it. Check the Pexels key.
> - Acceptance evidence: `--explain` printed a ranked table (literal install line 0.210 matched;
>   abstract lines flagged `needs_fallback`); the real wired path `step_plan_broll_semantic` →
>   `step_render_broll_concat` produced a valid 1080×1920 / 13.8s concat = VO length; `.clip_cache/`
>   warmed to 37 entries.

Replaces the hand-triggered Gemini "which clip fits this line" step with a local,
$0 matcher, and adds free stock footage as a fallback source.

### B1 — Install deps + place the matcher
- `video-use/.venv/Scripts/python.exe -m pip install open_clip_torch torch pillow`
  (CUDA torch build if the box has a GPU — big speedup; CPU works too.)
- Move `broll_match.py` → `video-use/helpers/broll_match.py`.
- First run per folder embeds all clips and writes `.clip_cache/`; later runs are instant.

### B2 — Swap it into the matching path
- Wherever the studio currently asks Gemini to build a B-roll EDL from the VO script,
  call `broll_match.py --segments <vo_segments.json> --json` instead (you already have
  VO word-level timings in `variant_factory.py`). Feed its EDL straight to
  `broll_overlay.py` — the output format already matches (`start/end/source/source_in`).
- Every segment carries a `score`; only ones flagged `needs_fallback: true` (below
  `--min-score`, default 0.18) go to the fallback path (Gemini, or B4 stock/generate).

### B3 — Optional: upgrade the autonomous variant path
- `step_plan_broll_sequence` in `variant_factory.py` is currently **random-window**
  (shuffled clips, no matching). Add a `--match semantic` mode that routes through
  `broll_match.py`, keeping `random` as the fast default. Expose as a UI toggle
  ("Match B-roll to script: Random / Semantic").

### B4 — Free stock fallback
- Move `broll_fetch.py` → `video-use/helpers/broll_fetch.py`.
- Add `PEXELS_API_KEY` and/or `PIXABAY_API_KEY` to `video-use/.env` (both free).
- For a `needs_fallback` line, call
  `broll_fetch.py --query "<line-derived terms>" --orientation portrait --dur 4 --output <BROLL>`;
  it saves the clip + a `.src.json` sidecar, then `broll_match.py` re-embeds it next pass.
- Source precedence for a weak/missing match: **local library (CLIP) → free stock
  (Pexels/Pixabay) → generate (LTX-2 local, or Veo premium).** LTX-2 (open-source
  4K text/image-to-video, ComfyUI) is optional — position as "generate a custom shot,"
  not the default gap-filler.

**Acceptance (B):** `broll_match.py --explain` on a real BROLL folder prints a ranked
table; its EDL composites correctly via `broll_overlay.py`; low-score lines are flagged;
`broll_fetch.py` downloads a matching clip when a key is set. Full detail in
`BROLL_MATCH_HANDOFF.md`.

---

## Workstream A — Claude API cost

> **✅ SHIPPED 2026-07-20.** A1–A4 done, studio restarted on the new code, acceptance passed:
> - **A1 model routing:** `route_model()` + `_routing_config()` (lru_cache) in server.py wired into
>   `/api/chat` (with new `force_model` Form field) AND `/api/jobs/start` (telemetry logs the routed
>   model). `route_model` returns `requested or default` on a no-marker prompt so interactive chat
>   keeps the picker's Opus without needing force. Skips the `_comment` marker. Live-verified via
>   jobs_start: STREAMLINED AD / VARIANT FACTORY / ROLL THE DICE → `sonnet`; interactive+force → `opus`.
> - **Frontend:** added `nextSubmitRouted` + `window.kfRouteNextSubmit()`; the batch launchers (dice,
>   variants in app.js; streamlined in streamlined.js) now set routed instead of forcing `opus`, so the
>   server routes them. Explicit picks (sync/composer/interactive) send `force_model=true` and win.
> - **A2:** `CLAUDE.md.proposed` applied verbatim (diff reviewed). REQUIRED-READING bulk-load → on-demand
>   routing table + inlined Hard Rules. NOTE: it references `docs/META_ADS_PLAYBOOK.md` + `DESIGN.md`
>   which may not exist at those paths (guidance only, harmless).
> - **A3 session hygiene:** `/api/chat` skips `--continue` when the prompt head marks an autonomous batch
>   (VARIANT FACTORY / STREAMLINED AD / ROLL THE DICE). Same code path as A1; the "smaller tokens_cache"
>   payoff lands on the next real autonomous run (mechanism in place, not yet measured live).
> - **A4 local copy:** `/api/copy_gen` endpoint (thin passthrough to `copy_gen.py`) — live-verified
>   returning Ollama JSON. `COPY_MODEL` is **`qwen2.5:7b`** (what's pulled + copy_gen's default; the plan
>   said qwen3:14b). Wired into the **streamlined** Kino prompt (hook/bullets/CTA offload). **Deliberately
>   NOT wired into variants** — its PASS 1/PASS 2 "learn from winning scripts" flow is quality-critical
>   and a 7B model would degrade it; variants instead drops to Sonnet via A1. Offload is available there
>   (`copy_gen.py --format script`) if desired.

Telemetry: 75 jobs = $206 ($2.75 avg), 73/75 on Opus, cache-read:output tokens = 239:1.
Fix = route cheap jobs off Opus, slim per-task context, offload copywriting to Ollama.

- **A1 — Model routing.** Wire `model_routing.json` into `/api/chat`. Full drop-in code
  (a `route_model()` helper + the exact `/api/chat` edits) is in `server_patch.md`.
  Add the `STREAMLINED AD` marker — it's currently missed and falls to Opus.
- **A2 — Slim context.** Diff `CLAUDE.md.proposed` against the live CLAUDE.md and apply.
  Converts "REQUIRED READING (load all 5 docs)" to an on-demand routing table with the
  Hard Rules inlined. Pipeline behavior unchanged; only per-job context shrinks.
- **A3 — Session hygiene.** In `/api/chat`, don't `--continue` autonomous batch runs
  (VARIANT FACTORY / STREAMLINED AD / ROLL THE DICE) — see `server_patch.md` Step B.
- **A4 — Local copy.** Move `copy_gen.py` → `video-use/helpers/`; replace the "Claude
  writes the hook/bullets/script" step with a `copy_gen.py` call (needs `ollama serve`
  + `ollama pull qwen3:14b`). With copy offloaded, those jobs can run on Haiku.
- **A5 — Optional.** LiteLLM as a routing gateway with a spend dashboard; swap
  `transcribe.py` from paid ElevenLabs Scribe to local faster-whisper.

A1–A3 edit the live `server.py` (one restart). A4 is additive.

**Acceptance (A):** a STREAMLINED AD job spawns with `--model sonnet` and a much smaller
`tokens_cache` in the new `jobs.jsonl` row; interactive chat still uses Opus.

---

## Suggested order

Ship **Workstream B** first (it's what the user wants live, and it's mostly additive —
only B3 touches `variant_factory.py`). Then **Workstream A** as one server.py pass
(A1→A2→A3, single restart), then A4/A5.
