# Kino Flow — macOS build handoff (for Caio)

**Goal:** build the macOS installer (`Kino-Flow.dmg`) once on your Mac, confirm
it runs, then it can be handed to any Mac teammate to install. You do **not**
need to write code — you run one script and test the result.

> **Working with Claude:** open this repo in Claude Code (or paste this file into
> Claude) and say *"help me build and test the Kino Flow Mac installer following
> HANDOFF.md."* The **Context for the AI assistant** section at the bottom gives
> Claude what it needs to troubleshoot. This is the **first** Mac build, so
> expect a small hiccup or two — that's exactly what this pass is for.

---

## What you need

- A **Mac** (Apple Silicon or Intel), macOS 11+.
- **git** (`git --version`; if missing, running it once prompts to install Xcode
  command-line tools — accept).
- Internet (the build downloads a static ffmpeg; first app launch installs a few
  things).
- **Google Drive for Desktop** installed and signed in (the app reads shared
  assets from it). Confirm you can see
  `…/Shared drives/Home Solutions Team Drive/Social/19 - Creative Assets/Kino Flow - Asset Folder`.
- **The gateway token** — ask Sean for it directly (Slack/1Password). It is NOT
  in this repo on purpose. Keep it private.

Everything else the build uses (rsync, curl, sips, iconutil, hdiutil) ships with
macOS.

---

## Build steps

**1. Get the code** (Terminal):
```bash
git clone https://github.com/Three-Ships/kino-flow.git
cd kino-flow
```

**2. Add the gateway token.** Create the local config and paste the token Sean
gave you:
```bash
cp installer/config.example.json installer/config.local.json
open -e installer/config.local.json
```
In TextEdit, replace `PASTE_YOUR_GATEWAY_TOKEN_HERE` with the token (keep the
quotes). Leave `gateway_url` as-is. Save and close. This file is gitignored — it
never gets committed.

**3. Build the DMG:**
```bash
cd installer/mac
chmod +x build-dmg.sh
./build-dmg.sh
```
Result: **`installer/mac/dist/Kino-Flow.dmg`**.

---

## Install & test

1. Open `Kino-Flow.dmg`, drag **Kino Flow** into **Applications**.
2. First launch: **right-click the app → Open** (it's unsigned, so macOS asks
   once — after that it opens normally).
3. First run is slow — it builds the Python environment, installs the Claude CLI,
   and ensures ffmpeg. Then a browser opens at **http://127.0.0.1:8765**.
4. **Smoke test:** pick a team (start with **Windows** — it's the most fully
   stocked), then run a **Streamlined Ad** and a **Variants** batch. Confirm
   finished videos appear (default: a `Final Output` folder; or the folder set in
   the sidebar's **Output Folder**).

---

## Known rough edges (expected, not bugs)

- **Unsigned app** → the right-click-Open step above. Normal for internal tools.
- **No quit window** — the app runs the studio in the background with no window.
  To stop it, quit it from **Activity Monitor** (search "python" or "uvicorn").
  A proper quit button is on the to-do list.
- **ffmpeg download** — the build pulls a static ffmpeg from evermeet.cx. If that
  fails, the app installs ffmpeg via Homebrew on first run instead (needs `brew`).

## If something breaks — what to capture

Send Sean (or paste into Claude) the **exact command** and the **full error
text**. The most likely first-build issue is a leftover Windows-specific path in
one of the helper scripts. If you see an error mentioning a path like
`…/.venv/Scripts/python.exe` or `command not found`, copy that line — it's a
quick fix on our side.

---

## Context for the AI assistant

You are helping Caio build and smoke-test the macOS installer for **Kino Flow**,
an internal video-ad studio. Key facts:

- **What it is:** a local FastAPI studio (`studio/server.py`) that serves a web UI
  at `127.0.0.1:8765` and drives a video pipeline via the `claude` CLI + ffmpeg.
  The two shipped functions are **Variants** and **Streamlined Ads**.
- **The `.dmg` build:** `installer/mac/build-dmg.sh` assembles `Kino Flow.app`
  (payload = `studio/` + `video-use/` source, a bundled static ffmpeg, a `.icns`
  icon, and a `uv`-based launcher) and runs `hdiutil` to make the dmg. It uses
  only built-in macOS tools. Do not rewrite it; help run it and fix concrete
  errors it reports.
- **Runtime bootstrap:** `installer/launch.py` (invoked by the app) loads
  `kino.config.json` (gateway URL + token), creates the Python venvs via `uv`
  (deps: requests, librosa, matplotlib, pillow, numpy for `video-use`;
  fastapi/uvicorn for `studio`), ensures ffmpeg + the Claude CLI, starts uvicorn,
  and opens the browser. Assets come from the Google Drive mount, auto-detected
  by `server.py`'s `detect_asset_root()`.
- **Gateway:** all provider API keys (Anthropic, ElevenLabs, HeyGen, Gemini,
  Pixabay) live behind a Cloudflare Worker; the app only holds a gateway token
  (from `config.local.json`). Never print or commit the token.
- **Cross-platform status:** this repo was Windows-first. A portability pass made
  the Python paths OS-aware — `server.py` `_vu_python()` (Scripts/python.exe on
  Windows vs bin/python on macOS) and a `$VU_PY` env var used in the UI prompts.
  If a pipeline step fails on macOS, the likely cause is a remaining
  Windows-specific assumption (a hardcoded `Scripts\python.exe`, a backslash
  path, or a `.exe` suffix). Grep for those, propose a minimal OS-aware fix, and
  note it for Sean rather than committing to the public repo yourself.
- **Do NOT** attempt to sign/notarize (intentionally unsigned), change the
  gateway, or commit secrets. Keep changes minimal and report findings.
