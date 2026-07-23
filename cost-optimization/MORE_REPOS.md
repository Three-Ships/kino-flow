# More free/OSS repos that help Veditor (video-creation tool)

Beyond the cost work. These are grouped by what they'd add and by how well they fit
your existing pipeline. "Fit" is my read of how cleanly it slots into what you already
have (`video-use/helpers/*`, ffmpeg/NVENC, ElevenLabs, Veo/Nano Banana).

## Tier 1 — direct cost cutters (replace paid APIs you already call)

**Local TTS to reduce ElevenLabs spend.** You call ElevenLabs for VO on every variant.
Local TTS is free and offline; keep ElevenLabs for the hero voice if you like the quality.
- **Kokoro-82M** (Apache-2.0) — tiny, fast, runs on CPU or ~2–3GB VRAM, 54 voices. Best
  default for narration. **Commercial-safe license.**
- **Chatterbox** (Resemble AI, MIT) — in blind tests, listeners preferred it over
  ElevenLabs 65% vs 25%. Best quality, commercial-safe.
- **Piper** (MIT) — fastest fully-offline, 30+ languages, CLI+Python. Good batch fallback.
- **XTTS v2 / Coqui** — best voice cloning from a ~6s sample, but weights are
  **non-commercial** — don't ship it in a paid product; fine for internal tests.
- Fit: high. Wraps behind a `tts_local.py` helper mirroring `tts_voice.py`.

**Local transcription to replace ElevenLabs Scribe** (already in the main plan):
- **faster-whisper** / **WhisperX** — GPU-accelerated, word-level timestamps (WhisperX adds
  forced alignment + diarization, ideal for your word-boundary cuts). Free, offline.
- Fit: high. `transcribe.py` is a thin API wrapper today; swap the backend, keep the cache.

## Tier 2 — new capabilities for a short-form ad tool

**Auto-reframe 16:9 → 9:16 with subject tracking.** You output 9:16 ads; this automates
the crop so the speaker/product stays in frame.
- **auto-vertical-reframe** (KazKozDev) — scene-aware, YOLOv11 + ByteTrack, tracks people/
  pets/vehicles through cuts. CLI, drops into an ffmpeg pipeline.
- **Clipify** — notably, a **Claude Code skill** already: transcribe → find moments →
  reframe with face-tracking → burn opus-style captions. Closest to your world; worth
  reading even just for approach.
- **opensource-clipping** (NaufalRizqullah) — face-tracking, karaoke subs, B-roll, BGM
  ducking, auto-thumbnails (Whisper + MediaPipe/YOLO).
- Fit: medium-high. These overlap your `broll_overlay`/`hook_overlay`; cherry-pick the
  reframe + active-speaker crop rather than adopting a whole app.

**Scene / shot detection.**
- **PySceneDetect** — content-aware shot-change detection; auto-split into clips. Useful for
  auto-selecting B-roll cutaways and for your `best_take`/`match_pairs` logic.
- Fit: high. Pure library, one dependency.

**Karaoke / word-by-word captions.** Your `caption_render.py` burns ASS already; if you
want the animated word-highlight "opus-clip" look, the Clipify/opensource-clipping caption
renderers are reference implementations (big bold, yellow active word).
- Fit: medium — you have a caption renderer; this is a style upgrade, not a replacement.

## Tier 3 — orchestration / assembly (evaluate, don't rush)

**MoviePy 2.x** — programmatic compositing/transitions/text in Python. You're mostly raw
ffmpeg today (faster, more control). MoviePy is nice for rapid prototyping of new overlay
formats, but don't rip out working ffmpeg paths for it. Fit: low-medium.

**LiteLLM** (from the cost plan) — the gateway for routing Claude ↔ Ollama with a spend
dashboard. Fit: high for the cost goal.

## Deliberately skipping / cautioning

- **Langflow** — visual LLM-flow builder; you already orchestrate in `server.py`. Adds a
  server to run and doesn't cut cost. Only worth it if you want a no-code pipeline UI.
- **Wispr Flow** — voice dictation for entering directions. Paid, convenience only, doesn't
  reduce tokens. Nice-to-have, not a saver.
- Whole "opus-clip clone" apps (OpenCut-AI, etc.) — great to mine for techniques, but
  adopting an entire app conflicts with your tight helper-per-operation architecture. Lift
  the specific module (reframe, caption style), not the framework.

## Suggested adoption order

1. **faster-whisper/WhisperX** (transcription) and **Kokoro/Chatterbox** (TTS) — biggest
   recurring-cost cuts, both slot behind existing helper shapes.
2. **PySceneDetect** + **auto-vertical-reframe** — genuine new capability for 9:16 ads.
3. **LiteLLM** — once you want central routing + a spend dashboard.
4. Everything else: mine for code, adopt selectively.

## Sources
- [Best Local TTS Models 2026 — Local AI Master](https://localaimaster.com/blog/best-local-tts-models)
- [Best Open-Source TTS 2026: Chatterbox beats ElevenLabs — FindSkill](https://findskill.ai/blog/best-open-source-tts-2026/)
- [Local TTS & Voice Cloning Licenses 2026 — PromptQuorum](https://www.promptquorum.com/power-local-llm/local-tts-voice-cloning-piper-coqui-xtts)
- [PySceneDetect](https://www.scenedetect.com/) · [GitHub](https://github.com/Breakthrough/PySceneDetect)
- [MoviePy — GitHub](https://github.com/zulko/moviepy)
- [Clipify (Claude Code skill) — GitHub](https://github.com/louisedesadeleer/clipify)
- [opensource-clipping — GitHub](https://github.com/NaufalRizqullah/opensource-clipping)
- [auto-vertical-reframe — GitHub](https://github.com/KazKozDev/auto-vertical-reframe)
- [OpenCut-AI — GitHub](https://github.com/Ekaanth/OpenCut-AI)
