<#
  setup_veditor_addons.ps1 - one-shot setup for the Veditor add-ons.

  Installs/configures everything the staged helpers need, ON THIS MACHINE:
    * Ollama + a local copy model                 (for copy_gen.py)
    * Python deps: open_clip_torch, torch, pillow  (for broll_match.py)
    * Pexels / Pixabay API keys -> video-use/.env  (for broll_fetch.py)
    * Copies the three helpers into video-use/helpers/
    * Verifies each piece and prints a summary

  Easiest: double-click setup.bat (handles PowerShell + flags for you).

  Params:
      -CopyModel qwen2.5:7b    # Ollama model to pull. Blank = auto-pick from VRAM.
      -SkipOllama              # skip the Ollama install/pull
      -Cuda                    # force the CUDA build of torch (auto-detected otherwise)
#>
param(
  [string]$CopyModel = "",
  [switch]$SkipOllama,
  [switch]$Cuda
)

$ErrorActionPreference = "Stop"
function Say($m){ Write-Host ""; Write-Host ("=== " + $m + " ===") -ForegroundColor Cyan }
function Ok($m){ Write-Host ("  [ok] " + $m) -ForegroundColor Green }
function Warn($m){ Write-Host ("  [warn] " + $m) -ForegroundColor Yellow }

# Resolve paths (script lives in <repo>/cost-optimization)
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo     = Split-Path -Parent $Here
$VideoUse = Join-Path $Repo "video-use"
$Helpers  = Join-Path $VideoUse "helpers"
$Py       = Join-Path $VideoUse ".venv\Scripts\python.exe"
$EnvFile  = Join-Path $VideoUse ".env"
$results  = [ordered]@{}

if(!(Test-Path $Py)){ throw ("Python venv not found at " + $Py + ". Run from the repo, or fix the path.") }
Ok ("repo: " + $Repo)

# 1. Copy helpers into video-use/helpers
Say "Placing helpers into video-use/helpers"
foreach($f in "broll_match.py","broll_fetch.py","copy_gen.py"){
  $src = Join-Path $Here $f
  if(Test-Path $src){ Copy-Item $src (Join-Path $Helpers $f) -Force; Ok ("copied " + $f) }
  else { Warn ("missing " + $src) }
}
$results["helpers"] = ("copied to " + $Helpers)

# 2. Python deps for broll_match
# This venv was created with uv (no pip inside). Prefer 'uv pip', else bootstrap pip.
Say "Installing Python deps (open_clip_torch, pillow, torch)"
$useUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
if($useUv){ Ok "using uv (venv has no pip)" }
else {
  Warn "uv not found - bootstrapping pip via ensurepip"
  & $Py -m ensurepip --upgrade *> $null
  & $Py -m pip install --upgrade pip *> $null
}
function Install-Pkgs {
  param([string[]]$Pkgs, [string[]]$Extra=@())
  if($useUv){ & uv pip install --python "$Py" @Pkgs @Extra }
  else      { & $Py -m pip install @Pkgs @Extra }
  if($LASTEXITCODE -ne 0){ Warn ("install FAILED: " + ($Pkgs -join ' ')); return $false }
  Ok ("installed: " + ($Pkgs -join ' ')); return $true
}
$hasNvidia = $false
try { & nvidia-smi | Out-Null; $hasNvidia = $true } catch {}
$okDeps = (Install-Pkgs -Pkgs @("open_clip_torch","pillow","transformers"))
if($Cuda -or $hasNvidia){
  Warn "NVIDIA GPU detected - installing CUDA torch (cu121). Large download."
  $okDeps = (Install-Pkgs -Pkgs @("torch") -Extra @("--index-url","https://download.pytorch.org/whl/cu121")) -and $okDeps
} else {
  $okDeps = (Install-Pkgs -Pkgs @("torch")) -and $okDeps
}
$results["python_deps"] = if($okDeps){ "open_clip_torch, pillow, torch" } else { "FAILED - see warnings above" }

