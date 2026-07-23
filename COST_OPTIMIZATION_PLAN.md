# Veditor Studio — Claude Token/Cost Optimization Plan

_Generated 2026-07-20. Goal: cut Claude API spend per task._

## What the telemetry actually says

Pulled from `videos/edit/jobs.jsonl` (75 completed jobs):

| Metric | Value |
|---|---|
| Total Claude spend | **$206.15** |
| Avg cost per job | **$2.75** |
| Jobs on Opus | **73 of 75** |
| Total cache-read tokens | **183,894,020** |
| Total output tokens | **770,265** |
| Context-to-output ratio | **239 : 1** |

**The finding in one sentence:** you're paying Opus prices to re-read ~2–4M tokens of context on every job (one hit 10.1M), while the actual work — writing a few ad bullets or a 63-word script — produces a few thousand output tokens. Context, not thinking, is the bill.

Concrete examples from the log:
- A "write 3 selling-point bullets + run one python command" job: **$3.51**, 2.6M cache tokens.
- Another bullets job: **$4.51**, 4.2M cache tokens.
- The Endurance full-video job: **$6.10**, 10.2M cache tokens.

None of these are hard reasoning tasks. They're cheap text generation wearing an Opus-sized context coat.

## Root causes (ranked by $ impact)

1. **Everything runs on Opus.** `server.py` passes whatever model is set, and 73/75 jobs landed on Opus. Opus cache-reads cost ~5× Sonnet and ~15× Haiku. Your own dashboard already flags this (`opus_heavy` finding in `server.py`).
2. **The whole knowledge base loads on every task.** CLAUDE.md forces "REQUIRED READING" of 5 philosophy docs (~20k words / ~27k tokens) *before any edit*, plus SKILL.md (3k words) and DESIGN.md (5k words). A "write 3 bullets" job does not need MOTION_PHILOSOPHY.md — but it pays to load it every time, and again on every `--continue` turn.
3. **`--continue` is the default.** `/api/chat` appends `--continue` unless told otherwise, so batch jobs inherit and re-cache the entire prior conversation. That's how a bullets job reaches 2–4M cached tokens and Endurance reaches 10M.

## The plan

### Tier 1 — biggest cut, no new software (do these first)

**1a. Model routing by job type.** Route in `server.py` before spawning the CLI:
- Pure copywriting / bullets / hooks / script drafting → **Sonnet** (or Haiku for one-liners).
- Genuine multi-step reasoning, review, or motion-graphics design → **Opus**.

Rough math: 184M cache tokens at Opus (~$1.50/M read) ≈ the bulk of the $206. The same reads on Sonnet (~$0.30/M) land near **$37** — a **~70–80% cut** on the copy-heavy jobs alone, changing nothing about output quality for text generation.

**1b. Slim the per-task context.** Stop force-loading all 5 philosophy docs + DESIGN.md on every job. Load them on demand: a cut job reads TRIMMING_PHILOSOPHY, a motion job reads MOTION_PHILOSOPHY, a copy job reads neither. This attacks the 239:1 ratio directly and compounds with 1a (smaller context × cheaper model).

**1c. Don't `--continue` batch jobs.** Variant-factory / streamlined-ad runs are self-contained and pre-authorized — give them a fresh session with a minimal prompt instead of inheriting conversation history.

### Tier 2 — Ollama (your instinct was right)

Run a local model for the text-generation jobs that currently burn Opus for pennies of real output: variant angles, hook lines, bullet copy, script drafts. Serve on `localhost:11434`, OpenAI-compatible, **$0/token**.

- Recommended model for marketing copy on a consumer GPU: **Qwen3 14B** (or **Phi-4 14B** on 12GB VRAM). A strong system prompt matters more than model size here.
- Fit: `variant_factory.py` and `streamlined_ad.py` copy steps are the prime candidates. Claude Code stays the orchestrator; it just stops doing the wordsmithing.
- Realistic effect: the recurring $2–4.50 copy jobs drop toward ~$0 in model cost.

### Tier 3 — supporting free/OSS repos

- **LiteLLM** (BerriAI, open source): a proxy that speaks the OpenAI API and routes across Ollama + Claude with per-request cost tracking and budget-based fallback. This is the clean way to implement Tier 1a + Tier 2 without hand-rolling routing logic in `server.py`. It also gives you a real spend dashboard.
- **faster-whisper / WhisperX** (local transcription): `transcribe.py` currently calls the paid **ElevenLabs Scribe** API. Local Whisper on your NVENC GPU is free and offline. Doesn't touch Claude tokens, but cuts overall API spend and removes a network dependency.

### On your other two candidates

- **Langflow** — a visual LLM-flow builder. You already have a working Python orchestrator (`server.py`), so Langflow mostly adds a second server to babysit and a dependency, without cutting token cost. Skip unless you specifically want a drag-and-drop pipeline UI for non-developers.
- **Wispr Flow** — voice dictation. Nice input convenience (dictate editing directions), but it's a paid product and doesn't reduce Claude token usage. Orthogonal to the cost goal; treat as a "nice to have," not a saver.

## Split: implement here vs. delegate to Claude Code

**I can implement here (low-risk, isolated):**
- This plan document.
- A drop-in `copy_gen` helper that calls a local Ollama endpoint for copy, matching the existing `helpers/` pattern — so the copy step has a non-Claude path ready to wire in. _(Pending your OK, since it adds a file to the engine.)_
- A model-routing map (job-type → model) as a config file `server.py` can read.

**Delegate to Claude Code (touches the live 112KB `server.py` + needs the restart dance in CLAUDE.md):**
- Wire the routing map into `/api/chat` so job type selects the model.
- Refactor CLAUDE.md "REQUIRED READING" from always-load to on-demand/skill-routed loading.
- Make batch jobs run fresh instead of `--continue`.
- Optional: stand up LiteLLM as the gateway and point `CLAUDE_BIN` traffic through it; swap `transcribe.py` to faster-whisper.

## Expected outcome

Tier 1 alone should take the ~$2.75/job average down substantially (the copy-heavy jobs are where the money is). Tier 2 pushes copy-generation model cost toward zero. Rough target: **well over half** of the current Claude spend, most of it from routing + context slimming before any new repo is installed.

## Sources
- [Best Ollama Models 2026 — Local AI Master](https://localaimaster.com/blog/best-ollama-models)
- [Best LLMs for Creative Writing Locally 2026](https://llmhardware.io/guides/best-llm-for-writing-locally)
- [LiteLLM (BerriAI) — GitHub](https://github.com/BerriAI/litellm)
- [5 ways to cut Claude Code costs with LiteLLM](https://docs.litellm.ai/blog/save-claude-code-costs-with-litellm)
- [Run Claude Code with local agents using LiteLLM and Ollama](https://medium.com/@kamilmatejuk/run-claude-code-with-local-agents-using-litellm-and-ollama-ab88869cbd00)
