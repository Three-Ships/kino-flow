$Host.UI.RawUI.WindowTitle = "Aura Index"
Set-Location $PSScriptRoot
Clear-Host

Write-Host ""
Write-Host "  Starting Aura Index..." -ForegroundColor Cyan
Write-Host ""

# ── Install Python deps if needed ────────────────────────────────────────────
if (-not (Test-Path ".venv")) {
    Write-Host "  Setting up (first time, ~30 seconds)..."
    uv venv .venv --quiet
}
uv pip install fastapi "uvicorn[standard]" --quiet 2>$null

# ── Start the app server ──────────────────────────────────────────────────────
Write-Host "  Starting server..."
$serverProc = Start-Process `
    -FilePath ".\.venv\Scripts\uvicorn.exe" `
    -ArgumentList "app:app","--host","127.0.0.1","--port","3456" `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Minimized `
    -PassThru

# Wait until server responds
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        Invoke-RestMethod "http://127.0.0.1:3456/api/participants" -ErrorAction Stop | Out-Null
        $ready = $true; break
    } catch {}
}
if (-not $ready) {
    Write-Host ""
    Write-Host "  ERROR: Server failed to start." -ForegroundColor Red
    Write-Host "  Make sure you are running this from the color-quiz folder."
    Read-Host "`n  Press Enter to exit"
    exit 1
}

# ── Start cloudflared tunnel ──────────────────────────────────────────────────
Write-Host "  Opening secure tunnel (up to 20 seconds)..."
$logFile = Join-Path $env:TEMP "aura_cf.log"
if (Test-Path $logFile) { Remove-Item $logFile -Force }

$cfProc = Start-Process `
    -FilePath "$PSScriptRoot\cloudflared.exe" `
    -ArgumentList "tunnel","--url","http://127.0.0.1:3456","--no-autoupdate" `
    -RedirectStandardError $logFile `
    -WindowStyle Hidden `
    -PassThru

# Poll log for the trycloudflare.com URL
$url = ""
for ($i = 0; $i -lt 25; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Path $logFile) {
        $content = Get-Content $logFile -Raw -ErrorAction SilentlyContinue
        if ($content -match 'https://[a-zA-Z0-9-]+\.trycloudflare\.com') {
            $url = $Matches[0]; break
        }
    }
}

# ── Display result ────────────────────────────────────────────────────────────
Clear-Host
Write-Host ""

if ($url) {
    # Copy URL to clipboard
    $url | Set-Clipboard

    # Open admin dashboard
    Start-Process "http://localhost:3456/admin.html"

    Write-Host "  ==================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "    AURA INDEX IS LIVE" -ForegroundColor Green
    Write-Host ""
    Write-Host "  ==================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "    SEND THIS LINK TO YOUR TEAM:" -ForegroundColor White
    Write-Host ""
    Write-Host "      $url" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    (already copied to your clipboard)" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  ==================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "    YOUR DASHBOARD opened in your browser." -ForegroundColor White
    Write-Host "    If it didn't open:" -ForegroundColor Gray
    Write-Host "      http://localhost:3456/admin.html" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  ==================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "    Keep this window open for the whole meeting." -ForegroundColor White
    Write-Host "    Close it when done to shut everything down." -ForegroundColor Gray
    Write-Host ""
    Write-Host "  ==================================================" -ForegroundColor Green
    Write-Host ""

} else {
    Write-Host "  Server is running but tunnel URL timed out." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Try opening a second terminal and running:" -ForegroundColor White
    Write-Host "    .\cloudflared.exe tunnel --url http://127.0.0.1:3456" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Your admin dashboard: http://localhost:3456/admin.html" -ForegroundColor Gray
    Write-Host ""
}

Read-Host "  Press Enter to shut everything down"

# ── Cleanup ───────────────────────────────────────────────────────────────────
Write-Host "  Shutting down..." -ForegroundColor Gray
if ($cfProc     -and -not $cfProc.HasExited)     { Stop-Process -Id $cfProc.Id     -Force -ErrorAction SilentlyContinue }
if ($serverProc -and -not $serverProc.HasExited) { Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue }
