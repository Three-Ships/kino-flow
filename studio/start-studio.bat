@echo off
REM Veditor Studio launcher. Double-click to start, Ctrl+C in the window to stop.
setlocal
set UV=C:\Users\seanh\AppData\Local\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe
cd /d "%~dp0"
echo ==========================================
echo   Veditor Studio  http://127.0.0.1:8765
echo   Ctrl+C to stop
echo ==========================================
"%UV%" run uvicorn server:app --host 127.0.0.1 --port 8765
pause
