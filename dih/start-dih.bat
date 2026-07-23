@echo off
REM ── Dog is Human auto-editor launcher ─────────────────────────────────────
REM Creates its own venv (won't touch the studio or video-use venvs) and starts
REM the local web app on http://127.0.0.1:8770
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [dih] creating venv...
  uv venv .venv || python -m venv .venv
  echo [dih] installing dependencies...
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)

echo [dih] starting on http://127.0.0.1:8770
start "" http://127.0.0.1:8770
.venv\Scripts\python -m uvicorn server:app --host 127.0.0.1 --port 8770
