# VEO PROMPTING — how this studio prompts Google Veo

Distilled from Google's **"Veo for Ads: Prompting Guide"** (Nov 2025) — the full
source PDF lives at [docs/source/veo_for_ads-prompting_guide.pdf](source/veo_for_ads-prompting_guide.pdf).
Read this BEFORE writing any Veo prompt, the same way TRIMMING_PHILOSOPHY governs cuts.

Helper: `video-use/helpers/veo_video.py` (`--list-models` if a model 404s).
Outputs land in `videos/edit/genai/<run>/` with a `.gen.json` provenance sidecar.

---

## Hard rules (studio-specific)

1. **Cost gate.** Veo bills per second (~$0.15/s fast tier, ~$0.40+/s standard —
   verify current pricing). Before ANY batch (>1 clip), state clip count ×
   estimated cost and wait for approval. One exploratory clip is fine ungated.
2. **Never one-line prompts.** Every prompt uses the full 7-part anatomy below.
   Thin prompts produce generic stock-footage output — wasted spend.
3. **Aspect default is 9:16** (Meta/vertical, matches the variants pipeline);
   16:9 only when the deliverable calls for it.
4. **Character or product consistency = Nano Banana first.** Generate the still
   with `nano_banana.py` (see NANO_BANANA_PROMPTING.md), then feed it to Veo as
   `--image` (first-frame conditioning). Do not try to keep a character
   consistent across clips through text alone — it will drift.
5. **No identifiable minors** — Veo safety filters block children; don't prompt
   for them.
6. **Provenance sidecars stay with the clip.** Never delete `.gen.json` files;
   the timeline and Manager rely on them for auditability.

---

## The 7-part prompt anatomy (use ALL seven, in this order)

**Shot Type → Action → Setting → Character(s) → Camera Movement → Style → Audio**

Canonical example (running-shoes ad from the guide):

> "Exterior / Low-Angle Tracking Shot. A runner sprints through a city at dawn.
> A downtown city street, just before sunrise. The pavement is damp and
> reflective from a recent rain. Modern glass-and-steel skyscrapers line the
> empty street. Steam rises from a manhole cover, and the first golden light of
> dawn is just beginning to hit the tops of the buildings. An athletic woman in
> her late 20s, running with intense focus and fluid motion. She wears
> minimalist black running gear, making her brand-new, brightly-colored running
> shoes the focal point. Her breath mists in the cool morning air. A fast,
> smooth, low-angle tracking shot that moves perfectly alongside the runner. The
> camera is positioned just above the pavement, keeping the new running shoes in
> the center of the frame as they pound the wet ground, kicking up small,
> cinematic splashes of water. High-energy, polished, and motivational. The
> color palette is clean and cool (steely blues and deep greys), which makes the
> vibrant color of the running shoes pop dramatically. Anamorphic lens flares
> streak horizontally from streetlights. Hyper-realistic and rhythmic. The
> dominant sound is the sharp, cushioned thud-thud-thud of the running shoes on
> the pavement. The runner's steady, powerful breathing. A deep, driving
> electronic beat begins to swell, building energy and intensity."

### 1 · Shot Type
Low-angle (powerful) · high-angle (small/vulnerable) · bird's-eye · close-up
(emotion/detail) · extreme close-up · medium (dialogue, waist-up) · wide /
establishing · over-the-shoulder · POV.

### 2 · Action
Short and simple — one fundamental thing happening. Movements, interactions,
emotional expressions, subtle motion (breeze, tapping fingers), transformations
(flower blooming). Detail comes from the other six parts, not from stacking actions.

### 3 · Setting
Ground the subject with sensory language: interior/exterior location, time of
day, weather, era, atmospheric details (dust motes in a sunbeam, heat haze,
reflections on wet pavement).

