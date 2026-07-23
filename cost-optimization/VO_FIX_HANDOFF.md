# Fix: garbled/"non-human" VO tail on variants

## Root cause (evidenced, not guessed)
The July 21 variant's TTS input (`variant_1.mp4.timeline.json → script_text`) contained
non-ASCII "unspeakable tokens": a **®** ("Fibrex®") and **em-dashes** ("— plus").
ElevenLabs slurs/glitches on these — the caption timing shows a ~5-second smear across
the offer line (16.1s→21.2s) where the ® + dash cluster sits. It was **not** a TTS
backup (voice_id confirms ElevenLabs) and **not** a stretch/pad artifact (VO 25.39s ≈
video 25.33s). The Kino prompt's "scrub unspeakable symbols" step is a SOFT instruction;
Opus tended to honor it, Sonnet let it through. Fix deterministically so it's
model-independent — no need to move variants back to Opus.

## The fix — `vo_sanitize.py` (staged, tested)
`sanitize_for_tts(text)` removes/normalizes ® ™ © em/en-dashes … smart-quotes emoji &
% + and other symbols; keeps sentence punctuation; converts dashes to comma pauses.
Verified on the actual failing script → clean ASCII, natural pauses, clean copy unchanged.

## Wire-in (Claude Code)
`video-use/` is edit-restricted for the assistant that wrote this, so apply it:

1. Copy `cost-optimization/vo_sanitize.py` → `video-use/helpers/vo_sanitize.py`.
2. In `video-use/helpers/tts_voice.py`, inside `synthesize()`, immediately before the
   payload is built (the `"text": text,` line ~96):
   ```python
   from vo_sanitize import sanitize_for_tts   # helpers/ is already on sys.path
   text = sanitize_for_tts(text)
   ```
   This is the single choke point for ALL VO (variants, streamlined, etc.).

## HARD constraint — VO only, NOT captions
CLAUDE.md requires **Fibrex®** (with ®) in on-screen caption text for brand/legal reasons.
Apply the scrub ONLY to the spoken text inside `tts_voice.synthesize`. Do NOT run it over
the caption/ASS/SRT builders — those must keep ® and original formatting. The wire-in
point above is correct because captions are built separately from the VO transcript.

## Acceptance
- `python video-use/helpers/vo_sanitize.py "Fibrex® off — now"` → `Fibrex off, now`
- Re-run a variant whose copy mentions Fibrex / uses an offer line; confirm the VO around
  the offer is clean, and the burned caption still shows **Fibrex®**.

## Unrelated finding from the same run (FYI)
`timeline.json → broll_match: "random"` — this variant used random b-roll, not semantic.
The Semantic toggle in the Variants modal wasn't on (or defaulted to random). Not a
defect; just flag to the user if they expected semantic matching.
