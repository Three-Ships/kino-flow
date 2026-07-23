# KINOKORE — Design Document

A Claude-powered video editing studio. Operators describe what they want in
plain English; the agent runs the actual edit using a library of Python
helpers, with the browser as the conductor's seat.

This doc is for engineers and team members picking up the project. It
explains the **why** behind decisions, not just the what — the what is in
the code. Read this once before making structural changes.

**Last revised:** 2026-05-13. Sections marked ★ have changes from earlier
drafts — review those first if you've read this before.

---

## 1. Audience and operating model

KINOKORE has three user types:

- **Operator** — picks assets, types a prompt, hits Send. Doesn't see
  ffmpeg commands. Switches between Endurance, Windows, Bath teams. Works
  primarily in the Chat panel and Roll the Dice flow.
- **Producer** — same as Operator plus configures asset roots, brand
  guidelines, caption presets, music levels. Owns the Jobs tab for
  cost/wall-time monitoring.
- **Maintainer** (you, if you're reading this) — owns helpers, the studio
  server, the prompt builders, and integrations. Everything below is for
  you.

The operator experience is the source of truth. If a helper is correct
but the operator can't trigger it cleanly from the browser, the helper
is broken.

---

## 2. System map

```
                              browser
                                 |
                                 |  SSE stream
                                 v
   +-----------------------------------------------------+
   |  studio/server.py  (FastAPI, port 8765)             |
   |  - /api/chat       SSE: spawns `claude --print`     |
   |  - /api/files      list videos/ and videos/edit/    |
   |  - /api/fs/*       arbitrary filesystem browse      |
   |  - /api/heygen/*   proxies HeyGen list/voices       |
   |  - /api/jobs/*     job ledger (jobs.jsonl)          |
   |  - /api/timeline   tracks for the timeline strip*   |
   +-----------------------------------------------------+
        |                                |
        |  spawns                        |  reads/writes
        v                                v
   +-----------------+        +------------------------+
   | claude CLI      |        |  videos/               |
   | (per-prompt     |        |    *.mp4, *.wav, *.txt | <- Resources
   |  agent run)     |        |    edit/               |
   |                 |        |      <run_id>/         | <- Outputs
   |  invokes:       |        |        final.mp4       |
   +-----------------+        |        proxy_1080p.mp4 |
        |                     |        captions.srt    |
        |  Bash               |        ...             |
        v                     +------------------------+
   +------------------------+
   | video-use/helpers/*.py |
   |  - sync_audio.py       |
   |  - best_take.py        |
   |  - transcribe.py       |
   |  - level_audio.py      |
   |  - broll_overlay.py    |
   |  - graphics_overlay.py |
   |  - tts_voice.py        |
   |  - tts_music.py        |
   |  - heygen_video.py     |
   |  - render.py           |
   |  - ...                 |
   +------------------------+
        |
        v
   +----------+
   |  ffmpeg  |  (NVENC on RTX-class GPU; CFR re-encode pattern)
   +----------+
```

\* Timeline strip removed 2026-05-07; endpoint retained for future use.

The browser never invokes ffmpeg or Python helpers directly. It builds a
prompt (markdown with code blocks), POSTs it to `/api/chat`, and streams
the agent's tool calls + results back through SSE. The agent reads the
prompt, runs helpers via Bash, the helpers write files to `videos/edit/`,
the studio polls `/api/files` and surfaces the deliverables in the Files
tab.

---

## 3. Repository layout

```
/
|-- CLAUDE.md                     # standing instructions for the agent
|-- DESIGN.md                     # this file
|-- docs/
|   |-- TRIMMING_PHILOSOPHY.md    # how to cut a talking head
|   |-- RENDER_EXPORT_PHILOSOPHY.md
|   `-- MOTION_PHILOSOPHY.md
|-- studio/
|   |-- server.py                 # FastAPI + SSE
|   |-- start-studio.bat
|   |-- .venv/                    # uvicorn, fastapi, anthropic CLI
|   `-- static/
|       |-- index.html
|       |-- style.css
|       |-- app.js                # ~4400 lines, see Section 9
|       |-- kinokore-logo.jpg     # master logo (1116x2000)
|       |-- kinokore-icon.png     # iris medallion crop
|       |-- kinokore-icon-128.png # sidebar brand mark
|       |-- favicon-32.png        # browser tab icon
|       |-- favicon-64.png        # high-DPI tab icon
|       |-- apple-touch-icon.png  # iOS / macOS pinned icon (180x180)
|       |-- heygen_avatars.json   # cached HeyGen avatars list (~3.5 MB)
|       `-- heygen_voices.json    # cached HeyGen voices list (~1 MB)
|-- video-use/
|   |-- SKILL.md
|   |-- .env                      # ELEVENLABS_API_KEY, HEYGEN_API_KEY
|   |-- .venv/
|   `-- helpers/*.py              # the helpers Claude calls
|-- hyperframes/                  # motion-graphics renderer
`-- videos/                       # KINOKORE-managed asset and output storage
    |-- <raw uploads>.mp4
    |-- <raw uploads>.wav
    `-- edit/
        |-- jobs.jsonl            # job ledger (one record per /api/chat POST)
        |-- sync_log.jsonl        # one record per sync_audio.apply_sync() call
        `-- <run_id>/             # per-run output folder (Section 5.1)
            |-- proxy_1080p.mp4
            |-- captions.srt
            `-- final.mp4
```

Sync batch outputs live with the source material instead of in
`videos/edit/`:

```
<any source folder, on Google Drive or local SSD>/
|-- <raw videos>.mp4 / .MOV
|-- <raw audio>.WAV
`-- synced/                       # match_pairs.py output (Section 5.1)
    `-- <stem>_synced.mp4
```

The asset library (HOOKS, B-Roll, CTAs, Music, SFX, Brand Guidelines) is
NOT inside this repo — it lives on an external drive or a Google Drive
sync path the operator points the gear at. See Section 7.

---

## 4. The pipeline contract

A "pipeline run" is one user prompt that produces one deliverable. The
operator initiates it in one of three ways:

1. **Send button (Chat panel)** — types a prompt, optionally picks a video
   from the composer. `buildPipelinePrompt()` wraps the user text in
   structured markdown with sections for Assets, Performance,
   Standing Rules, B-roll, Captions, Graphics, Music/SFX.
2. **Roll the Dice** — fully autonomous. Picks a random hook from the
   selected team's HOOKS folder, writes a script, generates VO, picks
   b-roll, builds captions, composites, encodes. Reads its parameters
   from sidebar state (active team, dice asset root, caption preset).
3. **Avatar tab** — generates a HeyGen segment or PIP overlay. The
   prompt body invokes `heygen_video.py` with the chosen avatar/voice
   and waits for completion.

All three converge on the same hard rules (Section 5).

### 4.1 Prompt structure

Every pipeline prompt starts with:

```
## Pipeline build - autonomous execution, NO approval gates,
   NO preview gates, NO permission asks

**HARD RULE - RUN STRAIGHT THROUGH.**
   (autonomy clause)

### Team / brand: <team name>           (only if team is selected)

### Output folder (HARD RULE)
   videos/edit/<stem>_<RUN_TS>/

### Performance - proxy-first downscale (HARD RULE)
   ffmpeg ... scale=1920:1080 ... -> <run_dir>/proxy_1080p.mp4

### Assets
   - Video: <path>
   - External audio: <path>  (optional)
   - Script: <path>          (optional)
   ...

### Standing rules
   - Level dialogue to peak [-6, -3] dBFS
   - Level music bed to [-24, -20] dBFS
   - Subtitles burn LAST
   ...

### B-roll          (optional)
### Captions        (optional)
### Graphics        (optional)
### Music & SFX     (optional)

(freeform user notes)
```

This structure is intentional. The agent on Opus 4.7 with a 1M context
window does NOT need terse prompts — it needs scoped, rule-marked
sections so it doesn't confuse a caption rule with a music rule. Most
"why is the agent confused?" bugs are missing or misplaced section
headers.

### 4.2 The autonomy clause

```
HARD RULE - RUN STRAIGHT THROUGH. Once every input under Assets is
verified to exist, execute every step back-to-back without pausing.
Do NOT ask "should I proceed?", do NOT render a 10-second preview
and wait, do NOT ask which mode to use, do NOT confirm encoder
choices. The user has pre-authorized the entire pipeline by
clicking Send. The only legitimate stop is a hard failure.
```

This overrides the CLAUDE.md preview-gate hard rule. CLAUDE.md says
"always render a 10s preview before final encode." That's true for
ad-hoc operator-driven work. For pipeline runs initiated through the
composer, the operator has explicitly pre-authorized by clicking Send,
so the preview gate becomes friction not safety.

When extending the prompt builder, do NOT add "want me to continue?"
checkpoints. Every extra ask costs the operator $0.05-$0.10 in agent
overhead and several minutes of wall time.

---

## 5. Hard rules (cross-cutting)

### 5.1 Per-run / per-batch output folders ★

Every operation that produces deliverables uses a scoped subfolder. Two
patterns by purpose:

**Pipeline / Dice / HeyGen** — each run creates ONE folder under
`videos/edit/`:

- Pipeline composer: `videos/edit/<source_stem>_<RUN_TS>/`
- Roll the Dice:    `videos/edit/dice_<RUN_TS>/{hook_proxy.mp4, hook_trimmed.mp4, vo.wav, broll_edl.json, captions.srt, final.mp4}`
- HeyGen:           `videos/edit/heygen_<mode>_<RUN_TS>/{avatar.mp4, composite.mp4}`

All artifacts go inside: proxies, EDLs, SRTs, intermediate cuts, the final
MP4. Pre-2026-05-07 runs landed at the top of `videos/edit/`; those legacy
files appear under the "_loose (pre-folder runs)" collapsed card in the
Files tab.

**match_pairs.py (bulk audio sync)** — uses a `synced/` subfolder
co-located with the source files, NOT `videos/edit/`:

```
<source folder>/
├── *.mp4 / *.MOV    ← raw videos
├── *.WAV            ← raw audio takes
└── synced/
    └── <stem>_synced.mp4  ← all batch outputs
```

The reasoning: sync outputs belong with their source material (operators
will grade or cut them together as a batch), while pipeline deliverables
belong in the project's `videos/edit/` workspace. The subfolder name is
controlled by `SYNCED_SUBDIR` at the top of `match_pairs.py`.

`discover()` in `match_pairs.py` explicitly excludes `synced/` from its
scan so re-runs don't try to re-sync the helper's own outputs (which
would produce `*_synced_synced.mp4` garbage). It also skips legacy
`*_synced.mp4` files left at the folder root from pre-subfolder runs and
any `._sync_tmp_*` partial encodes from interrupted jobs.

### 5.2 Proxy-first downscale

Source footage is typically 4K. Final delivery is 1080p (Meta cap).
Without an explicit rule, the agent re-applies `scale=1920:1080` on
every encode pass (sync, best_take, EDL cut, graphics composite,
captions, final). A 6-min 4K clip ends up taking 15+ min and ~$5 in
agent overhead, scaling the same frames five times.

**The fix** (always injected): probe source -> if `>1080p`, downsize
ONCE to a `proxy_1080p.mp4` at the top of the run -> every downstream
step operates on the proxy -> no `scale=` filter anywhere later.

NVENC command:
```
ffmpeg -y -hwaccel cuda -i <source> \
  -vf "scale=1920:1080:flags=lanczos" \
  -c:v h264_nvenc -preset p4 -rc vbr -cq 19 \
  -b:v 12M -maxrate 18M -bufsize 24M \
  -c:a copy -movflags +faststart \
  <run_dir>/proxy_1080p.mp4
```

If you're tempted to add a `scale=1920:1080` to any other ffmpeg
command in the pipeline, you're doing it wrong. Stream-copy or
re-encode at native (now-1080p) resolution.

### 5.3 Audio levels

Hard limits, enforced by the prompt:

| Stream    | Peak target  | Method                        |
|-----------|--------------|-------------------------------|
| Dialogue  | -6 to -3 dBFS| `level_audio.py --dialogue` or `sync_audio.py --level-dialogue` |
| Music bed | -24 to -20 dBFS| `level_audio.py --music`    |

Dialogue boost is UPWARD (lavs record around -12 dBFS). Never cut
dialogue below -6 dBFS. Music must NEVER exceed -20 dBFS under active
VO. `loudnorm` is forbidden — use peak gain only, two-pass if needed.

### 5.4 Captions burn LAST

Every other layer (cuts, graphics, b-roll, music mix) must lock first.
Captions are an `ffmpeg ... -vf subtitles=<srt>:force_style='...'`
pass on the locked composite. Never composite atop a caption-burned
video.

### 5.5 Stream-copy when possible

If a step only trims at keyframes or changes container, use `-c copy`.
Re-encode ONLY when filters require it (subtitles burn, overlay
composite, dialogue level on the same MP4 as video). Each unnecessary
re-encode is a full decode + scale + encode of every frame.

### 5.6 Background long ffmpeg jobs

For any ffmpeg step the agent estimates >2 min, the prompt instructs:

```
ffmpeg ... &
wait
```

No `-progress`, no live narration. The operator pays Claude tokens per
second the agent is "watching." Fire-and-wait, parse exit code, move on.

### 5.7 Atomic write + idempotency (sync_audio.py) ★

Added 2026-05-13 after the parallel-encode race burned 5 files in the
2026-05-13 RbA Street Shoot batch (see Section 12.2). Two-part fix:

**Atomic write** — ffmpeg encodes to `._sync_tmp_<PID>_<name>.mp4` first;
on successful exit `os.replace()` publishes to the final path. Two
processes encoding to the same final path now write to different PID-
suffixed temp files, and last-rename-wins atomically. A partially-written
file never appears at the final destination. If the encode fails or is
killed, the temp file is left as orphan junk and gets cleaned up on the
next run by `match_pairs.py`'s `discover()` skip rule.

**Idempotency guard** — `_existing_sync_is_current()` checks the sync log
for a recent entry matching the same `(out_path, video, audio, offset±10ms,
leveling targets)` AND re-probes the file's audio peak on disk to confirm
it's still in the target window. If yes, the encode is skipped and a
"reused" log entry is written. This makes the helper safe to restart:
killing and re-running picks up where it left off instead of redoing all
the work.

Path moves invalidate the idempotency check (different `out_path` = no
match in the log). That's intentional — if you move the sync output, the
helper treats it as a new target and re-encodes.

---

## 6. Team / brand scoping

KINOKORE serves three product lines: **Endurance** (auto warranty),
**Windows** (RbA Windows), **Bath** (Walk-in Showers). Each has its own
brand voice, asset palette, and compliance guidelines.

### 6.1 Sidebar picker

Three pills in the sidebar above Roll the Dice. Click a pill to scope
the next run to that team; click the active pill again to clear.
Persisted in `localStorage['veditor.team.v1']`.

### 6.2 Asset folder convention

```
<asset root>/                  # what the gear icon points at
  Endurance/                   # team folder (alias-matched)
    HOOKS/
    B-Roll/
    CTAs/
    Music/
    SFX/
    Brand Guidelines/
  Windows/
    HOOKS/
    ...
  Bath/
    ...
```

Team folder name matching is case-insensitive alias-based:

| Team       | Aliases (lowercase match)                                        |
|------------|------------------------------------------------------------------|
| Endurance  | endurance, auto warranty, auto-warranty, auto                    |
| Windows    | windows, rba windows, rba-windows, rba                           |
| Bath       | bath, walk-in showers, walkin, showers, walk-in                  |

If no team folder matches under the asset root, `resolveTeamFolder()`
falls back to the bare root (legacy behavior).

### 6.3 Where it applies

The active team is injected into:

1. **`buildPipelinePrompt()`** — every Send-button run
2. **Roll the Dice prompt** — autonomous compose
3. **`diceRootSlot()`** — the Music/SFX tab "rotate root" sync button

The prompt block:

```
### Team / brand: Bath . Walk-in Showers
This run is scoped to the Bath product line. ALL creative decisions -
script copy, b-roll selections, music tone, caption phrasing, brand
voice, terminology - must be appropriate for Walk-in Showers. Do NOT
mix references from other product lines.
```

If a `Brand Guidelines/` folder exists inside the team folder, the dice
prompt's Step 0 says "read every doc here FIRST before writing a word
of script or choosing any asset."

---

## 7. Asset library (external)

Operators don't keep their working library inside this repo. The gear
icon on Roll the Dice points at any local path — typically a Google
Drive Desktop sync folder, an external SSD, or a network share.

### 7.1 Google Drive handoff

For team rollout: install Google Drive Desktop on each operator's
machine, sync the shared `Assets/` folder to a local path, and turn OFF
"stream files / files-on-demand." Files must physically exist on disk
so ffprobe/ffmpeg can read them as ordinary local paths.

Typical operator path: `G:\My Drive\Assets\` or
`C:\Users\<name>\Google Drive\Assets\`. They point the gear at that,
click Save, done.

For headless/server setups, use rclone with `--vfs-cache-mode full`.

### 7.2 What lives in each subfolder

- **HOOKS/** — pre-recorded hook videos. Roll the Dice picks one at
  random and runs it through the full trim pipeline (silence removal,
  false-start detection, word-boundary cuts, 30 ms crossfades).
- **B-Roll/** — supplementary footage clips. EDL builder maps script
  sections to clips. Each clip is probed; if >1080p, proxied first.
- **CTAs/** — end-card clips appended as the final 3-5s.
- **Music/** — beds for the Music/SFX tab. Modes: Full bed / Intro only
  / Outro only.
- **SFX/** — short sound effects placed at transitions/impact moments.
- **Brand Guidelines/** — markdown, PDF, or text docs the agent reads
  BEFORE writing script copy.

Brand Guidelines is consulted by the dice flow, NOT by the regular
pipeline (yet). Eventually the regular pipeline should also pre-flight
read brand docs when a team is active.

---

## 8. Integrations

### 8.1 ElevenLabs (TTS)

- Key: `ELEVENLABS_API_KEY` in `video-use/.env`
- Helper: `video-use/helpers/tts_voice.py` for VO, `tts_music.py` for
  music beds
- Cache: `studio/static/voices.json` (refresh via "refresh voices" in
  the VO modal)
- Output convention: `<run_dir>/vo.wav` (dice) or
  `videos/edit/<voice_name>_<ts>.mp3` (composer)

### 8.2 HeyGen (avatar video)

- Key: `HEYGEN_API_KEY` in `video-use/.env`
- Plan tier: Creator. Some "premium" public avatars are Team-tier-only
  and will fail at generation time with a quota error.
- Helper: `video-use/helpers/heygen_video.py`
  - `--list-avatars` / `--list-voices` (writes to `--output` path as JSON)
  - Generate mode polls every 8s with 12-min hard timeout
  - Supports `--transparent` for WebM-with-alpha (PIP overlay)
- Server proxies: `/api/heygen/avatars`, `/api/heygen/voices`,
  `/api/heygen/status` — cached to disk to avoid re-hitting the API
- Cache files: `studio/static/heygen_avatars.json` (~3.5 MB),
  `heygen_voices.json` (~1 MB)
- Default avatar (operator's twin): `a78e96535de64bd4bbf758c1ec0eb90a`

#### 8.2.1 Why we write JSON to disk

On Windows, `subprocess.run(text=True)` decodes captured stdout using
the system's default code page (cp1252), NOT UTF-8 — even with
`PYTHONUTF8=1` set on the child. HeyGen returns avatar names with
non-ASCII characters; cp1252 decoding raises `UnicodeDecodeError`
and FastAPI returns 500. The helper writes JSON directly to disk via
`--output`; the server reads the file as UTF-8. Don't change this
back to stdout-capture without first solving the encoding problem.

#### 8.2.2 Quality tiers

Two cost tiers in the Avatar tab:

| Tier  | Pixel grid options                  | Credit cost |
|-------|-------------------------------------|-------------|
| 720p  | 1280x720 / 720x1280 / 720x720        | ~half       |
| 1080p | 1920x1080 / 1080x1920 / 1080x1080    | full        |

Aspect is independent: landscape / vertical / square.

### 8.3 Anthropic Claude

- The studio shells out to the `claude` CLI binary (`CLAUDE_BIN` env or
  PATH lookup).
- Each `/api/chat` POST spawns a fresh `claude --print` subprocess that
  streams stream-json events back through SSE.
- Models: Opus 4.7 (default for high-judgment pipeline runs), Sonnet 4.6
  (mechanical helper runs like HeyGen generate or audio sync), Haiku 4.5
  (rare — only when explicitly requested).
- The sidebar model dropdown sets the default; the composer model select
  overrides per-Send.

---

## 9. The browser app (`studio/static/app.js`)

Single 4000+ line file, no bundler. Sections (in order of execution):

1. **Top-level utilities** — `$`, `$$`, error trap, Split.js panes.
2. **DOM refs and constants** — `composerVideoSelect`, `presetState`,
   localStorage keys, `TEAMS` constant.
3. **FS browser core** — `loadFsRoots`, `loadFsList`, `renderFsRow`
   (legacy; mostly hidden now but still services drag-from-files).
4. **Tab switching** — `.tab` click -> `activateTab(name)`.
5. **Job tracker** — `detectOperations()` parses Bash commands to tag
   jobs with op names (`audio_sync`, `tts_voice`, `heygen_video`, etc.).
6. **Pipeline prompt builder** — `buildPipelinePrompt({...})`. The big
   function. Composes the markdown the agent receives.
7. **Captions / Graphics / B-roll / Music tab state** — persisted in
   localStorage, rendered as chips/grids.
8. **HeyGen tab** — `initHeygenTab()` IIFE.
9. **Team picker** — `initTeamPicker()` IIFE.
10. **Files tab (new)** — `initFilesTab()` IIFE.
11. **Workflow summary** — `initWorkflowSummary()` IIFE.
12. **Timeline stub** — disabled (was removed 2026-05-07).

### 9.1 Module-level vs IIFE

Older code attaches event listeners at module level. Newer code wraps
each subsystem in an IIFE. **Always use IIFEs for new subsystems.** A
single throw in module-level code halts script execution at that line
— EVERY listener defined after the throw never attaches. The error
trap at the top of `app.js` is the safety net; IIFEs are the
preventative measure.

Lesson learned 2026-05-07: removing an HTML element (`#fs-refresh-btn`)
without removing the corresponding `addEventListener` call at module
level made all buttons in the studio appear dead. The trap caught it
in a banner; the fix was guarding with `if (fsRefreshBtn)`.

### 9.2 State persistence

`localStorage` keys, namespaced under `veditor.` (legacy from
pre-rebrand):

| Key                              | Purpose                                  |
|----------------------------------|------------------------------------------|
| `veditor.preset.v1`              | caption preset (id, font, max chars, ...)|
| `veditor.broll.v1`               | b-roll tab state                         |
| `veditor.musicSfx.v1`            | music + SFX tab state                    |
| `veditor.diceRoot.v1`            | asset root path                          |
| `veditor.team.v1`                | active team id (endurance/windows/bath)  |
| `veditor.heygen.v1`              | HeyGen tab picker state                  |
| `veditor.model.v1`               | preferred Claude model                   |
| `veditor.usage.v1`               | per-tab token/cost counters              |
| `veditor.fsLastPath.v1`          | last filesystem path browsed             |

When adding a new key, version it (`.v1`, `.v2`, ...) so a future
schema change can default cleanly on the migration.

---

## 10. Jobs and workflow classification

Every `/api/chat` POST starts a job. The browser detects operations from
each Bash command the agent runs (regex match against helper script
names and ffmpeg filter flags) and writes the result to `jobs.jsonl`
via `/api/jobs/update`.

The Jobs tab shows two summaries:

### 10.1 Per-operation table

Average cost / wall time / turns per detected operation. Useful for
finding which helpers are expensive.

### 10.2 Workflow categories

Five higher-level buckets matched by op-set heuristic (first match wins):

| Workflow      | Detection rule                                                | Color   |
|---------------|---------------------------------------------------------------|---------|
| HeyGEN        | `heygen_video` present                                        | orange  |
| AI B-Roll     | `tts_voice` AND `broll_overlay`                               | pink    |
| Complex UGC   | `best_take` AND `captions` AND (`broll_overlay` OR `tts_music`)| purple |
| Simple UGC    | `best_take` AND `captions`                                    | green   |
| A/V Sync      | (`audio_sync` OR `auto_pair_sync`) AND NOT captions AND NOT best_take| cyan |

Order matters — HeyGEN beats Complex UGC even if both rules match. If
you add a new helper that defines a new workflow, add the classifier
rule ABOVE more general ones.

### 10.3 Adding a new tracked operation

1. Add a detection clause in `detectOperations()` in `app.js`:
   ```js
   if (cmd.includes('your_helper.py')) ops.add('your_op');
   ```
2. If the new helper is part of a workflow, add or update a rule in
   the `WORKFLOWS` array.
3. Historical jobs will not have the new op — they were classified
   before the change. The averages calibrate forward only.

---

## 11. Cost and performance principles

The expensive resource is operator wait time and Claude tokens, NOT
ffmpeg CPU/GPU. Optimize accordingly.

1. **Compress agent involvement.** Long ffmpeg jobs run with `&` then
   `wait`. The agent doesn't poll `-progress`, doesn't narrate frames,
   doesn't comment on partial output. ~80% of "expensive" pipeline
   runs were the agent watching ffmpeg.
2. **Pre-fail on cheap checks.** Probe inputs before submitting render
   jobs. A 10-min HeyGen wait that errors at the end because the voice
   ID was wrong costs more than the API call itself.
3. **Stream-copy is free.** A trim-at-keyframes operation should never
   re-encode. If you find yourself decoding+encoding to "fix" container
   metadata, write a `-c copy` instead.
4. **Sonnet for mechanical work.** Helper runs that only invoke the
   same Python script with arguments don't benefit from Opus reasoning.
   HeyGen generate, audio sync, simple captions burn — Sonnet 4.6
   delivers identical output at ~5x lower cost.
5. **Per-run folders enable parallelism.** Two pipeline runs can
   execute simultaneously without colliding on output paths, as long
   as they get different `RUN_TS` values.

---

## 12. Known limitations and roadmap

### 12.1 Not implemented yet

- **Logo asset.** KINOKORE rebrand is complete in titles and sidebar
  text; logo image file is pending.
- **Brand Guidelines pre-flight in regular pipeline.** Dice flow reads
  brand docs before writing script. Regular pipeline currently does
  not — it should, when a team is selected and the team folder has a
  Brand Guidelines/ subfolder.
- **Workflow-aware Send.** Operator types a prompt; we could classify
  the INTENDED workflow and surface the avg cost/time before they hit
  Send.
- **Output thumbnails.** Files tab Outputs view lists folders but
  doesn't show a video thumbnail for the deliverable. Would need an
  ffprobe-keyframe-grab on the server side.

### 12.2 Known sharp edges ★

- **Parallel sync_audio encodes can corrupt outputs (fixed 2026-05-13).**
  When the studio kills+restarts an in-flight sync agent, the orphan
  Python process and its ffmpeg children keep running. If the retry starts
  before they exit, both `match_pairs.py` processes write to the SAME
  output paths simultaneously, racing on the AAC mux interleave. Symptom:
  files exist at expected sizes but their audio decodes to silence
  (-91 dBFS) with `channel element 3.2 not allocated` errors. **Fixed**
  via atomic write (Section 5.7). Don't undo the PID-suffixed temp +
  `os.replace()` pattern.
- **Duplicate source files burn extra wall time.** If the source folder
  contains both `foo.mp4` and `foo (1).mp4` (byte-identical from a
  duplicate upload), `match_pairs.py` treats them as separate jobs and
  encodes both. Cheap to dedupe before running (just `rm` the `(1)`).
- **Windows subprocess stdout decoding.** Documented in Section 8.2.1.
  Don't capture large UTF-8 outputs through `subprocess.run(text=True)` —
  use disk for the round-trip when output is >100 KB.
- **Hidden-attribute CSS conflict.** Any `.foo { display: flex }` rule
  on an element that toggles via the HTML `hidden` attribute MUST also
  declare `.foo[hidden] { display: none !important; }`. Otherwise the
  element renders even when hidden, which for fixed-position overlays
  means the entire UI becomes click-dead. The error trap at the top of
  `app.js` (window.onerror banner) catches the symptom; the lesson is to
  use `:not([hidden])` toggles or always-explicit display rules.
- **Module-level `addEventListener` calls.** Use IIFEs (Section 9.1). A
  single throw on a null DOM element halts script parsing at that line —
  EVERY listener defined after the throw never attaches. Has happened
  twice; the global error trap was added specifically to make this
  diagnosable.
- **HeyGen Creator quota.** ~10-15 min of generated video per month.
  Hits silently — generation fails with a quota error mid-run, and
  the avatar grid still shows the avatars (since the list endpoint is
  free). Communicate this to operators before they burn the limit on
  experiments.
- **46+ pre-folder runs.** Files at top of `videos/edit/` from before
  2026-05-07 are bucketed under `_loose (pre-folder runs)` and stay
  there. No retroactive cleanup; new runs use per-run folders.

### 12.3 Things that look weird but are correct

- The studio's localStorage keys are namespaced `veditor.*` despite
  the KINOKORE rebrand. They're operator state; renaming would wipe
  every operator's saved preferences on next load. Leave them.
- `sync_audio.py` defaults to `--auto-trim-head`. The synced MP4
  starts at content-start with audio at t=0. Downstream tools assume
  a 0-based timeline. Don't apply manual SRT offsets — they'll
  double-shift.
- `sync_audio.py` re-encodes the video stream with NVENC instead of
  `-c:v copy`. Costs ~1 min wall time but eliminates a freeze-frame
  failure mode caused by Canon's variable-PTS H.264 packet structure.
- `sync_audio.py` writes outputs via a `._sync_tmp_<PID>_<name>.mp4`
  staging file that ffmpeg targets directly, then `os.replace()` to the
  final path. The temp files sometimes appear briefly in the output
  folder during an encode and disappear on completion — that's the
  atomic-write pattern, not a bug. If you find an orphan `._sync_tmp_*`
  file, it's safe to delete (means an encode crashed mid-run); `match_
  pairs.py`'s `discover()` ignores them on the next run anyway.
- `match_pairs.py` writes to `<source_dir>/synced/` not next to the
  sources. Section 5.1 explains why. The studio Files tab Outputs view
  doesn't show this folder because it's outside `videos/edit/` — operators
  navigate to it via Resources view or the OS file manager.

---

## 13. Making changes

### 13.1 Adding a new helper

1. Write `video-use/helpers/your_helper.py`. Follow the existing
   convention: argparse, `--json` output mode, `--apply` flag for
   destructive ops, exit non-zero on failure.
2. Add operation detection in `detectOperations()` in `app.js`.
3. If this helper enables a new workflow, add a classifier rule.
4. Update `buildPipelinePrompt()` or the relevant tab handler to
   include the helper command in the prompt body.

### 13.2 Adding a new tab

1. HTML: add a `<button class="tab" data-tab="<id>">` in the sidebar
   and a `<div class="tab-panel" data-panel="<id>">` in the content
   panel.
2. JS: wrap the tab's init in an IIFE. Lazy-load any expensive data
   on first tab click (see `initHeygenTab()` for the pattern).
3. CSS: any fixed-position overlays MUST include the `[hidden]`
   override (Section 12.2).

### 13.3 Changing the pipeline prompt

`buildPipelinePrompt()` is the single source of truth. Test by setting
`composerBuildBtn`'s click handler to log the result (it already does:
"preview prompt" buttons in the captions/graphics/etc. tabs). NEVER
add interactive checkpoints. NEVER weaken the autonomy clause.

### 13.4 Restarting the studio

Hard rule from `CLAUDE.md`: do not restart while any `claude.exe` or
`ffmpeg.exe` job is running on the machine. Check with:

```
Get-Process claude,ffmpeg -ErrorAction SilentlyContinue
```

If clear, `Ctrl+C` in the studio terminal and relaunch via
`studio/start-studio.bat`. The browser reconnects automatically when
SSE comes back up.

---

## 14. Operational checklist for new team rollout

When onboarding a new operator on a new machine:

1. Clone the repo (or distribute as a packaged folder).
2. Install Python 3.12+, Node 20+, ffmpeg with NVENC support (bundled
   in `D:\Program Files\ffmpeg\bin` on the canonical setup; adjust
   `FFMPEG_DIR` for theirs).
3. `cd video-use && uv venv && uv pip sync requirements.txt`
4. `cd studio && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`
5. Drop `ELEVENLABS_API_KEY` and `HEYGEN_API_KEY` into
   `video-use/.env`.
6. Install Google Drive Desktop, sync the team assets folder.
7. Launch `studio/start-studio.bat`. Open `http://127.0.0.1:8765/`.
8. Gear icon on Roll the Dice -> browse to the Drive-synced assets
   folder -> Save.
9. Click the team pill that matches their lane (Endurance / Windows /
   Bath).
10. Drop a test video into the right-side dropzone, type "cut to
    script, add captions, export," hit Send.

If step 10 doesn't produce `videos/edit/<stem>_<ts>/final.mp4` in
under 5 minutes wall time, something is broken — start with the Logs
tab.

**Bulk audio-sync sanity check (for crews using dual-system audio):**

11. Put a small test set (2-3 videos + 2-3 audio takes) in a folder.
12. From the studio chat, paste the "auto-pair every video in `<folder>`"
    prompt. It runs `match_pairs.py` with `--audio-continuous`.
13. Confirm the outputs land in `<folder>/synced/`, NOT next to the
    source files (the synced subfolder is the 2026-05-13 convention).
14. Re-run the same prompt. The idempotency guard (Section 5.7) should
    skip the already-good files in ~1 second per file with a
    `[skip] ... reusing existing` line in stderr. If it re-encodes them
    all, the guard is broken — check `_existing_sync_is_current()` and
    the sync_log path comparison.

---

## 15. Session changelog ★

Significant milestones since the original DESIGN.md draft (2026-05-06):

- **2026-05-07** — Files tab redesigned (Outputs / Resources sub-tabs +
  inline text preview); Jobs tab gained workflow-category averages; team
  picker extended from Roll the Dice into every pipeline run; global JS
  error trap added.
- **2026-05-07** — Per-run output folder rule baked into all three
  pipeline entry points (composer / dice / HeyGen).
- **2026-05-07** — Proxy-first downscale baked into pipeline prompts (one
  4K→1080p pass at the top, all downstream work runs on the proxy).
- **2026-05-07** — Removed timeline strip; rebranded VEDITOR → KINOKORE
  in title bar / sidebar / header (logo + favicons added 2026-05-13).
- **2026-05-07** — Dice modal upgraded to surface team-scoped slot
  discovery and explicit error toasts when team or root is misconfigured.
- **2026-05-13** — Brand review removed from Dice prompt per operator
  request.
- **2026-05-13** — KINOKORE logo + favicon set wired in (silver/gold
  wordmark via CSS `background-clip: text`).
- **2026-05-13** — `sync_audio.py` got atomic write + idempotency guard
  (Section 5.7). `match_pairs.py` now outputs to `<source>/synced/`
  subfolder (Section 5.1).
- **2026-05-13** — Diagnosed and fixed the parallel-encode race that
  silently corrupted 5 of 15 outputs on the 2026-05-13 RbA Street Shoot
  batch (Section 12.2). Root cause: studio kill+restart of the agent
  left orphan Python + ffmpeg processes racing with the retry on the
  same output paths.
