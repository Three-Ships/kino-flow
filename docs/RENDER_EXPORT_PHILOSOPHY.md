RENDER & EXPORT PHILOSOPHY — How this studio hands work back to you
Required reading before any render or composite step. Defines the two delivery modes, when each is the right call, the wall-time you should expect, and the question the agent must always ask before kicking off a long render.

0 · The non-negotiable rule
Before any render that produces a deliverable, the agent MUST ask the user which mode they want and quote the time estimate.

Never silently default. Both modes are valid; the wrong one wastes hours.

1 · The two modes
Mode A — Full composite (final.mp4)
The studio renders everything: trimmed base + motion-graphics overlay + face-crop transitions + (optional) burned subtitles, all baked into a single H.264 MP4 ready to upload.

sources → trim → base.mp4 → render overlay.mov → ffmpeg composite → final.mp4
You get one file. No NLE needed. Upload-ready.

Mode B — Layered handoff (separate files for an NLE)
The studio renders the base cut and the motion-graphics overlays as separate assets. You composite in Premiere / Resolve / FCP at the timeline level — drop the base on V1, overlays on V2, scrub freely.

sources → trim → base.mp4 + render overlay.mov  → handoff
                                                  ↓
                                        you composite in NLE
You get a folder of assets. Real-time preview, free iteration, your color/audio decisions live in your NLE.

2 · When to pick which
Situation	Mode	Why
Short-form (<2 min): intro, hook, social cut, end card	A — Full composite	Composite cost is small; one-file convenience wins
Long-form (>10 min)	B — Handoff	Full composite scales poorly; NLE preview is real-time
You'll iterate on card timings or fonts after the first render	B — Handoff	NLE drag-edit > re-running 30 min of ffmpeg
You want to add B-roll, music ducking, cutaways	B — Handoff	Audio/B-roll judgment lives in the NLE
Hand-off to a teammate without an NLE	A — Full composite	Universal MP4, no setup
The motion graphics cover < 30% of the video duration	B — Handoff	Don't render alpha frames over an hour of nothing
You're producing for an automated pipeline (CI, scheduled cron, social autoposter)	A — Full composite	Single artifact, deterministic, no human in the loop
Default if the user is unsure: Mode B. It loses you nothing (you can always run Mode A's composite step later) and saves the most painful 30+ minutes of iteration.

3 · Wall-time you should quote
These are real measurements from this workspace (1080p @ 24fps, on an external exFAT drive, M-series Mac). Internal SSD gets ~2–4× faster on the composite step.

Step	Wall time	Scales with
Transcribe (ElevenLabs Scribe)	~10s per minute of audio	source duration
Pack transcripts	<1s	constant
Render base cut (render.py)	~1 min per minute of OUTPUT	output duration
Hyperframes overlay render	2–4 min per minute of overlay	overlay duration only
ffmpeg composite	~30 min per minute of output (exFAT) / ~6–10 min (SSD)	output duration
Mode A (full composite) total time
T_A ≈ T_transcribe + T_basecut + T_overlay + T_composite
    ≈ (0.2 × in)  +  (1 × out)  +  (3 × ovl)  +  (15–30 × out)
Where in = input duration, out = output duration, ovl = overlay duration.

Mode B (handoff) total time
T_B ≈ T_transcribe + T_basecut + T_overlay
    ≈ (0.2 × in)  +  (1 × out)  +  (3 × ovl)
T_B is ~10–30× faster than T_A for any output longer than a few minutes. The composite step is the dominant cost in Mode A.

Examples (rough, exFAT external drive)
Output length	Overlay length	Mode A total	Mode B total	Difference
0:37 (intro)	0:37	~35 min	~5 min	30 min
5:00	0:30	~2.5 hours	~10 min	2 hours saved
30:00	2:00	~16 hours	~35 min	most of a day saved
60:00	5:00	~32 hours	~75 min	not viable in Mode A
For anything over ~5 min of output, Mode A becomes hostile to iteration.

4 · The script the agent should run
Before any render, the agent says (in plain language, replacing the bracketed values):

The cut is ready: [X] minutes of output, [Y] seconds of motion graphics planned. Two options:

A. Full composite — single final.mp4. Estimated wall time: ~[T_A]. B. Layered handoff — final_basecut.mp4 + overlay_*.mov you composite in your NLE. Estimated wall time: ~[T_B] (saves ~[T_A − T_B]).

Which one?

Do not start rendering until the user picks.

If they pick B, the deliverables are:

edit/
  final_basecut.mp4              # H.264, loudness-normalized, ready for V1
  overlays/
    <segment>_<start>-<end>.mov  # ProRes 4444 alpha, drop on V2 at <start>
  edl.json                       # for re-deriving timing
  transcripts/<source>.json      # for re-deriving subs / karaoke
  master.srt                     # if subtitles were authored
If they pick A, the deliverable is:

edit/
  final.mp4
Plus the intermediates above, kept around for re-runs.

5 · Format conventions for handoff (Mode B)
Asset	Codec	Container	Notes
Base cut	H.264 yuv420p, CRF 18	.mp4	Universal NLE import
Motion-graphics overlay	ProRes 4444 yuva444p12le	.mov	Native alpha in every major NLE
Subtitles	SRT (UTF-8)	.srt	Track in NLE OR burn at export
Audio	AAC 48k stereo passthrough from base	.mp4 (in base)	Never re-encoded
Avoid WebM VP9 alpha for handoff — it's smaller but Premiere and FCP need a transcode step on import. Resolve handles it natively, but the universal choice is ProRes 4444.

6 · For long videos: render only what changes
Don't render a 60-minute overlay_full.mov. Most of that file is empty alpha and Puppeteer would still capture every frame.

Right approach for a 60-minute video with motion graphics on:

0:00–0:37   → intro mograph
14:30–14:38 → lower-third name card
59:20–60:00 → end card
Render three short overlay clips (intro.mov, lower_third.mov, end_card.mov), each only as long as its window. Place them at the right offsets in your NLE. Total overlay-render wall time: ~3 min instead of ~3 hours.

The data-start and data-duration on each composition root determine the overlay's own internal timeline; the NLE handles where to drop it on the master timeline.

7 · Anti-patterns
❌ Defaulting to Mode A "to be safe." If the output is over a few minutes, Mode A is the unsafe choice — slow, hard to iterate.
❌ Rendering an overlay longer than the actual motion-graphics windows. Render only the seconds where something is on screen.
❌ Picking Mode A for anything you're not 100% sure is final. The first composite always reveals one more nudge to make.
❌ Re-rendering the base cut to fix a card timing. The base cut is locked once approved. Card timing is a Mode B (or hyperframes-only) iteration.
❌ Quoting "fast" without numbers. Always give a real estimate.
8 · One-line summary
Always ask: full composite or layered handoff? Quote the time. Default to handoff for anything longer than a few minutes — composite is the bottleneck, your NLE is faster.