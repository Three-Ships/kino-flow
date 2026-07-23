# Dog is Human — Auto Editor

A stripped-down, single-purpose video editor for the Dog is Human (DiH) client.
Briefs go in; 8 voiceover-driven ad videos come out, each in its own folder.

## The pipeline

```
brief (Slack note / notes file)
   └─▶ Gemini writes a script (learns from knowledge/ + the brief)
        └─▶ ElevenLabs generates the voiceover
             └─▶ b-roll matched to each line  (label-first, CLIP fallback)
                  └─▶ composed 9:16 video  (captions burned last, music ducked)
                       └─▶ output/<timestamp>_<slug>/final.mp4
```

## First-time setup

1. **Keys** — make sure `video-use/.env` has `ELEVENLABS_API_KEY` and
   `GEMINI_API_KEY`. (The tool reads them from there; no new key files.)
2. **Start it** — double-click `start-dih.bat`. It builds its own `.venv`
   (never touches the studio or video-use venvs) and opens
   `http://127.0.0.1:8770`.
3. **Pick a voice** — Settings → Voice lists your ElevenLabs account voices.
   Choose the DiH voice and Save.

## Everyday use

1. **Add footage** — drop clips into labeled folders under `broll/`
   (`broll/happy dog/`, `broll/cleaning ears/`, …). Folder name = the label.
2. **Teach it** — put brand voice, product facts, and do-not-say rules in
   `knowledge/`. Everything there is fed to Gemini before each script.
3. **Add briefs** — paste each DiH Slack note in the UI (or drop `.md` files in
   `notes/`). One note = one video.
4. **Run** — "Run this" for a single video, or "Run all →" for the week's 8.
5. **Review** — finished videos appear at the bottom with preview + download.
   Each lives in `output/<timestamp>_<slug>/` with its script, VO, EDL, and meta.

## Slack

You're a guest in DiH's Slack, so today notes come in by paste/folder (see
`notes/README.md`). To pull many at once, use `slack_import.py`. When the Slack
connector is authorized, flip `slack.mode` to `connector` in `config.json` and a
live pull can drop briefs straight into `notes/` — nothing downstream changes.

## Config (`config.json`)

- `voice_id`, `voice_model`, `voice_settings` — ElevenLabs VO.
- `gemini_model` — default `gemini-3.5-flash`.
- `video` — resolution (default 1080×1920), fps, target/min/max seconds.
- `captions.enabled`, `music.enabled`, `music.vo_duck_db`.
- `broll.clip_fallback` / `broll.clip_min_score` — the CLIP fallback for lines
  with no label match (uses the existing `video-use` matcher + its venv).
- `batch_size` — videos per "Run all" (default 8).

## Command line (optional)

```
python gemini_script.py --note notes/itchy.md --json      # just the script
python orchestrate.py   --note notes/itchy.md             # one full video
python orchestrate.py   --batch                           # all briefs (up to 8)
```

## Notes / limits
- b-roll segment timing is distributed across the VO by line length (no per-word
  alignment) — reliable and in-sync by construction.
- First render on a new machine tries `h264_nvenc`, then falls back to `libx264`.
- The CLIP fallback needs `torch`/`open_clip` in the `video-use` venv; without it,
  unmatched lines get a random clip and are flagged `needs_review` in `meta.json`.
