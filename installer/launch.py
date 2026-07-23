#!/usr/bin/env python3
"""Kino Flow launcher / first-run bootstrap (cross-platform).

Both the Windows (.exe) and macOS (.dmg) installers drop the app payload plus
this launcher and a `kino.config.json` (gateway URL + token, baked at build
time). Running it:

  1. loads config → sets KINO_GATEWAY_URL / KINO_GATEWAY_TOKEN in the env so the
     studio routes Claude through your shared-key gateway;
  2. ensures the Python venvs + light deps exist (creates them via `uv` on first
     run — the assets stay on Google Drive, so this stays small);
  3. ensures the Claude CLI is present (npm global);
  4. starts the studio (uvicorn) and opens the browser at 127.0.0.1:8765.

Assets come from Google Drive (auto-detected by the server), so nothing large
is bundled or downloaded here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ROOT = HERE                      # install dir: contains studio/ + video-use/
STUDIO = APP_ROOT / "studio"
VIDEO_USE = APP_ROOT / "video-use"
PORT = int(os.environ.get("KINO_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
IS_WIN = os.name == "nt"


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WIN else "bin/python")


def log(msg: str) -> None:
    print(f"[kino] {msg}", flush=True)


def load_config() -> dict:
    for name in ("kino.config.json", "config.local.json", "config.example.json"):
        p = APP_ROOT / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    return {}


def apply_config(cfg: dict) -> None:
    url = (cfg.get("gateway_url") or "").strip()
    token = (cfg.get("gateway_token") or "").strip()
    if url and not token.startswith("PASTE_"):
        os.environ["KINO_GATEWAY_URL"] = url
        if token:
            os.environ["KINO_GATEWAY_TOKEN"] = token
        log(f"gateway → {url}")
    else:
        log("no gateway configured — Claude will use the local login (dev mode)")


def _uv() -> str | None:
    return shutil.which("uv")


def ensure_venv(venv: Path, install_cmd: list[str], marker_import: str) -> None:
    """Create the venv + install deps if the marker import isn't available."""
    py = _venv_python(venv)
    if py.exists():
        probe = subprocess.run([str(py), "-c", f"import {marker_import}"],
                               capture_output=True)
        if probe.returncode == 0:
            return
    uv = _uv()
    if not uv:
        log("ERROR: `uv` is not installed. Install it from https://astral.sh/uv "
            "and re-launch.")
        sys.exit(2)
    log(f"first-run setup: building {venv.parent.name} environment (one time)…")
    if not py.exists():
        subprocess.run([uv, "venv", str(venv)], check=True)
    subprocess.run([uv, "pip", "install", "--python", str(py), *install_cmd], check=True)


def ensure_ffmpeg() -> None:
    """ffmpeg must be reachable. The installer bundles it (added to PATH above);
    as a fallback, install via Homebrew on macOS."""
    if shutil.which("ffmpeg"):
        return
    if not IS_WIN:
        brew = shutil.which("brew")
        if brew:
            log("installing ffmpeg via Homebrew (one time)…")
            subprocess.run([brew, "install", "ffmpeg"], check=False)
            return
    log("WARNING: ffmpeg not found. Install it (Windows: winget install Gyan.FFmpeg; "
        "macOS: brew install ffmpeg) and re-launch.")


def ensure_claude_cli() -> None:
    if shutil.which("claude"):
        return
    npm = shutil.which("npm")
    if not npm:
        log("WARNING: Node/npm not found — the Claude CLI can't be installed. "
            "Install Node.js, then re-launch. (Chat/Variants/Streamlined need it.)")
        return
    log("installing the Claude CLI (one time)…")
    subprocess.run([npm, "install", "-g", "@anthropic-ai/claude-code"], check=False)


def main() -> None:
    apply_config(load_config())

    # Bundled ffmpeg (installer ships it next to the launcher) → onto PATH.
    bundled_ff = APP_ROOT / "ffmpeg" / ("bin" if IS_WIN else "")
    if bundled_ff.exists():
        os.environ["PATH"] = str(bundled_ff) + os.pathsep + os.environ.get("PATH", "")

    # video-use env (helpers) — light deps; assets live on Drive.
    ensure_venv(VIDEO_USE / ".venv",
                ["requests", "librosa", "matplotlib", "pillow", "numpy"],
                marker_import="librosa")
    # studio env (web server).
    ensure_venv(STUDIO / ".venv",
                ["fastapi", "uvicorn[standard]", "python-multipart"],
                marker_import="fastapi")
    ensure_ffmpeg()
    ensure_claude_cli()

    studio_py = _venv_python(STUDIO / ".venv")
    log(f"starting Kino Flow at {URL}")
    proc = subprocess.Popen(
        [str(studio_py), "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(STUDIO),
    )
    # Give uvicorn a moment, then open the browser.
    time.sleep(2.5)
    try:
        webbrowser.open(URL)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
