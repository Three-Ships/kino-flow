# Kino Flow — installers

Bootstrap installers for the Kino Flow studio. They ship the app + a launcher;
on first run the launcher builds the light Python envs, ensures the Claude CLI,
and starts the studio in the browser. Assets come from Google Drive (auto-
detected), so nothing large is bundled.

## Build-time config (do this first)

```bash
cp installer/config.example.json installer/config.local.json
# edit config.local.json → set your gateway_token (and gateway_url if different)
```

`config.local.json` is **gitignored** — the repo is public, so the token never
gets committed. The build copies it in as `kino.config.json`.

## Prerequisites on each user's machine

- **Google Drive for Desktop** (assets live in a shared drive)
- **uv** (Python manager) and **Node.js** (for the Claude CLI) — the launcher
  installs the Claude CLI itself; uv/Node are lightweight prereqs the installer
  checks for. ffmpeg is bundled.

## Windows (.exe)  — buildable on Windows

Uses Inno Setup. See `windows/` (`kino-flow.iss` + `build.ps1`). Output:
`Kino-Flow-Setup.exe`. Unsigned → users click past the SmartScreen "More info →
Run anyway" prompt (internal use).

## macOS (.dmg)  — build on a Mac

See `mac/` (`build-dmg.sh`). Output: `Kino-Flow.dmg` containing `Kino Flow.app`.
Unsigned → users right-click → Open the first time (Gatekeeper).

> **Mac portability note:** the pipeline currently hardcodes Windows venv paths
> (`video-use/.venv/Scripts/python.exe`, ×13 in the UI + 3 in server.py). These
> must be made cross-platform before the Mac build will run. Tracked as the
> Mac-port pass — do it before shipping the `.dmg`.
