@echo off
cd /d "%~dp0"
title Aura Index

echo.
echo  ◎  Aura Index
echo  ─────────────────────────────
echo.

if not exist .venv (
    echo  Creating virtual environment...
    uv venv .venv --quiet
)

echo  Installing dependencies...
uv pip install fastapi "uvicorn[standard]" --quiet

echo.
echo  Server starting at:
echo.
echo    Quiz    →  http://localhost:3456
echo    Results →  http://localhost:3456/admin.html
echo.
echo  Press Ctrl+C to stop.
echo.

.venv\Scripts\uvicorn.exe app:app --host 0.0.0.0 --port 3456

pause
