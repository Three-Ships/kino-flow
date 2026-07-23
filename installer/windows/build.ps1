# Build the Kino Flow Windows installer (Kino-Flow-Setup.exe).
# Preps the baked config, stages ffmpeg, generates the icon, then runs ISCC.
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$root = (Resolve-Path (Join-Path $here "..\..")).Path
Set-Location $here

# 1) Baked config - prefer the real (gitignored) config.local.json.
$cfgLocal   = Join-Path $here "..\config.local.json"
$cfgExample = Join-Path $here "..\config.example.json"
if (Test-Path $cfgLocal) {
  Copy-Item $cfgLocal (Join-Path $here "kino.config.json") -Force
  Write-Host "config: using config.local.json"
} else {
  Copy-Item $cfgExample (Join-Path $here "kino.config.json") -Force
  Write-Warning "config.local.json not found - baking the EXAMPLE (placeholder token). Create installer/config.local.json for a production build."
}

# 2) Stage ffmpeg (+ ffprobe) next to the installer.
$ffcmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffcmd) { throw "ffmpeg not found on PATH." }
$ffbin = Split-Path $ffcmd.Source
$dst = Join-Path $here "ffmpeg\bin"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item (Join-Path $ffbin "ffmpeg.exe") $dst -Force
if (Test-Path (Join-Path $ffbin "ffprobe.exe")) { Copy-Item (Join-Path $ffbin "ffprobe.exe") $dst -Force }
Write-Host "ffmpeg staged"

# 3) Icon from the logo (Pillow, in the video-use venv).
$logo = Join-Path $root "studio\static\kinoflow-logo.png"
$vupy = Join-Path $root "video-use\.venv\Scripts\python.exe"
$ico  = Join-Path $here "kino.ico"
if (-not (Test-Path $logo)) { throw "logo not found: $logo" }
if (-not (Test-Path $vupy)) { throw "video-use venv python not found: $vupy" }
& $vupy (Join-Path $here "_make_icon.py") $logo $ico
if (-not (Test-Path $ico)) { throw "icon generation failed" }

# 4) Compile.
$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { throw "ISCC not found at $iscc" }
& $iscc "kino-flow.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)" }
Write-Host "`nDONE -> $(Join-Path $here 'output\Kino-Flow-Setup.exe')"
