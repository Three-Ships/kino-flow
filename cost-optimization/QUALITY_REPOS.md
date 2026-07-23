# Quality-boosting OSS repos for Veditor

Repos that raise the **output quality** of the ads, mapped to where they slot into your
existing pipeline (`video-use/helpers/*`, hyperframes, Veo/Nano Banana, ElevenLabs).
"Fit" = how cleanly it drops into what you already have. Ranked within each stage.

---

## 1. Audio quality — highest perceived-quality-per-effort

Audience judges an ad's production value on audio more than almost anything, and this is
the cheapest lift you have.

- **DeepFilterNet3** (real-time neural speech denoise) — strip room noise/hiss from VO and
  talking-head takes. Beats RNNoise on generalization; runs CPU-only. Wrap behind a
  `clean_voice.py` step before leveling. **Fit: high.**
- **FFmpeg `loudnorm` (EBU R128)** — you already level with `level_audio.py`, but true
  loudness-normalize the final mix to **-14 LUFS** (the social/YouTube target; -16 for
  quieter platforms). Makes every variant land at consistent, platform-correct loudness.
  **Fit: trivial — one filter, no new dependency.**
- **Demucs** (Meta, stem separation) — isolate vocals from a reference track, or pull music
  off a source clip so you can re-bed it cleanly. Also lets you duck music under VO with a
  real sidechain instead of a static gain. **Fit: medium.**
- **RNNoise** — lighter CPU-only fallback where DeepFilterNet is overkill (batch runs).

## 2. Generative consistency — makes AI assets look on-brand, not generic

Your Veo/Nano Banana output is the biggest quality variance in the app. These lock
identity/brand so generated shots match each other and the real footage.

- **FLUX.2** (Black Forest Labs) — current open-source quality benchmark; takes **up to 10
  reference images** in one generation and preserves product appearance, character identity,
  and style across scenes. This is the single biggest upgrade for branded/product shots vs.
  a thin text prompt. **Fit: high — a stronger backend for the image step.**
- **IP-Adapter** — style/subject transfer from reference images with no fine-tuning. Feed
  your brand's real product photo and get on-brand generations. **Fit: high.**
- **ControlNet** (OpenPose/depth/canny) — force composition/posture so a product sits where
  you want and a spokesperson holds a consistent pose across frames. **Fit: medium.**
- **PuLID / InstantID** — lock a specific face across every generated frame (recurring
  spokesperson). **Fit: medium.** Pro stack people use: low-strength LoRA + PuLID + ControlNet.

## 3. Footage & generative-clip enhancement — resolution/motion polish

- **Real-ESRGAN** (upscaling) — the practical win here isn't your Canon footage, it's
  **upscaling Veo/genAI clips (often 720p) to match your hero 4K footage** so cutaways don't
  look soft. **Fit: high — targeted use on genAI output.**
- **RIFE** (frame interpolation) — smooth slow-motion on B-roll and clean 24→60fps for
  punchy motion. **Fit: medium.**
- **Video2X** / **REAL-Video-Enhancer** — wrappers that orchestrate Real-ESRGAN + RIFE +
  denoise through one CLI (frame-split via ffmpeg, reassemble with audio, scene-change
  aware). Easiest way to adopt both without wiring the models yourself. **Fit: high (CLI).**

## 4. Subject/background control — compositing quality

- **Robust Video Matting (RVM)** — temporally-consistent foreground/background separation
  (no green screen needed). Put a talking head cleanly over branded B-roll, or blur/replace
  distracting backgrounds. Temporal guidance avoids the frame-by-frame edge jitter that
  makes composites look cheap. **Fit: high — new capability for `broll_overlay`.**
- **BiRefNet** (high-res image matting) — the stills counterpart, with a Matting variant for
  soft edges (hair). Use on product/hook cards. **Fit: high.**
- (`rembg` is easier but edges jitter on motion — fine for stills, not video.)

## 5. Editing intelligence — smarter automatic decisions

