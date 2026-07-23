# NANO BANANA PROMPTING — how this studio prompts Gemini image models

Distilled from Google Cloud's **"Ultimate Nano Banana prompting guide"**
(Mar 2026) — full source PDF at
[docs/source/nano_banana_prompting_guide.pdf](source/nano_banana_prompting_guide.pdf).
Read this BEFORE writing any image-generation prompt.

Helper: `video-use/helpers/nano_banana.py` (`--list-models` if a model 404s).
Outputs land in `videos/edit/genai/<run>/` with a `.gen.json` provenance sidecar.

## Model cheat-sheet

| Nickname | API family | Use when |
|---|---|---|
| **Nano Banana Pro** | Gemini 3 Pro Image | default — hero shots, text rendering, 4K |
| **Nano Banana 2** | Gemini 3.1 Flash Image | volume/cheap: drafts, iterations, contact sheets |

Tech limits that matter: up to **14 reference images** per prompt · aspect
ratios 1:1, 3:2, 2:3, 3:4, 4:3, 4:5, 5:4, **9:16, 16:9**, 21:9 · sizes 1K/2K/4K
· sharp multilingual text rendering · all outputs carry a SynthID watermark +
C2PA credentials (fine for ads; know that it's there).

## Hard rules (studio-specific)

1. **Aspect must be stated** — default 9:16 for anything feeding the vertical
   ad pipeline, 16:9 for Veo landscape first-frames. Never let it default.
2. **Iterate conversationally, cheap-first.** Draft on the Flash-tier model,
   re-run the winner on Pro at 2K/4K. Images are cents, but batches of 4K Pro
   renders still add up — mention cost when generating >10.
3. **Positive framing.** Describe what you want ("empty street"), never what
   you don't ("no cars").
4. **Exact text in quotes.** Any words that must render in the image go in
   quotes with a font direction. Long copy: settle the wording in conversation
   FIRST, then ask for the image containing it (the "text-first hack").
5. **Provenance sidecars stay with the image** (`.gen.json`).

## The five frameworks

### 1 · Text-to-image (blank canvas)
Narrative description, not keyword soup:

**`[Subject] + [Action] + [Location/context] + [Composition] + [Style]`**

> "[Subject] A striking fashion model wearing a tailored brown dress, sleek
> boots, and holding a structured handbag. [Action] Posing with a confident,
> statuesque stance, slightly turned. [Location] A seamless, deep cherry red
> studio backdrop. [Composition] Medium-full shot, center-framed. [Style]
> Fashion magazine editorial, shot on medium-format analog film, pronounced
> grain, high saturation, cinematic lighting."

### 2 · Generation with references (consistency & compositing)
**`[Reference images] + [Relationship instruction] + [New scenario]`**

> "Using the attached napkin sketch as the structure and the attached fabric
> sample as the texture, transform this into a high-fidelity 3D armchair
> render. Place it in a sun-drenched, minimalist living room."

This is THE tool for character consistency and product integration: pass the
product photo / character still via `--image` (repeatable), state each image's
role explicitly, then describe the new scene.

### 3 · Editing (base image + change instruction)
Focus the prompt on **what changes and what stays the same**:
- Semantic masking by text: "Remove the man from the photo" — be explicit that
  everything else stays identical.
- Add elements: base image + object image + combine instruction.
- Style transfer: photo + "recreate this exact content as a Van Gogh-style painting."

### 4 · Text rendering & localization
- Quote the words: `"URBAN EXPLORER"`, `"10% OFF"`.
- Direct the type: "in a heavy, blocky Impact font", "thin minimalist Century
  Gothic", "bold white sans-serif".
- Localize: write the prompt in English, specify the target language for the
  rendered text.
- Effects: text-as-mask works ("bold letters spell 'New York' — a photo of the
  skyline visible ONLY inside the letterforms").

### 5 · Prompting like a Creative Director
Layer studio controls into any of the above:
- **Lighting**: "three-point softbox setup", "chiaroscuro, harsh high
  contrast", "golden hour backlighting, long shadows".
- **Camera/lens**: "shot on a GoPro" (immersive distortion), "Fujifilm color
  science", "cheap disposable camera flash aesthetic"; "low-angle, shallow
  depth of field (f/1.8)", "wide-angle", "macro".
- **Grade/film stock**: "1980s color film, slightly grainy", "cinematic muted
  teal grade".
- **Materiality**: never "a suit" — "navy blue tweed"; never "armor" — "ornate
  elven plate, etched silver leaf". Name surfaces on mockups ("minimalist
  ceramic coffee mug").

## How it chains with Veo (the studio pipeline)

1. **Character pipeline**: Nano Banana still (iterate until approved) → Veo
   `--image` first-frame / ingredients → consistent character across clips.
2. **Product pipeline**: reference photo → NB studio hero shot → Veo camera
   choreography around it (see VEO_PROMPTING.md § Product shots).
3. **Keyframe direction**: generate first and last frames with NB, let Veo
   bridge them (epic transitions).