### 4 · Character(s)
Specificity kills generic output. Appearance, age, wardrobe, voice, demeanor —
described like a costume designer briefing. Multiple subjects are fine.
Dialogue goes in quotes and lip-syncs; add a target language and Veo
translates + lip-syncs it ("…says in Spanish, 'The best way to start your day!'").

### 5 · Camera Movement
Static · pan · dolly in/out · zoom in/out · crane · aerial/drone ·
handheld/shaky (realism, urgency) · whip pan · arc shot. One clear movement per
clip; say where the camera sits and what it keeps in frame.

### 6 · Style
Artistic register ("cinematic, shot on 35mm", "claymation", "gritty graphic
novel"), lighting design ("golden hour glow", "film noir deep shadows",
"volumetric rays"), color palette ("steely blues so the product pops"), plus
mood words (high-energy, contemplative). End-of-prompt control tokens the guide
uses: `(no cuts) (single continuous shot) (no music)`.

### 7 · Audio
Veo generates audio — direct it explicitly or you get a random bed:
- **SFX**: "distinct sizzle", "crunchy, sugary typing sounds"
- **Ambient**: "city traffic and distant sirens", "quiet office hum"
- **Dialogue**: quoted lines per character; VO with accent/tone notes
- **Music**: "a deep, driving electronic beat begins to swell" — or `(no music)`
  when the studio will lay its own bed (usual case: our Music/SFX pipeline
  handles the bed, so default to `(no music)` unless told otherwise).

---

## Workflows

### Text-to-video
Full anatomy prompt → `veo_video.py --prompt "..." --output clip.mp4`.

### Image-to-video (first frame)
`--image frame.png` + prompt describing the motion from that frame. Use for:
brand stills, Nano Banana character/product shots, last-frame chaining
(extend a clip by feeding its final frame back as the next clip's first frame —
grab it with `ffmpeg -sseof -0.05 -i clip.mp4 -frames:v 1 last.png`).

### Subject reference (the API-legal avatar / HeyGen-alternative path)
`--reference-image photo.png` (repeatable, up to 3 images of ONE subject) —
Veo 3.1 preserves that person/character/product's appearance in the output,
with lip-synced generated dialogue (quote the line in the prompt). Notes:
- Flow's `@me` avatars have **no API** (account-locked by design) — this is
  the equivalent for automated work.
- The generated VOICE is synthetic, not a clone. For the user's real voice:
  prompt `(no dialogue)` and mux an ElevenLabs cloned-voice VO instead
  (lip-sync is lost), or accept the generated voice for spokesperson-style clips.
- People in reference modes require `personGeneration: allow_adult` (adults only).

### Product shots (the ads workflow)
1. Nano Banana: product reference photo → clean studio hero still.
2. Veo with that still as `--image`, prompt = camera choreography around the
   product ("orbiting fluidly… sweeps low past the base, glides upward along
   the curve… capturing edge flares, bevels").
3. The more product-surface detail in the prompt, the better it holds up.

### A/B variants
Write ONE master prompt, then swap slot values only — `<age & sex>`,
`<gear color>`, `<shoe color>` — keeping everything else identical. This is the
Veo analogue of the variants factory's angle-diverse scripts: comparable
results, isolated variables.

### Template for long-form control (from the guide's template section)
For maximum control write the prompt as labeled blocks:

```
Time of Day – …
Interior/Exterior + Shot Type – …
Basic Scene – …  (what happens, beat by beat, natural micro-movements)
Detailed Environment / Location – …
Detailed Character (1) – …  (head-to-toe, materials, textures)
Camera Movement – …
Cinematic Look & Style – …  (film stock, palette, grain, depth of field)
Sound Design – …
(no cuts) (single continuous shot) (no music)
```

---

## Prompt-writing help

Kino IS the Gemini-style prompt expander from the guide: when the user gives a
thin idea ("woman running, focus on the shoes"), expand it through the full
anatomy before generating — and show the expanded prompt for approval when the
run is gated (batches, or anything the user will pay real money for).