# Pick a copy model that fits the GPU
function Get-VramGB {
  try {
    $mb = (& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
    if($mb){ return [math]::Round([int]$mb/1024, 1) }
  } catch {}
  return 0
}
function Get-CopyModel($vram){
  if($vram -ge 16){ return "qwen3:14b" }   # ~9GB
  if($vram -ge 8) { return "qwen2.5:7b" }   # ~4.7GB - great copywriter, safe default
  if($vram -ge 4) { return "qwen2.5:3b" }   # ~2GB
  return "llama3.2:1b"                       # ~1.3GB / runs on CPU fine
}

# 3. Ollama + copy model
if(-not $SkipOllama){
  $vram = Get-VramGB
  if(-not $CopyModel){
    $CopyModel = Get-CopyModel $vram
    if($vram -gt 0){ Ok ("detected ~" + $vram + "GB VRAM -> using " + $CopyModel) }
    else { Warn ("no NVIDIA VRAM detected -> using " + $CopyModel + " (CPU-friendly)") }
  }
  Say ("Setting up Ollama + " + $CopyModel)
  $ollama = Get-Command ollama -ErrorAction SilentlyContinue
  if(-not $ollama){
    Warn "Ollama not found - installing via winget"
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
    $env:Path = $env:Path + ";" + $env:LOCALAPPDATA + "\Programs\Ollama"
  } else { Ok "Ollama already installed" }
  Start-Process -WindowStyle Hidden ollama -ArgumentList "serve" -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
  Ok ("pulling " + $CopyModel + " (this can take a while)")
  ollama pull $CopyModel
  $results["ollama"] = ($CopyModel + " pulled")
} else { Warn "skipping Ollama (SkipOllama flag)"; $results["ollama"] = "skipped" }

# 4. API keys -> video-use/.env
Say "Pexels / Pixabay API keys (free - pexels.com/api, pixabay.com/api/docs)"
if(!(Test-Path $EnvFile)){ New-Item -ItemType File -Path $EnvFile | Out-Null }
$envText = Get-Content $EnvFile -Raw -ErrorAction SilentlyContinue
function Set-Key($name){
  if($envText -and $envText -match ("(?m)^" + $name + "=")){ Ok ($name + " already set"); return }
  $val = Read-Host ("  Enter " + $name + " (blank to skip)")
  if($val){ Add-Content $EnvFile ($name + "=" + $val); Ok ($name + " saved") }
  else { Warn ($name + " skipped - broll_fetch will error until it is set") }
}
Set-Key "PEXELS_API_KEY"
Set-Key "PIXABAY_API_KEY"
# Record the chosen copy model so copy_gen.py asks Ollama for the model we pulled.
if(-not $SkipOllama){
  if($envText -and $envText -match "(?m)^COPY_MODEL="){
    (Get-Content $EnvFile) -replace "(?m)^COPY_MODEL=.*", ("COPY_MODEL=" + $CopyModel) | Set-Content $EnvFile
  } else { Add-Content $EnvFile ("COPY_MODEL=" + $CopyModel) }
  Ok ("COPY_MODEL=" + $CopyModel + " written to .env")
}
$results["api_keys"] = ("written to " + $EnvFile)

# 5. Verify
Say "Verifying"
& $Py -c "import open_clip, torch, PIL; print('  torch', torch.__version__, '| cuda', torch.cuda.is_available())" 2>$null
if($LASTEXITCODE -eq 0){ Ok "imports work (open_clip + torch + PIL)" } else { Warn "imports FAILED - Python deps did not install; re-run (see deps warnings above)" }
& $Py (Join-Path $Helpers "broll_match.py") --help *> $null; if($LASTEXITCODE -eq 0){ Ok "broll_match.py runs" } else { Warn "broll_match.py --help failed" }
& $Py (Join-Path $Helpers "broll_fetch.py") --help *> $null; if($LASTEXITCODE -eq 0){ Ok "broll_fetch.py runs" } else { Warn "broll_fetch.py --help failed" }
& $Py (Join-Path $Helpers "copy_gen.py")   --help *> $null; if($LASTEXITCODE -eq 0){ Ok "copy_gen.py runs" } else { Warn "copy_gen.py --help failed" }
if(-not $SkipOllama){ ollama list *> $null; if($LASTEXITCODE -eq 0){ Ok "ollama responding" } else { Warn "ollama not responding yet - run 'ollama serve'" } }

Say "SUMMARY"
$results.GetEnumerator() | ForEach-Object { "  {0,-14} {1}" -f $_.Key, $_.Value }
Write-Host ""
Write-Host "Next: tell Claude Code to implement Workstream B in CLAUDE_CODE_HANDOFF.md." -ForegroundColor Cyan
