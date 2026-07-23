@echo off
REM Kino Flow launcher (Windows). Ensures uv is available (it provides Python),
REM then runs the cross-platform launcher which bootstraps envs + starts the studio.
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo Installing uv ^(one-time setup^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

uv run --python 3.11 "%~dp0launch.py"
if errorlevel 1 (
  echo.
  echo Kino Flow exited with an error. Press any key to close.
  pause >nul
)
