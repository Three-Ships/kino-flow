@echo off
REM ============================================================
REM  Veditor add-ons setup launcher
REM  Double-click this file, OR run it from CMD:
REM      "E:\Claude Veditor\cost-optimization\setup.bat"
REM  It runs the PowerShell setup with the right flags for you.
REM  Optional model override:  setup.bat -CopyModel qwen2.5:3b
REM ============================================================
setlocal
cd /d "%~dp0"

REM Warn (do not block) if not running as administrator - Ollama installer may need it.
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo.
  echo [notice] Not running as administrator.
  echo          Python deps + API keys will still work. If the Ollama install
  echo          fails, right-click setup.bat and choose "Run as administrator".
  echo.
)

echo Launching Veditor add-ons setup...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_veditor_addons.ps1" %*

echo.
echo ============================================================
echo  Setup finished (scroll up for the summary / any warnings).
echo ============================================================
pause
