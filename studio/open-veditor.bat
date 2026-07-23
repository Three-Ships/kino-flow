@echo off
REM Veditor Studio smart launcher.
REM   - If the server is already running, just open the browser.
REM   - Otherwise, start it in a separate window and wait until it's ready.
setlocal
set UV=C:\Users\seanh\AppData\Local\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe
set STUDIO_DIR=%~dp0
set URL=http://127.0.0.1:8765

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if %errorlevel% equ 0 (
    echo Veditor already running. Opening browser...
    start "" "%URL%"
    exit /b 0
)

echo Starting Veditor Studio...
start "Veditor Studio" /D "%STUDIO_DIR%" cmd /k ""%UV%" run uvicorn server:app --host 127.0.0.1 --port 8765"

REM Poll /api/health for up to ~10 seconds, then open the browser.
powershell -NoProfile -Command "for ($i = 0; $i -lt 20; $i++) { try { Invoke-WebRequest http://127.0.0.1:8765/api/health -UseBasicParsing -TimeoutSec 1 ^| Out-Null; exit 0 } catch { Start-Sleep -Milliseconds 500 } }; exit 1"

start "" "%URL%"
exit /b 0