- **CLIP-based semantic B-roll matching** — embed your B-roll library + the VO script and
  auto-pick the cutaway that actually matches what's being said, instead of guessing. Pair
  with a small vector index. Turns `match_pairs.py`/`broll_overlay.py` from keyword/manual
  into semantic. **Fit: medium-high — real intelligence upgrade.**
- **PySceneDetect** — content-aware shot detection to auto-segment B-roll into usable clips
  and pick clean cut points. **Fit: high, one dependency.**
- **librosa** / **madmom** (beat detection) — detect music beats and snap cuts + your
  streamlined-ad bullet/graphic pops to the beat. Beat-aligned motion reads as
  professionally edited. **Fit: high — big feel upgrade for the music-bed formats.**

## 6. Motion graphics — raise the ceiling above hyperframes

You're on hyperframes (HTML/GSAP). Options if you want more power for hero beats:

- **Remotion** — make videos with React; arbitrary JS logic per frame, deterministic
  rendering, huge ecosystem, built for thousands of data-driven variations (exactly your
  variant-factory use case). More capable than HTML/GSAP for complex hero animation, at the
  cost of a React build step. **Fit: medium — evaluate for hero beats, keep hyperframes for
  the rest.**
- **Lottie / LottieFiles** — drop professionally-designed vector animations straight into
  hyperframes (it already supports Lottie). Fastest way to add polished transitions/icons
  without hand-animating. **Fit: high — asset library, not a framework change.**
- **Revideo** (OSS, closest to Remotion) / **Twick** (MIT, timeline-native React) /
  **Motion Canvas** — alternatives if you want code-first with a timeline model.

## 7. Automated quality QA — catch bad renders before they ship

- **VMAF** (Netflix perceptual quality metric) — score each render and flag encodes that
  dropped quality (bad bitrate, artifacts) before delivery. Add as a post-render gate in the
  studio's existing findings/telemetry panel. **Fit: high — pairs with your jobs dashboard.**

---

## Recommended adoption order (quality-first)

1. **Audio**: DeepFilterNet3 + `loudnorm` to -14 LUFS. Cheapest, most audible upgrade. Ship first.
2. **Generative consistency**: FLUX.2 + IP-Adapter for on-brand product/character shots — the biggest variance in current output.
3. **Enhancement**: Real-ESRGAN on genAI clips (via Video2X) so cutaways match hero 4K.
4. **Matting**: Robust Video Matting for clean composites.
5. **Editing intelligence**: beat-sync (librosa) + CLIP B-roll matching + PySceneDetect.
6. **Motion**: Lottie assets now; evaluate Remotion for hero beats later.
7. **QA**: VMAF render gate.

Each is a self-contained helper you (or Claude Code) can wrap in the `video-use/helpers/*`
pattern — no framework rewrite required. I can stage any of these as a drop-in helper the
way I did `copy_gen.py`; say which and I'll build it.

## Sources
- [DeepFilterNet3 setup 2026](https://aiadoptionagency.com/deepfilternet-3-noise-suppression/) · [DeepFilterNet vs RNNoise](https://noisereducerai.com/blogs/deepfilternet-ai-noise-reduction/)
- [Demucs / audio denoise guide](https://vife.ai/blog/ai-noise-reduction-practical-audio-denoising-guide)
- [Best open-source image models 2026 (FLUX.2)](https://www.bentoml.com/blog/a-guide-to-open-source-image-generation-models) · [Consistent characters 2026](https://thinkpeak.ai/best-loras-consistent-characters-2026/)
- [Video2X review](https://www.aiarty.com/ai-video-enhancer/video2x.htm) · [REAL-Video-Enhancer](https://github.com/TNTwise/REAL-Video-Enhancer)
- [Robust Video Matting / BiRefNet roundup](https://aidailyshot.com/blog/top-open-source-ai-video-background-generators-2026)
- [Semantic video search with CLIP](https://docs.vultr.com/semantic-video-frame-search-using-openai-clip-and-vector-database) · [PySceneDetect](https://www.scenedetect.com/)
- [Remotion vs Motion Canvas vs Revideo 2026](https://www.pkgpulse.com/guides/remotion-vs-motion-canvas-vs-revideo-programmatic-video-2026) · [Remotion](https://www.remotion.dev/)
