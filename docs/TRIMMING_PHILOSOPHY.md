TRIMMING PHILOSOPHY — How to cut a talking head, deterministically
Required reading before any cut on this workspace. Pulled from the lessons of the first intro video (Thai, ~3min raw → 37.2s final). Re-read every time.

The end-state we want: every kept segment starts on a real consonant onset and ends just after the final consonant releases, with no audible breath/hiss trailing, no syllable clipped at either edge, and a smooth 30 ms fade across the join. Anything else is wrong.

0 · The Hard Rules (non-negotiable)
These exist because deviating produces silent failures or broken output.

Word-boundary cuts only. Snap every cut edge to a word from the Scribe transcript. Never cut inside a word.
30 ms audio fades at every segment boundary (afade=t=in:st=0:d=0.03, afade=t=out:st={dur-0.03}:d=0.03). No exceptions — without them every cut pops.
Per-segment extract → lossless -c copy concat. Never single-pass filtergraph the whole thing — that double-encodes every segment when overlays are added later.
Verbatim word-level ASR only. Never SRT/phrase mode. We need sub-second gap data and per-character timestamps for the heuristics below.
Cache transcripts per source. Never re-transcribe unless the source byte-changed. Scribe credits are real money.
Strategy confirmation before execution. Write the plain-English cut plan, present it as a table of timestamps + translations, wait for the user to approve. Never start cutting until "approve" is in chat.
All session outputs in <videos_dir>/edit/. Never write inside a skill directory.
1 · The mental model
A Thai (or English) talking head usually contains four kinds of material:

Material	Action
Clean takes (the speaker said the line and meant it)	Keep
False starts (1–2 syllables then restart, often a duplicate prefix)	Drop, keep the longer/last variant
Fillers / retakes (full alt versions of the same line)	Drop the worse one
Dead air (silence > 500ms while reading the script)	Drop
The pattern in takes_packed.md is almost always (short prefix)(longer prefix)(complete take). The complete take always wins.

2 · Building the EDL (the only step that matters)
2.1 Find your candidate ranges
Open takes_packed.md. It groups characters into phrases on silences ≥ 0.5s. Each phrase is a candidate clean take. The duplicates and stutters fall out by inspection.

2.2 Tighten head and tail using word-level timestamps — NOT phrase boundaries
This is the crucial trick. The phrase end Scribe gives you is wrong. Specifically:

The last character of a phrase ending in a stop consonant (Thai: บ ด ก ป / English: any plosive) is reported with an inflated end time that bleeds into trailing silence. We measured บ durations of 2–9 seconds — almost all of it silence.
The first character at the start of a phrase is similarly inflated when the speaker breathes/pauses before speaking. Vowels like เ show 0.8s when the actual onset is 0.05s of sound.
Algorithm (apply per kept phrase):

NORMAL_MAX = 0.25      # any char duration > this is silence-inflated
LAST_CHAR_CAP = 0.28   # let the final consonant sound for up to 280ms
TAIL_PAD = 0.08        # natural release pad
HEAD_PAD = 0.05        # safety pad before first onset

first_word, last_word = phrase.first_word, phrase.last_word
last_dur = last_word.end - last_word.start
real_end = (last_word.start + LAST_CHAR_CAP) if last_dur > NORMAL_MAX else last_word.end
tight_end = real_end + TAIL_PAD
tight_start = max(0.0, first_word.start - HEAD_PAD)
This single heuristic cut our example video from a baggy 73s to a punchy 37s without clipping a single syllable.

2.3 Pads — the working window
Allowed pad range: 30–200 ms. Tighter for fast-paced edit, looser for cinematic.

HEAD_PAD = 30–50 ms is the floor. Below that and Scribe drift will clip the leading consonant on some segments.
TAIL_PAD = 50–100 ms is normal; bump to 150 ms only on a hold/breath beat.
For stop-consonant final (most Thai phrases), the consonant itself eats up to 280 ms, so the EFFECTIVE post-pad after sound stops is LAST_CHAR_CAP - real_consonant_duration + TAIL_PAD ≈ 100–150ms. That feels right.
2.4 Fix specific gap complaints by inspecting word-level data
When the user says "the gap is too long after X" or "too long before Y", do this — never guess by ear:

# Extract every char in the suspect window
[w for w in words if W_START <= w['start'] < W_END]
Then read the dur = end - start of each. The single character with dur > 0.5s is the silence ghost. Its real spoken duration is ≤ 250ms. Snap the cut accordingly.

Real examples from the intro video:

Seg 11 last char ด: 177.00 → 180.12 (3.12s of silence). Real end ~177.10.
Seg 12 last char บ: 184.64 → 186.98 (2.34s). Real end ~184.78.
Seg 13 first char เ: 188.96 → 189.80 (840ms before any sound). Real onset ~189.70.
2.5 Stutter trim (in-segment cleanup)
If the speaker stutters at the start of a kept segment (ใส่ ใส่โมชั่น...), don't drop the segment — find the second instance of the duplicated syllable in the word list and start the cut there. Pre-pad 30 ms.

3 · The render
helpers/render.py does everything if the EDL is right:

python helpers/render.py edl.json -o final_basecut.mp4 --no-subtitles
This:

Extracts each range with auto color grade + 30 ms fades baked in (per-segment MP4).
Concats lossless via the concat demuxer.
Loudness-normalizes to −14 LUFS / −1 dBTP / LRA 11 (TikTok / IG / YouTube safe).
--no-subtitles for the base cut. Karaoke captions and burned subs are added during the motion-graphics composite, not here.

4 · The conversation loop
The cut almost always takes 2–3 iterations. Standard rhythm:

Propose a plan as a markdown table of # | timestamps | translation. Wait for approval.
First render. User watches. Common feedback: "too punchy", "syllable X clipped", "gap before line Y is too long".
Iterate using §2.4 — never global-tweak the pads, always pinpoint the offending segment edge from word-level data.
Re-render. Repeat until "ship it".
Fast iteration trick: clear clips_graded/ before each re-render so the helper re-extracts. Skipping this with stale clips silently re-uses the old segment durations.

5 · Anti-patterns
❌ Using phrase-end timestamps as cut points. They look right, they're not.
❌ Padding more to "be safe". Generous pad introduces dead air; aggressive pad with smart-tail is what feels professional.
❌ Re-transcribing because the cut is wrong. Transcripts are always cached; bad cuts are EDL bugs.
❌ Burning subtitles before the user signs off the cut. Subtitles are LAST, after motion graphics, after final lock. (See video-use Hard Rule #1.)
❌ Single-pass filtergraph render. Every segment will be re-encoded when you later add overlays — quality loss + 5× the wait. Always per-segment + concat.
❌ Running transcribe.py on a file with ._* AppleDouble siblings. On exFAT the AppleDouble files break ALL Python globs in the project. Always find . -name '._*' -delete before any helper run, and export COPYFILE_DISABLE=1 in the shell.
6 · One-line summary
Trim with word-level data, never with the ear. Cap inflated stop-consonant tails at 280 ms. Show the plan, wait for approval, render, iterate, ship.