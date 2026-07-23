# Claude Veditor — Video Editing Studio

This directory is a Claude-powered video editing studio. The user drops source video into `videos/`, gives editing directions in plain English, and the assistant produces edited deliverables.

> **Context-cost rule (READ FIRST).** Telemetry showed jobs were loading ~27k tokens of
> philosophy docs on every task regardless of relevance, driving a 239:1 context-to-output
> ratio. Do **not** bulk-read the `docs/` philosophy files. Read only the one(s) the
> current operation maps to in the table below, and only when you're actually about to do
> that operation. The Hard Rules that used to live across those docs are inlined here so a
> job never needs to open a doc just to stay compliant.

## Stack

- **`video-use/`** — conversation-driven editing engine (Python). Transcription, silence removal, cuts, color grading, audio fades, subtitles. Skill registered at `~/.claude/skills/video-use`.
- **`hyperframes/`** — HTML/GSAP-to-MP4 motion graphics renderer (Node). Used via `npx hyperframes`.
- **`videos/`** — drop source files here. All edit outputs go in `videos/edit/`.
- **`docs/`** — philosophy documents that govern HOW edits are made. **Load on demand only — see the routing table.**

## Context routing — read a doc ONLY when doing its operation

| If the job involves… | Read (and only then) | Otherwise |
|---|---|---|
| Cutting a talking head / trimming / EDL | `docs/TRIMMING_PHILOSOPHY.md` | skip |
| A composite / final render / export mode | `docs/RENDER_EXPORT_PHILOSOPHY.md` | skip |
| Motion graphics / GSAP / hyperframes | `docs/MOTION_PHILOSOPHY.md` | skip |
| Veo / generative video | `docs/VEO_PROMPTING.md` | skip |
| Nano Banana / generative images | `docs/NANO_BANANA_PROMPTING.md` | skip |
| Ad strategy / angle / Meta placement | `docs/META_ADS_PLAYBOOK.md` | skip |

Pure copywriting jobs (variant factory, streamlined ad, hook/bullet/CTA writing) need
**none** of these — the Hard Rules below are sufficient. `DESIGN.md` is reference-only;
never auto-load it.

## Hard Rules (inlined — always apply, no doc read required)

**Trimming.** Word-boundary cuts, 30 ms fades. Stop-consonant tail-cap heuristic:
`NORMAL_MAX=0.25, LAST_CHAR_CAP=0.28, TAIL_PAD=0.08, HEAD_PAD=0.05`. Propose the cut
plan as a markdown table (`# | timestamps | translation`) and **wait for "approve"
before cutting.** Bad cuts are EDL bugs, not transcript bugs — never re-transcribe to
"fix" a cut. (Full rationale: TRIMMING_PHILOSOPHY.md — read only if cutting.)

**Render / export.** Two delivery modes (full composite vs. layered handoff). **Always
ask which mode + quote a wall-time estimate before any render.** Default to handoff for
> a few minutes of output. (Detail: RENDER_EXPORT_PHILOSOPHY.md — read only if rendering.)

**Motion graphics.** Every sub-comp ends with `tl.to({}, { duration: SLOT_DURATION }, 0)`
to prevent black-frame flashes. Hand-build hero beats; `npx hyperframes add <block>` for
supporting beats. (The 11 Laws + recipes: MOTION_PHILOSOPHY.md — read only if animating.)

**Generative AI cost gate.** Veo bills per second (~$1–3+/8s clip). Before generating
MORE THAN ONE clip, state clip count × duration × est. cost and wait for approval. One
exploratory clip is fine ungated. Nano Banana images are cents — iterate freely on Flash
tier; mention cost past 10 Pro-tier renders. Helpers: `video-use/helpers/veo_video.py`,
`nano_banana.py` (write `.gen.json` sidecars — never delete). Prompts MUST follow the two
prompting docs — read them only when generating.

**Subtitles burn LAST** — after every overlay, after cut lock, after motion composite.

**Preview gate.** For any multi-step pipeline (rotate/resize/captions/aspect/sync), render
a 10-second `--draft` preview, tell the user the path, and wait for "go" before the full
encode. Skip only if the user said "just run it" or the full render is under 30s wall time.

**sync_audio.py** defaults to `--auto-trim-head` (offset ≥ 0.5s). Output starts at
content-start with audio at t=0 — downstream tools use a clean 0-based timeline, no manual
offset. Sidecar at `<output>.sync.json`. Pass `--no-trim-head` only when explicitly asked.

## Model routing (cost)

Job type selects the model (see `studio/model_routing.json`): copywriting/deterministic
renders → Sonnet/Haiku; only genuine multi-step reasoning/review → Opus. When
`copy_gen.py` (local Ollama) is available, do copy generation there, not on Claude.
Don't `--continue` autonomous batch runs — they're self-contained; a fresh session avoids
re-caching the whole prior conversation.

## Session loop (the rhythm)

1. User drops a video into `videos/` and describes what they want.
2. Consult the context-routing table; read the one relevant doc **only if** the op needs it.
3. Transcribe → pack → propose a cut plan as a markdown table.
4. **Wait for "approve" before cutting.** No exceptions.
5. Render base cut. Iterate using word-level data (never global pad tweaks).
6. Before any composite render, ask Mode A vs B + quote wall time.
7. Motion: hand-build hero beats; `npx hyperframes add <block>` for supporting beats.
8. All artifacts in `videos/edit/`. Never write inside `video-use/` or `hyperframes/`.

## Environment

- `video-use/.env` — `ELEVENLABS_API_KEY`, `HEYGEN_API_KEY` set. `GEMINI_API_KEY` for Veo / Nano Banana.
- `OLLAMA_HOST` / `COPY_MODEL` — for local copy generation via `copy_gen.py`.
- FFmpeg, bun, uv installed via winget.
- Hyperframes: use `npx hyperframes` (local monorepo install fails on Windows symlinks).

## Workspace conventions

- `find . -name '._*' -delete && export COPYFILE_DISABLE=1` before any helper run (AppleDouble files break globs on exFAT).
- Transcripts cached per source byte-hash.
- Subtitles burn last.

## Hard rule — don't kill the Veditor Studio server while it's busy

You're likely running INSIDE the studio (`claude --print` subprocess streaming to
`http://127.0.0.1:8765`). After a `server.py` change that needs a reload: (1) check for
another running `claude.exe`/`ffmpeg.exe` (`Get-Process claude,ffmpeg` — don't count your
own parent); (2) if anything is running, do NOT restart — tell the user to Ctrl+C and
relaunch via `start-studio.bat` when it finishes; (3) if clear, stop the uvicorn PID on
port 8765 and `Start-Process studio/start-studio.bat`. Never `Stop-Process`/`taskkill`
uvicorn/python-in-`studio\.venv`/port-8765, nor `winget upgrade`/`pip install` studio deps,
nor flush `~/.claude/` while a job is active. Static-file edits (`studio/static/*`) need no
restart.

---
_Changed vs. previous CLAUDE.md: "REQUIRED READING — before any edit" (bulk-load all
philosophy docs) replaced by the on-demand routing table + inlined Hard Rules. This is
Task 2 of the cost plan. Behaviour of the actual pipelines is unchanged; only what gets
pulled into context per job changes._
