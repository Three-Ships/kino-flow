"""Veditor Studio — web shell for the Claude-powered video editing project."""
from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(os.environ.get("VEDITOR_ROOT") or Path(__file__).resolve().parent.parent)
VIDEOS_DIR = PROJECT_ROOT / "videos"
EDIT_DIR = VIDEOS_DIR / "edit"
# Final deliverables land here — one subfolder per run — so completed videos
# are easy to find in the project root instead of buried under videos/edit/.
# Intermediates (proxies, EDLs, tmp) still live under videos/edit/.
FINAL_DIR = PROJECT_ROOT / "Final Output"
STATIC_DIR = Path(__file__).parent / "static"


def _vu_python() -> Path:
    """Path to the video-use venv's Python, per-OS (Scripts\\python.exe on
    Windows, bin/python on macOS/Linux). Used for the server's direct helper
    runs; the same relative form is injected as $VU_PY for the agent's shell."""
    base = PROJECT_ROOT / "video-use" / ".venv"
    return base / "Scripts" / "python.exe" if os.name == "nt" else base / "bin" / "python"


# Relative form of the above, for prompts the agent runs (cwd = project root).
VU_PY_REL = ("video-use/.venv/Scripts/python.exe" if os.name == "nt"
             else "video-use/.venv/bin/python")

# HARD cap on concurrent heavy renders (variant_factory ffmpeg/NVENC jobs).
# Each render decodes large 4K/10-bit b-roll and runs an NVENC encode. On a
# VRAM-limited laptop GPU (this box: RTX 5070 Laptop, 4 GB VRAM, 15 GB RAM),
# firing a whole batch concurrently overcommits VRAM+RAM and HARD-FREEZES the
# machine (confirmed 2026-07-07 — Kernel-Power 41, no bugcheck). This semaphore
# enforces the limit SERVER-SIDE so no prompt/agent behavior can bypass it:
# excess requests queue and run as slots free up. Override via VEDITOR_MAX_RENDERS.
try:
    MAX_CONCURRENT_RENDERS = max(1, int(os.environ.get("VEDITOR_MAX_RENDERS", "2")))
except ValueError:
    MAX_CONCURRENT_RENDERS = 2
_render_sem: "asyncio.Semaphore | None" = None

def _render_semaphore() -> "asyncio.Semaphore":
    # Created lazily so it binds to the running event loop.
    global _render_sem
    if _render_sem is None:
        _render_sem = asyncio.Semaphore(MAX_CONCURRENT_RENDERS)
    return _render_sem


# Proxy builds must be SERIALIZED (not just capped at 2), or two concurrent
# requests transcode the same clip to the same temp path and collide → 500 with
# orphaned .tmp.mp4 files (Caio's macOS report). Since proxy builds are
# idempotent, the second caller waits then finds the proxy already built.
_proxy_lock: "asyncio.Lock | None" = None


def _proxy_build_lock() -> "asyncio.Lock":
    global _proxy_lock
    if _proxy_lock is None:
        _proxy_lock = asyncio.Lock()
    return _proxy_lock

CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude")
              or (r"C:\Users\seanh\AppData\Roaming\npm\claude.cmd" if os.name == "nt" else "claude"))

# ── Shared-key gateway (production distribution) ─────────────────────
# In the distributed desktop app the user's machine must NOT hold the real
# Anthropic key. When KINO_GATEWAY_URL is set, route the spawned `claude`
# process through the Cloudflare Worker gateway (see gateway/): the worker holds
# the real key and the app sends only a per-user token. Unset (the default and
# the current local dev setup) → inherit the ambient env / local claude login
# exactly as before, so this change is a no-op until the gateway is deployed.
KINO_GATEWAY_URL = os.environ.get("KINO_GATEWAY_URL", "").rstrip("/")
KINO_GATEWAY_TOKEN = os.environ.get("KINO_GATEWAY_TOKEN", "")

# Dev convenience (per Caio's macOS report): when the gateway env isn't set —
# e.g. running `uvicorn server:app` directly instead of via installer/launch.py —
# fall back to the local build config so provider-key steps (Variants TTS,
# avatars, gen) still work. The packaged app sets the env in launch.py, so this
# branch is a no-op there.
if not KINO_GATEWAY_URL:
    try:
        _gcfg = json.loads((PROJECT_ROOT / "installer" / "config.local.json").read_text(encoding="utf-8"))
        _gu = (_gcfg.get("gateway_url") or "").strip().rstrip("/")
        _gt = (_gcfg.get("gateway_token") or "").strip()
        if _gu and not _gt.startswith("PASTE_"):
            KINO_GATEWAY_URL, KINO_GATEWAY_TOKEN = _gu, _gt
    except Exception:  # noqa: BLE001
        pass


def _claude_env() -> dict:
    """Environment for the spawned claude CLI. Routes Claude through the
    shared-key gateway when configured, else returns the ambient environment
    unchanged (local claude auth)."""
    env = os.environ.copy()
    # OS-correct path to the video-use Python, so agent prompts can call
    # `$VU_PY helper.py` and work on both Windows and macOS.
    env["VU_PY"] = VU_PY_REL
    if KINO_GATEWAY_URL:
        env["ANTHROPIC_BASE_URL"] = f"{KINO_GATEWAY_URL}/anthropic"
        # A stray local key would shadow the gateway routing — remove it.
        env.pop("ANTHROPIC_API_KEY", None)
        # Send the guard token when the gateway requires one (recommended).
        if KINO_GATEWAY_TOKEN:
            env["ANTHROPIC_AUTH_TOKEN"] = KINO_GATEWAY_TOKEN
    return env


# ── Shared asset root on Google Drive ────────────────────────────────
# Team assets (per-team B-Roll/HOOKS/CTAs/Music/Guidelines) live in a shared
# Google Drive folder that Google Drive for Desktop mounts locally. The path
# INSIDE Drive is identical on every machine — only the mount root differs
# (drive letter on Windows; ~/Library/CloudStorage on macOS). We detect the
# mount so every install points at the same central assets with no per-machine
# config. Override with KINO_ASSET_ROOT to force an explicit path.
ASSET_REL_PARTS = ("Shared drives", "Home Solutions Team Drive", "Social",
                   "19 - Creative Assets", "Kino Flow - Asset Folder")
_asset_root_cache: "str | None" = None


def detect_asset_root() -> str:
    """Locate the shared Kino asset folder on the local Google Drive mount.
    Returns "" if Drive isn't mounted / the folder isn't found. Caches only a
    successful hit, so a Drive mounted after startup is still picked up."""
    global _asset_root_cache
    override = os.environ.get("KINO_ASSET_ROOT", "").strip()
    if override:
        return override
    if _asset_root_cache:
        return _asset_root_cache
    rel = Path(*ASSET_REL_PARTS)
    candidates: list[Path] = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            candidates.append(Path(f"{letter}:\\") / rel)
    else:
        home = Path.home()
        candidates += [b / rel for b in sorted(home.glob("Library/CloudStorage/GoogleDrive-*"))]
        candidates.append(Path("/Volumes/GoogleDrive") / rel)
        vol = Path("/Volumes")
        if vol.exists():
            candidates += [b / rel for b in sorted(vol.glob("GoogleDrive-*"))]
    for c in candidates:
        try:
            if c.is_dir():
                _asset_root_cache = str(c)
                return _asset_root_cache
        except OSError:
            continue
    return ""


# ── Output folder (user-selectable) ──────────────────────────────────
# Finished deliverables default to FINAL_DIR ("Final Output/"). The user can
# redirect them to any local folder (e.g. a Google Drive path) from the app.
# Persisted here; read by the streamlined finalizer and surfaced to the client
# so the Variants/Dice prompts write to the same place. NOTE: the in-app Files
# browser can only preview outputs that live under the project root; external
# folders still receive the files, and each job reports its exact output path.
OUTPUT_CONFIG_FILE = EDIT_DIR / "output_config.json"


def _load_output_dir() -> str:
    try:
        data = json.loads(OUTPUT_CONFIG_FILE.read_text(encoding="utf-8"))
        return (data.get("dir") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def output_root() -> Path:
    """Resolved destination for finished deliverables (custom folder if set,
    else the default Final Output/)."""
    custom = _load_output_dir()
    if custom:
        return Path(heal_drive_letter(custom))
    return FINAL_DIR

# ── Model routing (cost-optimization Workstream A1) ──────────────────
# Job type → Claude model, so copywriting/deterministic-render jobs run on
# Sonnet/Haiku and only genuine multi-step reasoning gets Opus. Config in
# studio/model_routing.json. lru_cache → edits need a reload (it's config).
MODEL_ROUTING_FILE = Path(__file__).resolve().parent / "model_routing.json"


@functools.lru_cache(maxsize=1)
def _routing_config() -> dict:
    try:
        return json.loads(MODEL_ROUTING_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        # Fail safe: never block a job because the config is missing/bad.
        return {"default": "sonnet", "routes": {}, "prompt_markers": {}}


def route_model(prompt: str, requested: str = "", force: bool = False) -> str:
    """Pick a Claude model for a job.

    - `force` + a non-empty `requested` → the caller's explicit choice wins
      (the interactive/sync model pickers set this).
    - Otherwise classify from the prompt head via the marker map. If nothing
      matches, honor `requested` if given, else the configured default — so a
      plain interactive chat keeps whatever the picker sent.
    """
    cfg = _routing_config()
    if force and requested:
        return requested
    head = (prompt or "")[:200].upper()
    for marker, job_type in (cfg.get("prompt_markers") or {}).items():
        if marker.startswith("_"):          # skip the JSON _comment entry
            continue
        if marker in head:
            model = (cfg.get("routes") or {}).get(job_type)
            if model:
                return model
    return requested or cfg.get("default", "sonnet")


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
SCRIPT_EXTS = {".txt", ".md"}
SUBTITLE_EXTS = {".srt", ".vtt"}
JSON_EXTS = {".json", ".md"}

app = FastAPI(title="Veditor Studio")


def _safe(path: Path) -> Path:
    """Reject paths that escape the project root."""
    resolved = (PROJECT_ROOT / path).resolve()
    if PROJECT_ROOT.resolve() not in resolved.parents and resolved != PROJECT_ROOT.resolve():
        raise HTTPException(status_code=403, detail="path escapes project root")
    return resolved


def heal_drive_letter(path: str | Path) -> Path:
    """Self-heal a stale Windows drive letter on an absolute path.

    The studio + its assets live on an external SSD whose drive letter changes
    between mounts. Saved config (asset root, b-roll folders) is stored as an
    absolute path in the browser, so after a remount it points at a dead letter
    (e.g. `D:\\...\\B-Roll` when the SSD is now `E:`). If the given path doesn't
    exist but the SAME path exists under a different drive letter, return that
    one. Non-Windows / relative / already-valid paths pass through unchanged."""
    p = Path(path)
    if p.exists():
        return p
    drive = os.path.splitdrive(str(p))[0]   # e.g. "D:"
    if not (len(drive) == 2 and drive[1] == ":"):
        return p  # not a drive-letter-absolute path; nothing to heal
    rest = str(p)[len(drive):]              # "\01. Home Solutions\..."
    for letter in "EDFGHIJKLMNOPQRSTUVWXYZCAB":
        cand = Path(f"{letter}:{rest}")
        if cand.exists():
            return cand
    return p  # give up — let the caller's existence check raise as usual


# ─────────────────────────────────────────────────────────────────────
# Progress registry — time-based ETA for long renders. Any endpoint can
# register a job; the client polls GET /api/progress?job=<id> while its
# render POST is still in flight (FastAPI serves both concurrently). The
# percent is elapsed/estimate capped at 95% until the job is marked done,
# then it jumps to 100 — honest for an estimate, no false precision.
# ─────────────────────────────────────────────────────────────────────
_PROGRESS: dict[str, dict] = {}


def _progress_prune() -> None:
    now = time.monotonic()
    for k in [k for k, v in _PROGRESS.items()
              if v.get("finished") and now - v["finished"] > 120]:
        _PROGRESS.pop(k, None)


def _progress_start(job_id: str, label: str, estimate_s: float,
                    phase: str = "starting") -> None:
    if not job_id:
        return
    _progress_prune()
    _PROGRESS[job_id] = {
        "label": label, "phase": phase, "started": time.monotonic(),
        "estimate_s": max(1.0, float(estimate_s)), "done": False,
        "error": None, "output": None, "finished": None}


def _progress_phase(job_id: str, phase: str, add_estimate_s: float = 0.0) -> None:
    j = _PROGRESS.get(job_id)
    if j:
        j["phase"] = phase
        if add_estimate_s:
            j["estimate_s"] += add_estimate_s


def _progress_done(job_id: str, output: str | None = None,
                   error: str | None = None) -> None:
    j = _PROGRESS.get(job_id)
    if j:
        j.update(done=True, output=output, error=error, finished=time.monotonic())


@app.get("/api/progress")
async def get_progress(job: str):
    j = _PROGRESS.get(job)
    if not j:
        return {"found": False}
    elapsed = time.monotonic() - j["started"]
    if j["done"]:
        pct, eta = 100, 0
    else:
        pct = min(95, int(round(100 * elapsed / j["estimate_s"])))
        eta = max(0, int(round(j["estimate_s"] - elapsed)))
    return {"found": True, "label": j["label"], "phase": j["phase"],
            "percent": pct, "eta_s": eta, "done": j["done"],
            "error": j["error"], "output": j["output"]}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    target = VIDEOS_DIR / file.filename
    with target.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    return {
        "name": file.filename,
        "path": str(target.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "size": target.stat().st_size,
    }


@app.post("/api/chat")
async def chat(
    prompt: str = Form(...),
    continue_session: bool = Form(True),
    resume_session_id: str = Form(""),
    model: str = Form(""),
    force_model: bool = Form(False),   # UI/sync pickers set this; batch runs don't
):
    if not CLAUDE_BIN or not Path(CLAUDE_BIN).exists():
        raise HTTPException(500, f"claude CLI not found at {CLAUDE_BIN}")

    args = [CLAUDE_BIN, "--print", "--output-format", "stream-json", "--verbose"]

    # ── Model routing (Workstream A1) ──
    # Accepts CLI shortcuts ('opus'/'sonnet'/'haiku') or full model IDs.
    resolved_model = route_model(prompt, requested=model, force=force_model)
    args.extend(["--model", resolved_model])

    # ── Session hygiene (Workstream A3) ──
    # Autonomous, pre-authorized batch runs are self-contained — don't inherit
    # (and re-cache) the whole prior conversation. Detect them by prompt marker.
    head = prompt[:200].upper()
    is_autonomous = any(m in head for m in (
        "VARIANT FACTORY", "STREAMLINED AD", "ROLL THE DICE"))

    # Permission mode. Autonomous, pre-authorized generation (the user clicked
    # Generate) runs with bypassPermissions so it works in the packaged app with
    # NO reliance on a .claude/settings.local.json allowlist (which isn't in the
    # bundle). Interactive chat keeps acceptEdits (auto-accept edits, prompt for
    # other actions). Fix per Caio's macOS build report, 2026-07-23.
    args += ["--permission-mode",
             "bypassPermissions" if is_autonomous else "acceptEdits"]

    # Precedence: explicit --resume <id> > --continue > fresh session.
    if resume_session_id:
        args.extend(["--resume", resume_session_id])
    elif continue_session and not is_autonomous:
        args.append("--continue")
    # else: fresh session (autonomous batch, or caller asked not to continue)
    # Pass the prompt via stdin instead of argv. On Windows, multi-line argv
    # arguments with embedded newlines or non-ASCII chars (em-dashes, etc.)
    # get truncated at the first newline by the child process's command-line
    # re-parser. stdin sidesteps that entirely.

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(PROJECT_ROOT),
        env=_claude_env(),  # routes through the shared-key gateway when configured
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,  # 16MB per line — stream-json tool results can be large
    )
    assert proc.stdin is not None
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    async def stream() -> AsyncIterator[bytes]:
        try:
            assert proc.stdout is not None
            while True:
                try:
                    line = await proc.stdout.readline()
                except asyncio.LimitOverrunError as e:
                    # Drain & skip the oversized line; report it as a system event.
                    await proc.stdout.readexactly(e.consumed)
                    skipped = {"type": "system", "subtype": "skipped_oversize_event",
                               "consumed": e.consumed}
                    yield b"data: " + json.dumps(skipped).encode() + b"\n\n"
                    continue
                if not line:
                    break
                # Heartbeat-friendly: each event is its own SSE message.
                yield b"data: " + line.rstrip(b"\n") + b"\n\n"
            rc = await proc.wait()
            tail = {"type": "_done", "exit_code": rc}
            if rc != 0 and proc.stderr is not None:
                err = (await proc.stderr.read()).decode(errors="replace")
                tail["stderr"] = err[-2000:]
            yield b"data: " + json.dumps(tail).encode() + b"\n\n"
        except asyncio.CancelledError:
            proc.kill()
            raise

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/files")
async def list_files():
    """List source videos and edit artifacts."""
    sources = []
    if VIDEOS_DIR.exists():
        for f in VIDEOS_DIR.iterdir():
            ext = f.suffix.lower()
            if f.is_file() and ext in (VIDEO_EXTS | AUDIO_EXTS | SCRIPT_EXTS):
                if ext in VIDEO_EXTS:
                    kind = "video"
                elif ext in AUDIO_EXTS:
                    kind = "audio"
                else:
                    kind = "script"
                sources.append({
                    "path": str(f.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                    "name": f.name,
                    "kind": kind,
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                })

    artifacts = []
    if EDIT_DIR.exists():
        for f in EDIT_DIR.rglob("*"):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in VIDEO_EXTS | AUDIO_EXTS | SUBTITLE_EXTS | JSON_EXTS:
                if ext in VIDEO_EXTS:
                    kind = "video"
                elif ext in AUDIO_EXTS:
                    kind = "audio"
                elif ext in SUBTITLE_EXTS:
                    kind = "subtitle"
                else:
                    kind = "doc"
                artifacts.append({
                    "path": str(f.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                    "name": str(f.relative_to(EDIT_DIR)).replace("\\", "/"),
                    "kind": kind,
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                })

    # Final deliverables in "Final Output/<run>/…". Surfaced alongside edit
    # artifacts so the Files-tab Outputs view groups them into per-run cards
    # (it keys on the first segment of `name`). Marked final=True so the client
    # can flag them as the headline deliverable.
    if FINAL_DIR.exists():
        for f in FINAL_DIR.rglob("*"):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in VIDEO_EXTS | AUDIO_EXTS | SUBTITLE_EXTS | JSON_EXTS:
                kind = (
                    "video" if ext in VIDEO_EXTS
                    else "audio" if ext in AUDIO_EXTS
                    else "subtitle" if ext in SUBTITLE_EXTS
                    else "doc"
                )
                artifacts.append({
                    "path": str(f.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                    "name": str(f.relative_to(FINAL_DIR)).replace("\\", "/"),
                    "kind": kind,
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                    "final": True,
                })

    return {
        "sources": sorted(sources, key=lambda x: -x["mtime"]),
        "artifacts": sorted(artifacts, key=lambda x: -x["mtime"]),
    }


@app.get("/api/file/{path:path}")
async def get_file(path: str):
    target = _safe(Path(path))
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


@app.delete("/api/file/{path:path}")
async def delete_file(path: str):
    target = _safe(Path(path))
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "file not found")
    target.unlink()
    return {"deleted": str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")}


USAGE_LOG = EDIT_DIR / "usage.log"


@app.post("/api/usage")
async def append_usage(
    prompt: str = Form(""),
    turn: int = Form(0),
    input_tokens: int = Form(0),
    output_tokens: int = Form(0),
    cache_read: int = Form(0),
    cache_create: int = Form(0),
    cost_usd: float = Form(0.0),
    duration_ms: int = Form(0),
    is_error: bool = Form(False),
):
    """Append one turn's usage to videos/edit/usage.log as JSONL."""
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "turn": turn,
        "prompt": (prompt or "")[:280],
        "in": input_tokens,
        "out": output_tokens,
        "cache_read": cache_read,
        "cache_create": cache_create,
        "cost_usd": round(cost_usd, 6),
        "duration_ms": duration_ms,
        "is_error": is_error,
    }
    with USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return {"ok": True, "logged": str(USAGE_LOG.relative_to(PROJECT_ROOT)).replace("\\", "/")}


@app.get("/api/usage/summary")
async def usage_summary():
    """Tally usage.log into project lifetime totals."""
    totals = {
        "turns": 0, "in": 0, "out": 0,
        "cache_read": 0, "cache_create": 0,
        "cost_usd": 0.0, "errors": 0,
    }
    if USAGE_LOG.exists():
        for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            totals["turns"] += 1
            totals["in"] += r.get("in", 0)
            totals["out"] += r.get("out", 0)
            totals["cache_read"] += r.get("cache_read", 0)
            totals["cache_create"] += r.get("cache_create", 0)
            totals["cost_usd"] += r.get("cost_usd", 0.0)
            if r.get("is_error"):
                totals["errors"] += 1
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    totals["log_path"] = str(USAGE_LOG.relative_to(PROJECT_ROOT)).replace("\\", "/")
    totals["log_exists"] = USAGE_LOG.exists()
    return totals


SESSIONS_FILE = EDIT_DIR / "sessions.json"


def _load_sessions_file() -> list:
    if not SESSIONS_FILE.exists():
        return []
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _write_sessions_file(records: list) -> None:
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(records, indent=2), encoding="utf-8")


@app.post("/api/sessions/save")
async def session_save(payload: dict):
    """Mirror a frontend session record to disk so it survives across browsers."""
    if not isinstance(payload, dict) or "id" not in payload:
        raise HTTPException(400, "missing session id")
    records = _load_sessions_file()
    sid = payload["id"]
    idx = next((i for i, r in enumerate(records) if r.get("id") == sid), -1)
    record = {
        "id": sid,
        "name": payload.get("name") or f"Session {sid[:8]}",
        "video": payload.get("video"),
        "started": payload.get("started")
                   or (records[idx].get("started") if idx >= 0 else None),
        "lastActive": payload.get("lastActive"),
        "turns": payload.get("turns", 0),
        "cost": payload.get("cost", 0.0),
    }
    if idx >= 0:
        records[idx] = record
    else:
        records.insert(0, record)
    _write_sessions_file(records)
    return {"ok": True, "count": len(records)}


@app.get("/api/sessions")
async def session_list():
    return {"sessions": _load_sessions_file()}


@app.delete("/api/sessions/{sid}")
async def session_delete(sid: str):
    records = _load_sessions_file()
    new_records = [r for r in records if r.get("id") != sid]
    _write_sessions_file(new_records)
    return {"deleted": len(records) - len(new_records)}


JOBS_FILE = EDIT_DIR / "jobs.jsonl"


@app.post("/api/jobs/start")
async def jobs_start(payload: dict):
    """Start a new job. Body: {id, prompt, started_at, source_files?}.
    The job is appended to jobs.jsonl as an open record (completed_at=None).
    """
    if not isinstance(payload, dict) or "id" not in payload:
        raise HTTPException(400, "missing job id")
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": payload["id"],
        "prompt": (payload.get("prompt") or "")[:1000],
        "started_at": payload.get("started_at"),
        "completed_at": None,
        "source_files": payload.get("source_files") or [],
        "operations": [],
        "turns": 0,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cache": 0,
        "wall_clock_ms": 0,
        "output_files": [],
        # Log the model that was actually SPAWNED, not the picker default —
        # route the same way /api/chat does so telemetry reflects routing.
        "model": route_model(payload.get("prompt") or "",
                             payload.get("model") or "",
                             bool(payload.get("force_model"))),
    }
    with JOBS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return {"ok": True, "id": record["id"]}


def _rewrite_jobs(records: list) -> None:
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with JOBS_FILE.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _load_jobs() -> list:
    if not JOBS_FILE.exists():
        return []
    out: list = []
    for line in JOBS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@app.post("/api/jobs/update")
async def jobs_update(payload: dict):
    """Update an existing job. Body: full or partial record with `id`.
    Adds operations to the existing list, accumulates token/cost totals.
    """
    if not isinstance(payload, dict) or "id" not in payload:
        raise HTTPException(400, "missing job id")
    records = _load_jobs()
    sid = payload["id"]
    found = False
    for i, r in enumerate(records):
        if r.get("id") != sid:
            continue
        found = True
        # Append-only fields
        for op in payload.get("operations") or []:
            r.setdefault("operations", []).append(op)
        for f in payload.get("output_files") or []:
            if f not in r.setdefault("output_files", []):
                r["output_files"].append(f)
        # Accumulators
        for k in ("turns", "cost_usd", "tokens_in", "tokens_out", "tokens_cache", "wall_clock_ms"):
            if k in payload:
                r[k] = (r.get(k) or 0) + payload[k]
        # Direct overrides
        for k in ("completed_at", "model", "prompt"):
            if k in payload and payload[k] is not None:
                r[k] = payload[k]
        records[i] = r
        break
    if not found:
        raise HTTPException(404, f"job not found: {sid}")
    _rewrite_jobs(records)
    return {"ok": True}


@app.get("/api/jobs")
async def jobs_list():
    """Return all jobs and aggregated efficiency stats per operation type."""
    records = _load_jobs()
    # Aggregate by operation tag.
    by_op: dict = {}
    for r in records:
        if not r.get("completed_at"):
            continue  # exclude open jobs from aggregates
        ops = r.get("operations") or []
        # An op signature for the whole job is the set of unique op tags.
        op_set = sorted({o.get("op") for o in ops if o.get("op")})
        for op in op_set:
            stats = by_op.setdefault(op, {
                "n": 0, "total_cost": 0.0, "total_wall_ms": 0,
                "total_turns": 0,
            })
            stats["n"] += 1
            stats["total_cost"] += r.get("cost_usd") or 0.0
            stats["total_wall_ms"] += r.get("wall_clock_ms") or 0
            stats["total_turns"] += r.get("turns") or 0
    for op, s in by_op.items():
        s["avg_cost"] = round(s["total_cost"] / s["n"], 4)
        s["avg_wall_ms"] = round(s["total_wall_ms"] / s["n"])
        s["avg_turns"] = round(s["total_turns"] / s["n"], 1)
        s["total_cost"] = round(s["total_cost"], 4)

    return {
        "jobs": records,
        "by_operation": by_op,
        "log_path": str(JOBS_FILE.relative_to(PROJECT_ROOT)).replace("\\", "/"),
    }


@app.delete("/api/jobs/{jid}")
async def jobs_delete(jid: str):
    records = _load_jobs()
    new_records = [r for r in records if r.get("id") != jid]
    _rewrite_jobs(new_records)
    return {"deleted": len(records) - len(new_records)}


@app.get("/api/folders")
async def list_folders():
    """List `videos/` and any subdirectories under it that contain at least
    one media file. Used by the auto-pair folder picker.
    """
    out = []
    if not VIDEOS_DIR.exists():
        return {"folders": out, "default": "videos"}

    def has_media(p: Path) -> bool:
        try:
            for f in p.iterdir():
                if f.is_file() and f.suffix.lower() in (VIDEO_EXTS | AUDIO_EXTS):
                    return True
        except Exception:
            return False
        return False

    if has_media(VIDEOS_DIR):
        out.append("videos")
    for sub in sorted(VIDEOS_DIR.iterdir()):
        if not sub.is_dir():
            continue
        rel = "videos/" + sub.name
        if has_media(sub):
            out.append(rel)
    return {"folders": out, "default": out[0] if out else "videos"}


@app.get("/api/folder/scan")
async def scan_folder(path: str):
    """Count videos and audios directly inside `path` (non-recursive).

    Accepts any directory the studio process can read — local disk anywhere,
    not just inside the project root. This is a read-only metadata listing
    (no file contents leave the box), so it's safe to allow arbitrary paths
    for a single-user local tool.
    """
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        raise HTTPException(400, "empty path")
    p = Path(raw)
    # Bare relative path (e.g. "videos") → resolve under project root.
    if not p.is_absolute():
        p = (PROJECT_ROOT / p)
    else:
        p = heal_drive_letter(p)   # remap stale SSD drive letter if needed
    try:
        target = p.resolve()
    except Exception as e:
        raise HTTPException(400, f"cannot resolve path: {e}")
    if not target.exists() or not target.is_dir():
        raise HTTPException(404, f"folder not found or not a directory: {target}")

    videos = []
    audios = []
    project_root = PROJECT_ROOT.resolve()
    for f in sorted(target.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        # Try to express the path relative to the project root if possible
        # (so it's still useful for the file browser); otherwise emit absolute.
        try:
            rel = str(f.resolve().relative_to(project_root)).replace("\\", "/")
            in_project = True
        except ValueError:
            rel = str(f.resolve()).replace("\\", "/")
            in_project = False
        info = {"path": rel, "name": f.name, "size": f.stat().st_size, "in_project": in_project}
        if ext in VIDEO_EXTS:
            videos.append(info)
        elif ext in AUDIO_EXTS:
            audios.append(info)

    try:
        folder_disp = str(target.relative_to(project_root)).replace("\\", "/")
        in_project_root = True
    except ValueError:
        folder_disp = str(target).replace("\\", "/")
        in_project_root = False

    return {
        "folder": folder_disp,
        "absolute_folder": str(target).replace("\\", "/"),
        "in_project": in_project_root,
        "videos": videos,
        "audios": audios,
    }


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "project_root": str(PROJECT_ROOT),
        "claude_bin": CLAUDE_BIN,
        "claude_found": bool(CLAUDE_BIN and Path(CLAUDE_BIN).exists()),
        # Gateway routing status (never echo the token itself).
        "gateway_enabled": bool(KINO_GATEWAY_URL and KINO_GATEWAY_TOKEN),
        "gateway_url": KINO_GATEWAY_URL or None,
        "asset_root": detect_asset_root() or None,
    }


@app.get("/api/asset-root")
async def asset_root():
    """Shared Google Drive asset root, detected for this machine. The client
    seeds it into localStorage so Variants/Streamlined find the central assets
    regardless of drive letter or OS."""
    root = detect_asset_root()
    return {"root": root or None, "found": bool(root)}


@app.get("/api/output-dir")
async def get_output_dir():
    """Where finished deliverables are saved (custom folder or default)."""
    custom = _load_output_dir()
    return {"dir": custom or None,
            "resolved": str(output_root()),
            "default": str(FINAL_DIR),
            "is_custom": bool(custom)}


@app.post("/api/output-dir")
async def set_output_dir(payload: dict):
    """Set (or clear) the output folder. Empty/absent `dir` resets to default.
    The folder is created if it doesn't exist."""
    raw = (payload.get("dir") or "").strip()
    if not raw:
        try:
            OUTPUT_CONFIG_FILE.unlink()
        except FileNotFoundError:
            pass
        return {"dir": None, "resolved": str(FINAL_DIR), "is_custom": False}
    p = Path(heal_drive_letter(raw))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"cannot use that folder: {e}")
    OUTPUT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_CONFIG_FILE.write_text(json.dumps({"dir": str(p)}, indent=2), encoding="utf-8")
    return {"dir": str(p), "resolved": str(p), "is_custom": True}


# ============================================================================
# Filesystem browser — for the left-side file tree pane.
# Local-only studio: no path restriction. Operator owns the box.
# ============================================================================

@app.get("/api/fs/roots")
async def fs_roots():
    """List drive letters on Windows or `/` on POSIX, plus pinned shortcuts."""
    _ar = detect_asset_root()
    pinned = []
    for label, path in [
        ("Home",           Path.home()),
        ("Desktop",        Path.home() / "Desktop"),
        ("Downloads",      Path.home() / "Downloads"),
        ("Assets (Drive)", Path(_ar) if _ar else None),
        ("Output",         output_root()),
        ("project",        PROJECT_ROOT),
        ("videos",         VIDEOS_DIR),
        ("edit",           EDIT_DIR),
    ]:
        try:
            if path and path.exists():
                pinned.append({"label": label, "path": str(path).replace("\\", "/")})
        except OSError:
            continue

    drives = []
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            p = Path(f"{letter}:\\")
            try:
                if p.exists():
                    drives.append({"label": f"{letter}:\\", "path": f"{letter}:/"})
            except OSError:
                continue
    else:
        drives.append({"label": "/", "path": "/"})

    return {"drives": drives, "pinned": pinned}


@app.get("/api/fs/list")
async def fs_list(path: str):
    """List children of an absolute filesystem path. Hidden files included.
    Returns dirs first, then files, alphabetical within each group."""
    p = heal_drive_letter(Path(path))   # remap stale SSD drive letter if needed
    try:
        p = p.resolve()
    except OSError as e:
        raise HTTPException(400, f"could not resolve path: {e}")
    if not p.exists():
        raise HTTPException(404, f"not found: {p}")
    if not p.is_dir():
        raise HTTPException(400, f"not a directory: {p}")

    entries = []
    try:
        children = list(p.iterdir())
    except PermissionError:
        raise HTTPException(403, f"permission denied: {p}")

    for child in children:
        try:
            is_dir = child.is_dir()
            stat = child.stat()
            entries.append({
                "name":  child.name,
                "path":  str(child).replace("\\", "/"),
                "is_dir": is_dir,
                "size":  stat.st_size if not is_dir else 0,
                "mtime": stat.st_mtime,
                "ext":   child.suffix.lower().lstrip(".") if not is_dir else "",
            })
        except (PermissionError, OSError):
            # Skip unreadable items rather than failing the whole listing.
            continue

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

    # Build parent path for the "↑" navigation row.
    parent = str(p.parent).replace("\\", "/") if p.parent != p else None

    return {
        "path":   str(p).replace("\\", "/"),
        "parent": parent,
        "entries": entries,
        "count":  len(entries),
    }


@app.post("/api/fs/import")
async def fs_import(payload: dict):
    """Copy a file from anywhere on disk into videos/ so it shows up in
    the dropdowns. Returns the project-relative path of the imported copy.
    De-duplicates by appending _N suffix when the destination already exists."""
    source = payload.get("source")
    if not source:
        raise HTTPException(400, "missing 'source' in body")
    src = Path(source)
    try:
        src = src.resolve()
    except OSError as e:
        raise HTTPException(400, f"could not resolve source: {e}")
    if not src.exists() or not src.is_file():
        raise HTTPException(404, f"file not found or not a regular file: {src}")

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    dest = VIDEOS_DIR / src.name
    if dest.exists() and dest.resolve() != src:
        stem, ext = dest.stem, dest.suffix
        n = 2
        while (VIDEOS_DIR / f"{stem}_{n}{ext}").exists():
            n += 1
        dest = VIDEOS_DIR / f"{stem}_{n}{ext}"

    if src == dest.resolve():
        # File is already inside videos/, no copy needed.
        rel = str(dest.relative_to(PROJECT_ROOT)).replace("\\", "/")
        return {"imported": rel, "size": dest.stat().st_size, "copied": False}

    shutil.copy2(src, dest)
    rel = str(dest.relative_to(PROJECT_ROOT)).replace("\\", "/")
    return {"imported": rel, "size": dest.stat().st_size, "copied": True}


@app.get("/api/timeline")
async def get_timeline():
    """Return track data for the timeline strip.

    Reads:
    - videos/edit/graphics_edl.json  → GFX track
    - the newest .srt in videos/edit/ → CAPS track
    - the newest edited video in videos/edit/ → VIDEO track + duration (via ffprobe)
    """
    import re
    import subprocess

    out: dict = {"duration": None, "tracks": {"video": [], "gfx": [], "caps": []}}

    # ── VIDEO track: find newest mp4/mov in edit dir, probe duration ──
    video_files = sorted(
        [f for f in EDIT_DIR.glob("*") if f.suffix.lower() in VIDEO_EXTS and f.is_file()],
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    duration: float | None = None
    if video_files:
        vf = video_files[0]
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(vf)],
                capture_output=True, text=True, timeout=10,
            )
            duration = float(probe.stdout.strip())
        except Exception:
            duration = None
        out["tracks"]["video"] = [{"start": 0, "end": duration or 0, "label": vf.name}]
        out["duration"] = duration

    # ── GFX track: read graphics_edl.json ──
    edl_path = EDIT_DIR / "graphics_edl.json"
    if edl_path.exists():
        try:
            edl = json.loads(edl_path.read_text(encoding="utf-8"))
            for entry in edl:
                at  = float(entry.get("at", 0))
                dur = float(entry.get("duration", 1))
                out["tracks"]["gfx"].append({
                    "start": at,
                    "end":   at + dur,
                    "label": entry.get("text", entry.get("kind", "")),
                })
        except Exception:
            pass

    # ── CAPS track: parse newest .srt ──
    srt_files = sorted(
        [f for f in EDIT_DIR.glob("*.srt") if f.is_file()],
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    if srt_files:
        srt_text = srt_files[0].read_text(encoding="utf-8", errors="replace")
        # Parse SRT timestamp blocks  -->  HH:MM:SS,mmm --> HH:MM:SS,mmm
        ts_rx = re.compile(
            r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)"
        )
        def ts_to_sec(ts: str) -> float:
            ts = ts.replace(",", ".")
            parts = ts.split(":")
            h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
            return h * 3600 + m * 60 + s

        caps = []
        for m in ts_rx.finditer(srt_text):
            caps.append({"start": ts_to_sec(m.group(1)), "end": ts_to_sec(m.group(2))})
        # Merge adjacent caps into visual blocks for the track (avoids thousands of tiny slices)
        merged: list[dict] = []
        GAP = 2.0  # seconds — gaps smaller than this are merged visually
        for c in sorted(caps, key=lambda x: x["start"]):
            if merged and c["start"] - merged[-1]["end"] < GAP:
                merged[-1]["end"] = max(merged[-1]["end"], c["end"])
            else:
                merged.append(dict(c))
        out["tracks"]["caps"] = merged
        out["tracks"]["caps_source"] = srt_files[0].name

    return out


# ─────────────────────────────────────────────────────────────────────
# Timeline v2 — per-run multi-track manifests for the editor timeline.
#
# A "run" is one subfolder of videos/edit/. The pipeline leaves EDLs +
# SRT/ASS behind (cut_edl.json, broll_edl.json, master.srt); variant
# runs leave <variant>.timeline.json sidecars. These endpoints turn
# that into track/clip data the timeline UI can draw and edit.
# ─────────────────────────────────────────────────────────────────────

def _probe_duration(path: Path) -> float | None:
    import subprocess
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(probe.stdout.strip())
    except Exception:
        return None


def _parse_srt_cues(text: str) -> list[dict]:
    """Full SRT parse — timing AND text per cue (the legacy endpoint only kept timing)."""
    import re
    cues: list[dict] = []
    ts_rx = re.compile(r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)")

    def ts_to_sec(ts: str) -> float:
        h, m, s = ts.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    for block in re.split(r"\r?\n\r?\n", text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        m = None
        text_lines: list[str] = []
        for ln in lines:
            tm = ts_rx.search(ln)
            if tm and m is None:
                m = tm
            elif m is not None:
                text_lines.append(ln.strip())
        if m:
            cues.append({
                "start": ts_to_sec(m.group(1)),
                "end": ts_to_sec(m.group(2)),
                "text": " ".join(text_lines),
            })
    return sorted(cues, key=lambda c: c["start"])


def _rel_or_abs(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(PROJECT_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


# Program-file preference: latest pipeline stage first.
_STAGE_ORDER = ["final.mp4", "cut_captioned.mp4", "cut_broll.mp4",
                "cut_leveled.mp4", "cut_audio.mp4", "cut.mp4"]


def _titles_track(d: Path) -> dict:
    """User-placed text overlays, persisted per-run in titles.json. Always
    present (possibly empty) on editable runs so the timeline has a lane
    to click titles onto."""
    clips = []
    tj = d / "titles.json"
    if tj.exists():
        try:
            for i, t in enumerate(json.loads(tj.read_text(encoding="utf-8"))):
                clips.append({
                    "id": f"t{i}",
                    "start": float(t.get("start", 0)),
                    "end": float(t.get("end", 0)),
                    "label": t.get("text", ""),
                    "text": t.get("text", ""),
                    "x": float(t.get("x", 0.5)),
                    "y": float(t.get("y", 0.42)),
                    "size": int(t.get("size", 96)),
                    "color": t.get("color", "#FFFFFF"),
                    "note": "",
                })
        except Exception:
            pass
    return {"id": "t1", "kind": "titles", "label": "TITLES", "clips": clips}


def _hex_to_ass(color: str) -> str:
    """#RRGGBB → ASS &H00BBGGRR."""
    c = (color or "#FFFFFF").lstrip("#")
    if len(c) != 6:
        c = "FFFFFF"
    return f"&H00{c[4:6]}{c[2:4]}{c[0:2]}"


def _write_titles_ass(titles: list[dict], width: int, height: int, out: Path) -> None:
    """Standalone ASS for user title overlays. Positions are fractional
    (0–1) of the frame; rendered with a bold outlined style so they read
    over any footage."""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        "Style: KinoTitle,Arial,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,4,2,5,40,40,40,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for t in titles:
        x = round(float(t.get("x", 0.5)) * width)
        y = round(float(t.get("y", 0.42)) * height)
        size = int(t.get("size", 96))
        ov = f"{{\\pos({x},{y})\\fs{size}\\c{_hex_to_ass(t.get('color', '#FFFFFF'))}}}"
        text = str(t.get("text", "")).replace("\n", "\\N")
        lines.append(f"Dialogue: 1,{_ass_ts(float(t['start']))},{_ass_ts(float(t['end']))},"
                     f"KinoTitle,,0,0,0,,{ov}{text}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _probe_dims(path: Path) -> tuple[int, int]:
    import subprocess
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        w, h = probe.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 1080, 1920


def _run_dir(run: str) -> Path:
    # Restrict to direct children of videos/edit — no traversal.
    name = Path(run).name
    d = EDIT_DIR / name
    if not d.exists() or not d.is_dir():
        raise HTTPException(404, f"run folder not found: {name}")
    return d


@app.get("/api/timeline/runs")
async def timeline_runs():
    """List edit runs, newest first, with a flag for whether real timeline
    data (EDLs / sidecars / SRT) exists vs. just a bare deliverable."""
    runs = []
    if not EDIT_DIR.exists():
        return {"runs": runs}
    for d in EDIT_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        videos = [f.name for f in d.iterdir()
                  if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        if not videos:
            continue
        has_edl = (d / "cut_edl.json").exists() or (d / "broll_edl.json").exists()
        sidecars = list(d.glob("*.timeline.json"))
        has_srt = any(d.glob("*.srt"))
        runs.append({
            "run": d.name,
            "mtime": d.stat().st_mtime,
            "videos": sorted(videos),
            "has_timeline": has_edl or bool(sidecars) or has_srt,
            "variant_run": bool(sidecars) or (not has_edl and any(v.startswith("variant_") for v in videos)),
        })
    runs.sort(key=lambda r: -r["mtime"])
    return {"runs": runs}


def _manifest_from_edls(d: Path) -> dict:
    """Standard pipeline run: cut_edl.json + broll_edl.json + master.srt."""
    tracks: list[dict] = [_titles_track(d)]
    duration: float | None = None
    editable = {"cut": False, "broll": False, "captions": False, "titles": True}

    # program file = newest pipeline stage present
    program: Path | None = None
    stages: dict[str, str] = {}
    for name in _STAGE_ORDER:
        f = d / name
        if f.exists():
            stages[name.rsplit(".", 1)[0]] = _rel_or_abs(f)
            if program is None:
                program = f
    if program is None:  # fall back to any video in the folder
        vids = sorted([f for f in d.iterdir() if f.suffix.lower() in VIDEO_EXTS],
                      key=lambda f: -f.stat().st_mtime)
        program = vids[0] if vids else None
    if program is not None:
        duration = _probe_duration(program)

    # ── V2: b-roll cutaways (already in output-timeline time) ──
    broll_path = d / "broll_edl.json"
    if broll_path.exists():
        try:
            edl = json.loads(broll_path.read_text(encoding="utf-8"))
            clips = []
            for i, seg in enumerate(edl):
                src = str(seg.get("source", ""))
                clips.append({
                    "id": f"b{i}",
                    "start": float(seg.get("start", 0)),
                    "end": float(seg.get("end", 0)),
                    "label": Path(src).name or f"broll {i}",
                    "source": src,
                    "source_in": float(seg.get("source_in", 0) or 0),
                    "note": seg.get("note", ""),
                })
            tracks.append({"id": "v2", "kind": "video", "label": "B-ROLL", "clips": clips})
            editable["broll"] = True
        except Exception:
            pass

    # ── V1 + A1: talking-head cut, source ranges mapped onto output time ──
    cut_path = d / "cut_edl.json"
    if cut_path.exists():
        try:
            cut = json.loads(cut_path.read_text(encoding="utf-8"))
            sources = cut.get("sources", {})
            clips = []
            cursor = 0.0
            for i, rng in enumerate(cut.get("ranges", [])):
                s, e = float(rng.get("start", 0)), float(rng.get("end", 0))
                src_key = rng.get("source", "src")
                src = str(sources.get(src_key, src_key))
                seg_dur = max(0.0, e - s)
                clips.append({
                    "id": f"c{i}",
                    "start": round(cursor, 3),
                    "end": round(cursor + seg_dur, 3),
                    "label": f"{Path(src).stem} · {s:.2f}–{e:.2f}",
                    "source": src,
                    "source_key": src_key,
                    "source_in": s,
                    "source_out": e,
                    "note": rng.get("note", ""),
                })
                cursor += seg_dur
            tracks.append({"id": "v1", "kind": "video", "label": "A-CAM", "clips": clips})
            tracks.append({"id": "a1", "kind": "audio", "label": "DIALOG",
                           "clips": [dict(c, id=c["id"].replace("c", "d")) for c in clips]})
            editable["cut"] = True
            if duration is None and clips:
                duration = clips[-1]["end"]
        except Exception:
            pass

    # ── CC: caption cues with text ──
    srts = sorted(d.glob("*.srt"), key=lambda f: -f.stat().st_mtime)
    if srts:
        try:
            cues = _parse_srt_cues(srts[0].read_text(encoding="utf-8", errors="replace"))
            tracks.append({
                "id": "cc", "kind": "captions", "label": "CAPTIONS",
                "source_file": _rel_or_abs(srts[0]),
                "clips": [dict(c, id=f"s{i}", label=c["text"]) for i, c in enumerate(cues)],
            })
            editable["captions"] = True
        except Exception:
            pass

    return {
        "program": _rel_or_abs(program) if program else None,
        "duration": duration,
        "stages": stages,
        "tracks": tracks,
        "editable": editable,
    }


def _sidecar_vo_exists(d: Path, video: str, data: dict) -> bool:
    """Can this sidecar run be re-muxed? Mirrors reassemble.py's discovery."""
    vo = data.get("vo_file")
    if vo and heal_drive_letter(Path(vo)).exists():
        return True
    if list(d.glob(f"{Path(video).stem}.vo.*")):
        return True
    vo_dir = d / "vo"
    return vo_dir.exists() and any(
        f.suffix.lower() in (".mp3", ".wav", ".m4a") for f in vo_dir.iterdir() if f.is_file())


def _sidecar_srt(d: Path, video: str) -> Path | None:
    """Exact-stem match first, else newest .srt in the run folder (hand-
    assembled runs name their SRT after the project, not the video)."""
    exact = d / (Path(video).stem + ".srt")
    if exact.exists():
        return exact
    srts = sorted(d.glob("*.srt"), key=lambda f: -f.stat().st_mtime)
    return srts[0] if srts else None


def _manifest_from_sidecar(d: Path, video: str) -> dict:
    """Sidecar run: <video>.timeline.json written by variant_factory or a
    hand-assembly (assemble.py). The concat IS the program timeline, so the
    clip track is v1 and edits ride the same cut-edit path as EDL runs."""
    vf = d / Path(video).name
    sidecar = d / (Path(video).name + ".timeline.json")
    if not sidecar.exists():
        raise HTTPException(404, f"no timeline sidecar for {video}")
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    duration = data.get("vo_duration_s") or _probe_duration(vf)

    is_streamlined = str(data.get("format", "")).startswith("streamlined-")
    tracks: list[dict] = [_titles_track(d)]

    # HOOK band — the streamlined-ad hook line, editable via the inspector.
    hook_text = data.get("hook")
    if hook_text:
        tracks.append({"id": "hk", "kind": "hook", "label": "HOOK", "clips": [{
            "id": "hk0", "start": 0.0, "end": float(duration or 0),
            "label": hook_text, "text": hook_text, "source": ""}]})

    clips = []
    cursor = 0.0
    for i, seg in enumerate(data.get("broll_clips", [])):
        src = str(seg.get("source", ""))
        s_in = float(seg.get("in", 0))
        s_out = float(seg.get("out", 0))
        seg_dur = max(0.0, s_out - s_in)
        clips.append({
            "id": f"b{i}",
            "start": round(cursor, 3),
            "end": round(cursor + seg_dur, 3),
            "label": Path(src).name,
            "source": src,
            "source_key": src,   # round-trips the real path through edits
            "source_in": s_in,
            "source_out": s_out,
            "note": seg.get("note", ""),
        })
        cursor += seg_dur
    tracks.append({"id": "v1", "kind": "video", "label": "B-ROLL", "clips": clips})

    # Only show the VO lane when a VO track actually exists on disk. Bullets
    # streamlined ads have no VO — showing an empty VO lane misled the eye.
    vo_present = _sidecar_vo_exists(d, video, data)
    if duration and vo_present:
        voice = data.get("voice_name") or data.get("voice_id", "")
        tracks.append({"id": "a1", "kind": "audio", "label": "VO", "clips": [{
            "id": "vo0", "start": 0.0, "end": float(duration),
            "label": f"ElevenLabs VO · {voice}",
            "source": "", "note": (data.get("script_text") or data.get("script") or "")[:200],
        }]})

    # MUSIC bed — swap/remove on re-render (streamlined runs record the path).
    music = data.get("music")
    if music:
        tracks.append({"id": "a2", "kind": "audio", "label": "MUSIC", "clips": [{
            "id": "m0", "start": 0.0, "end": float(duration or 0),
            "label": Path(str(music)).name,
            "source": str(music), "source_key": str(music)}]})

    srt = _sidecar_srt(d, video)
    if srt:
        cues = _parse_srt_cues(srt.read_text(encoding="utf-8", errors="replace"))
        tracks.append({
            "id": "cc", "kind": "captions", "label": "CAPTIONS",
            "source_file": _rel_or_abs(srt),
            "clips": [dict(c, id=f"s{i}", label=c["text"]) for i, c in enumerate(cues)],
        })

    can_reassemble = bool(clips) and vo_present
    return {
        "program": _rel_or_abs(vf) if vf.exists() else None,
        "duration": duration,
        "stages": {},
        "tracks": tracks,
        # cut=True routes v1 trims through the standard cut-edit UI path;
        # the render endpoint detects the sidecar and reassembles instead.
        # music/hook are editable on streamlined runs, which re-render by
        # regenerating through streamlined_ad.py (no VO/reassemble needed).
        "editable": {"cut": can_reassemble, "broll": False,
                     "captions": srt is not None, "titles": can_reassemble,
                     "music": is_streamlined and bool(music),
                     "hook": is_streamlined and bool(hook_text)},
        "variant": True,
        "sidecar_video": Path(video).name,
    }


@app.get("/api/timeline/manifest")
async def timeline_manifest(run: str, video: str | None = None):
    d = _run_dir(run)
    sidecar_videos = sorted(f.name[: -len(".timeline.json")] for f in d.glob("*.timeline.json"))
    if video and (d / (Path(video).name + ".timeline.json")).exists():
        out = _manifest_from_sidecar(d, video)
    elif not video and sidecar_videos and not (d / "cut_edl.json").exists() and not (d / "broll_edl.json").exists():
        out = _manifest_from_sidecar(d, sidecar_videos[0])
    else:
        out = _manifest_from_edls(d)
    out["run"] = d.name
    out["sidecar_videos"] = sidecar_videos
    return out


_HELPERS_DIR = PROJECT_ROOT / "video-use" / "helpers"
_HELPERS_PYTHON = _vu_python()


def _helpers_py() -> str:
    return str(_HELPERS_PYTHON) if _HELPERS_PYTHON.exists() else "python"


# ── Local ad-copy generation (Workstream A4) ─────────────────────────
# Thin passthrough to helpers/copy_gen.py (local Ollama, $0/token). The
# copywriting step (hook/bullets/CTA/scripts) offloads here instead of
# spending an Opus turn; Claude Code just orchestrates. Ollama must be up
# (`ollama serve` + the COPY_MODEL pulled).
@app.post("/api/copy_gen")
async def copy_gen(payload: dict):
    fmt = payload.get("format")
    if fmt not in ("bullets", "hook", "script", "vo"):
        raise HTTPException(400, "format must be bullets|hook|script|vo")
    cmd = [_helpers_py(), str(_HELPERS_DIR / "copy_gen.py"), "--format", fmt, "--json"]
    brand = (payload.get("brand") or "").strip()
    if brand:
        bp = heal_drive_letter(brand)
        if not bp.exists():
            raise HTTPException(404, f"brand file not found: {brand}")
        cmd += ["--brand", str(bp)]
    for key, flag in (("angle", "--angle"), ("hook", "--hook"), ("cta", "--cta"),
                      ("model", "--model")):
        val = (payload.get(key) or "").strip()
        if val:
            cmd += [flag, val]
    for key, flag in (("count", "--count"), ("words", "--words"),
                      ("min_bullets", "--min-bullets"), ("max_bullets", "--max-bullets"),
                      ("max_chars", "--max-chars"), ("max_words", "--max-words")):
        if key in payload:
            cmd += [flag, str(int(payload[key]))]
    if "temperature" in payload:
        cmd += ["--temperature", str(float(payload["temperature"]))]

    def work() -> dict:
        import subprocess
        proc = subprocess.run(cmd, capture_output=True, cwd=str(PROJECT_ROOT),
                              timeout=180,
                              env={**os.environ, "PYTHONUTF8": "1"})
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        err = (proc.stderr or b"").decode("utf-8", errors="replace")
        if proc.returncode != 0:
            # exit 3 = Ollama unreachable; surface a clear hint.
            hint = " (is `ollama serve` running + COPY_MODEL pulled?)" if proc.returncode == 3 else ""
            raise HTTPException(502, f"copy_gen failed (exit {proc.returncode}){hint}: {err[-400:]}")
        try:
            return json.loads(out[out.index("{"):out.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(502, f"copy_gen returned no JSON: {out[:300]}")

    return await asyncio.to_thread(work)


def _ff_filter_path(p: Path) -> str:
    """Escape a Windows path for use inside an ffmpeg filter argument."""
    s = str(p).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


def _srt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_ts(t: float) -> str:
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _rebuild_ass(template: Path, cues: list[dict], out_path: Path) -> None:
    """Re-emit an .ass file: keep everything through the [Events] Format line
    (script info + styles untouched), regenerate Dialogue lines from cues.
    Layer/Style/margins and any leading override block ({\\an5\\pos...}) are
    copied from the first original Dialogue so placement/styling survive."""
    lines = template.read_text(encoding="utf-8", errors="replace").splitlines()
    header: list[str] = []
    proto_fields: list[str] | None = None
    proto_override = ""
    in_events = False
    for ln in lines:
        if not in_events:
            header.append(ln)
            if ln.strip().lower().startswith("[events]"):
                in_events = True
        elif ln.strip().lower().startswith("format:"):
            header.append(ln)
        elif ln.strip().lower().startswith("dialogue:") and proto_fields is None:
            # Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
            body = ln.split(":", 1)[1]
            proto_fields = body.split(",", 9)
            text = proto_fields[9] if len(proto_fields) > 9 else ""
            if text.startswith("{"):
                proto_override = text[: text.find("}") + 1]
    if proto_fields is None:
        proto_fields = ["0", "", "", "Default", "", "0", "0", "0", ""]

    def wrap(text: str, max_chars: int = 24) -> str:
        """Balance a long cue onto two lines like step_build_captions does —
        without this, edited full-sentence cues render as one line running
        off the frame edge."""
        if "\n" in text:
            return text.replace("\n", "\\N")
        if len(text) <= max_chars:
            return text
        words = text.split()
        line1, count = [], 0
        for w in words:
            if count + len(w) + (1 if line1 else 0) > len(text) // 2 and line1:
                break
            count += len(w) + (1 if line1 else 0)
            line1.append(w)
        rest = words[len(line1):]
        return " ".join(line1) + ("\\N" + " ".join(rest) if rest else "")

    out_lines = list(header)
    for c in cues:
        text = wrap(str(c.get("text", "")))
        f = list(proto_fields)
        f[1], f[2] = _ass_ts(float(c["start"])), _ass_ts(float(c["end"]))
        out_lines.append("Dialogue:" + ",".join(f[:9]) + "," + proto_override + text)
    out_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _run_step(cmd: list[str], log: list[str], timeout: int = 900) -> None:
    import subprocess
    log.append("$ " + " ".join(cmd))
    proc = subprocess.run(
        cmd, capture_output=True, timeout=timeout, cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONUTF8": "1", "COPYFILE_DISABLE": "1"},
    )
    tail = (proc.stderr or b"").decode("utf-8", errors="replace")[-2000:]
    if proc.returncode != 0:
        log.append(tail)
        raise HTTPException(502, f"step failed ({cmd[0]} → exit {proc.returncode}): {tail[-500:]}")
    log.append(tail[-400:])


@app.post("/api/timeline/render")
async def timeline_render(payload: dict):
    """Deterministically re-render a run from timeline edits.

    Payload: { run, cut_ranges?|null, broll?|null, captions?|null, broll_root? }
    null / absent section = untouched. Sections carry the FULL desired state
    (a deleted clip is simply absent). cut_ranges are in SOURCE time; broll
    segments and caption cues are in output-timeline time.
    Output: <run>/final_edit<N>.mp4 — originals and prior edits are never
    overwritten; first edit backs up the original EDLs as *.orig.json.
    """
    run = payload.get("run") or ""
    d = _run_dir(run)
    cut_ranges = payload.get("cut_ranges")
    broll = payload.get("broll")
    captions = payload.get("captions")
    titles = payload.get("titles")
    # music/hook edits (streamlined runs). `music` may be a path OR an explicit
    # null (= remove the bed), so presence of the key — not its value — signals
    # an edit. `hook` is a string.
    music_edit = "music" in payload
    hook_edit = payload.get("hook")
    if (cut_ranges is None and broll is None and captions is None
            and titles is None and not music_edit and hook_edit is None):
        raise HTTPException(400, "no edits supplied")

    def _backup_once(f: Path) -> None:
        b = f.with_suffix(f.suffix + ".orig")
        if f.exists() and not b.exists():
            shutil.copy2(f, b)

    def _effective_titles() -> list[dict]:
        """Payload wins; otherwise whatever titles.json already holds —
        existing titles must survive a cut/caption re-render."""
        if titles is not None:
            tj = d / "titles.json"
            _backup_once(tj)
            tj.write_text(json.dumps(titles, indent=2), encoding="utf-8")
            return titles
        tj = d / "titles.json"
        if tj.exists():
            try:
                return json.loads(tj.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
        return []

    video = payload.get("video")
    sidecar = (d / (Path(video).name + ".timeline.json")) if video else None

    # ── Streamlined-ad runs: regenerate deterministically via
    # streamlined_ad.py so hook / music / background edits all rebuild in one
    # pass (reassemble.py can't do music beds or the hook/bullet/CTA ASS). ──
    if sidecar is not None and sidecar.exists() and not (d / "cut_edl.json").exists():
        _sdata = json.loads(sidecar.read_text(encoding="utf-8"))
        if str(_sdata.get("format", "")).startswith("streamlined-"):
            stem = Path(video).stem
            n = 1
            while (d / f"{stem}_edit{n}.mp4").exists():
                n += 1
            out = d / f"{stem}_edit{n}.mp4"
            fmt = _sdata.get("format", "streamlined-bullets").removeprefix("streamlined-")
            hook = (hook_edit if hook_edit is not None else _sdata.get("hook")) or ""
            # background: an edited v1 clip source wins, else the sidecar's
            bg = None
            if cut_ranges:
                bg = (cut_ranges[0] or {}).get("source")
            if not bg:
                bclips = _sdata.get("broll_clips") or []
                bg = bclips[0].get("source") if bclips else None
            if not bg:
                raise HTTPException(400, "streamlined run has no background to re-render")
            music = payload.get("music") if music_edit else _sdata.get("music")
            try:
                w, h = (int(x) for x in str(_sdata.get("resolution", "1080x1920")).split("x"))
            except ValueError:
                w, h = 1080, 1920
            cmd = [_helpers_py(), str(_HELPERS_DIR / "streamlined_ad.py"),
                   "--format", fmt,
                   "--background", str(heal_drive_letter(bg)),
                   "--hook", hook,
                   "--cta", _sdata.get("cta") or "",
                   "--duration", str(float(_sdata.get("vo_duration_s") or 15)),
                   "--width", str(w), "--height", str(h),
                   "--output", str(out)]
            for b in (_sdata.get("bullets") or []):
                cmd += ["--bullet", b]
            disclaimer = (payload.get("disclaimer") if "disclaimer" in payload
                          else _sdata.get("disclaimer")) or ""
            if disclaimer.strip():
                cmd += ["--disclaimer", disclaimer.strip()]
            if music:
                cmd += ["--music", str(heal_drive_letter(music))]
            brand = _sdata.get("brand") or {}
            if brand.get("primary"):
                cmd += ["--brand-primary", brand["primary"]]
            if brand.get("accent"):
                cmd += ["--brand-accent", brand["accent"]]
            if brand.get("logo"):
                cmd += ["--logo", str(heal_drive_letter(brand["logo"]))]
            if fmt == "text-vo":
                # a hook change must re-voice; need the voice id (persisted by
                # streamlined_ad.py; fall back to the studio default VO voice)
                cmd += ["--voice-id", str(_sdata.get("voice_id") or "xsLQCPQf2lJnUdFzvAJ2")]

            job_id = payload.get("job_id") or ""
            s_est = 6.0 + float(_sdata.get("vo_duration_s") or 15) + (6.0 if fmt == "text-vo" else 0.0)

            def streamlined_work() -> dict:
                _progress_start(job_id, "Streamlined re-render", s_est, "rendering")
                try:
                    log: list[str] = []
                    _run_step(cmd, log, timeout=600)
                    _progress_done(job_id, output=_rel_or_abs(out))
                    return {"output": _rel_or_abs(out), "log": log[-8:], "version": n}
                except Exception as e:  # noqa: BLE001
                    _progress_done(job_id, error=str(getattr(e, "detail", e)))
                    raise

            async with _render_semaphore():
                return await asyncio.to_thread(streamlined_work)

    # ── Sidecar (concat-assembly) runs: variants + hand-assembled videos.
    # The v1 track IS the sequential clip list, so cut_ranges carry real
    # source paths (source_key round-trip) and re-render = reassemble.
    if sidecar is not None and sidecar.exists() and not (d / "cut_edl.json").exists():
        stem = Path(video).stem
        n = 1
        while (d / f"{stem}_edit{n}.mp4").exists():
            n += 1
        out = d / f"{stem}_edit{n}.mp4"

        def sidecar_work() -> dict:
            log: list[str] = []
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if cut_ranges is not None:
                _backup_once(sidecar)
                data["broll_clips"] = [{
                    "source": str(r.get("source", "")),
                    "in": float(r.get("start", 0)),
                    "out": float(r.get("end", 0)),
                    "note": r.get("note", ""),
                } for r in cut_ranges]
                sidecar.write_text(json.dumps(data, indent=2), encoding="utf-8")

            ass_override: Path | None = None
            if captions is not None:
                srt_path = _sidecar_srt(d, video) or (d / f"{stem}.srt")
                _backup_once(srt_path)
                srt_blocks = [f"{i+1}\n{_srt_ts(float(c['start']))} --> {_srt_ts(float(c['end']))}\n{c.get('text','')}\n"
                              for i, c in enumerate(captions)]
                srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")
                # never use generated edit artifacts (titles_edit*/captions_edit*)
                # as the style template — that once restyled a user's captions
                # as titles
                asses = [a for a in sorted(d.glob("*.ass"), key=lambda f: -f.stat().st_mtime)
                         if not a.name.startswith(("captions_edit", "titles_edit"))]
                if asses:
                    _backup_once(asses[0])
                    template = asses[0].with_suffix(".ass.orig")
                    ass_override = d / f"captions_edit{n}.ass"
                    _rebuild_ass(template if template.exists() else asses[0],
                                 captions, ass_override)

            eff_titles = _effective_titles()
            titles_ass: Path | None = None
            if eff_titles:
                try:
                    w, h = (int(x) for x in str(data.get("resolution", "1080x1920")).split("x"))
                except ValueError:
                    w, h = 1080, 1920
                titles_ass = d / f"titles_edit{n}.ass"
                _write_titles_ass(eff_titles, w, h, titles_ass)

            cmd = [_helpers_py(), str(_HELPERS_DIR / "reassemble.py"),
                   "--sidecar", str(sidecar), "--output", str(out)]
            if ass_override is not None:
                cmd += ["--ass", str(ass_override)]
            if titles_ass is not None:
                cmd += ["--titles-ass", str(titles_ass)]
            _run_step(cmd, log)
            return {"output": _rel_or_abs(out), "log": log[-12:], "version": n}

        async with _render_semaphore():
            return await asyncio.to_thread(sidecar_work)

    # ── EDL runs (standard pipeline) ──
    # Versioned output name — never clobber.
    n = 1
    while (d / f"final_edit{n}.mp4").exists():
        n += 1
    final_out = d / f"final_edit{n}.mp4"

    def work() -> dict:
        log: list[str] = []
        py = _helpers_py()

        # ── 1. base video: re-cut if ranges changed, else reuse pre-broll stage ──
        if cut_ranges is not None:
            edl_path = d / "cut_edl.json"
            if not edl_path.exists():
                raise HTTPException(400, "run has no cut_edl.json — cut is not editable here")
            edl = json.loads(edl_path.read_text(encoding="utf-8"))
            # heal stale drive letters in source paths before ffmpeg sees them
            sources = {k: str(heal_drive_letter(v)) for k, v in edl.get("sources", {}).items()}
            _backup_once(edl_path)
            edl_new = {"sources": sources, "ranges": cut_ranges}
            edl_path.write_text(json.dumps(edl_new, indent=2), encoding="utf-8")
            base = d / f"cut_edit{n}.mp4"
            _run_step([py, str(_HELPERS_DIR / "render.py"), str(edl_path),
                       "-o", str(base), "--no-subtitles"], log)
        else:
            # No re-cut. If b-roll is being re-laid, start from the pre-broll
            # stage; if captions are being re-burned, keep the b-roll composite;
            # if ONLY titles changed, start from the already-captioned final so
            # existing captions survive without a re-burn.
            if broll is not None:
                stage_pref = ("cut_audio", "cut_leveled", "cut")
            elif captions is not None:
                stage_pref = ("cut_broll", "cut_audio", "cut_leveled", "cut")
            else:  # titles-only edit
                stage_pref = ("final", "cut_captioned", "cut_broll",
                              "cut_audio", "cut_leveled", "cut")
            base = next((d / f"{s}.mp4" for s in stage_pref
                         if (d / f"{s}.mp4").exists()), None)
            if base is None:
                raise HTTPException(400, "no base cut video found in run folder")

        # ── 2. b-roll overlay ──
        current = base
        if broll is not None and len(broll) > 0:
            bedl_path = d / "broll_edl.json"
            _backup_once(bedl_path)
            bedl_path.write_text(json.dumps(broll, indent=2), encoding="utf-8")
            root = payload.get("broll_root")
            cmd = [py, str(_HELPERS_DIR / "broll_overlay.py"), str(current),
                   "--edl", str(bedl_path), "--output", str(d / f"broll_edit{n}.mp4")]
            if root:
                cmd += ["--broll-root", str(heal_drive_letter(root))]
            _run_step(cmd, log)
            current = d / f"broll_edit{n}.mp4"

        # ── 3. captions + titles — burn LAST, per house rule ──
        ass_files = [a for a in sorted(d.glob("*.ass"), key=lambda f: -f.stat().st_mtime)
                     if not a.name.startswith(("captions_edit", "titles_edit"))]
        burn_ass: Path | None = None
        if captions is not None:
            srt_path = next(iter(sorted(d.glob("*.srt"), key=lambda f: -f.stat().st_mtime)), d / "master.srt")
            _backup_once(srt_path)
            srt_blocks = [f"{i+1}\n{_srt_ts(float(c['start']))} --> {_srt_ts(float(c['end']))}\n{c.get('text','')}\n"
                          for i, c in enumerate(captions)]
            srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")
            if ass_files:
                _backup_once(ass_files[0])
                burn_ass = d / f"captions_edit{n}.ass"
                _rebuild_ass(ass_files[0].with_suffix(".ass.orig") if ass_files[0].with_suffix(".ass.orig").exists() else ass_files[0],
                             captions, burn_ass)
        elif ass_files and (cut_ranges is not None or broll is not None):
            # video changed under unchanged captions → re-burn existing ass
            burn_ass = ass_files[0]

        # titles ride on top of everything. On a titles-only edit `current`
        # is the captioned final, so titles alone get burned; on video/caption
        # edits both filters chain into one encode.
        eff_titles = _effective_titles()
        titles_ass: Path | None = None
        if eff_titles:
            w, h = _probe_dims(current)
            titles_ass = d / f"titles_edit{n}.ass"
            _write_titles_ass(eff_titles, w, h, titles_ass)

        filters = [f"ass='{_ff_filter_path(a)}'"
                   for a in (burn_ass, titles_ass) if a is not None]
        if filters:
            _run_step(["ffmpeg", "-y", "-i", str(current),
                       "-vf", ",".join(filters),
                       "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19",
                       "-c:a", "copy", str(final_out)], log)
        else:
            shutil.copy2(current, final_out)

        return {"output": _rel_or_abs(final_out), "log": log[-12:], "version": n}

    async with _render_semaphore():
        return await asyncio.to_thread(work)


# ─────────────────────────────────────────────────────────────────────
# Streamlined ads — deterministic hook/bullets/CTA text-over-video ads.
# Thin passthrough to helpers/streamlined_ad.py; no LLM involved.
# ─────────────────────────────────────────────────────────────────────

_ASPECT_DIMS = {"9x16": (1080, 1920), "1x1": (1080, 1080), "16x9": (1920, 1080)}


@app.post("/api/streamlined_ad")
async def streamlined_ad(payload: dict):
    import re
    fmt = payload.get("format")
    if fmt not in ("bullets", "text-vo"):
        raise HTTPException(400, "format must be 'bullets' or 'text-vo'")
    hook = (payload.get("hook") or "").strip()
    if not hook:
        raise HTTPException(400, "hook is required")
    background = payload.get("background")
    if not background:
        raise HTTPException(400, "background (file or folder) is required")
    bullets = [b.strip() for b in (payload.get("bullets") or []) if b.strip()]
    if fmt == "bullets" and not bullets:
        raise HTTPException(400, "bullets format needs at least one bullet")
    voice_id = payload.get("voice_id")
    if fmt == "text-vo" and not voice_id:
        raise HTTPException(400, "text-vo format needs voice_id")

    w, h = _ASPECT_DIMS.get(payload.get("aspect") or "9x16", (1080, 1920))
    from datetime import datetime as _dt
    run_name = "streamlined_" + _dt.now().strftime("%Y-%m-%d-%H-%M-%S")
    out = EDIT_DIR / run_name / "streamlined.mp4"

    cmd = [_helpers_py(), str(_HELPERS_DIR / "streamlined_ad.py"),
           "--format", fmt,
           "--background", str(heal_drive_letter(background)),
           "--hook", hook,
           "--cta", payload.get("cta") or "",
           "--duration", str(float(payload.get("duration") or 15)),
           "--width", str(w), "--height", str(h),
           "--output", str(out)]
    for b in bullets:
        cmd += ["--bullet", b]
    if (payload.get("disclaimer") or "").strip():
        cmd += ["--disclaimer", payload["disclaimer"].strip()]
    if payload.get("music"):
        cmd += ["--music", str(heal_drive_letter(payload["music"]))]
    if voice_id:
        cmd += ["--voice-id", str(voice_id)]
    if payload.get("logo"):
        cmd += ["--logo", str(heal_drive_letter(payload["logo"]))]
    for key, flag in (("brand_primary", "--brand-primary"),
                      ("brand_accent", "--brand-accent"),
                      ("cta_bg", "--cta-bg"), ("cta_fg", "--cta-fg")):
        val = (payload.get(key) or "").strip()
        if val:
            if not re.fullmatch(r"#?[0-9a-fA-F]{6}", val):
                raise HTTPException(400, f"{key} must be #RRGGBB")
            cmd += [flag, val]
    if payload.get("num_broll"):
        try:
            n = max(1, int(payload["num_broll"]))
        except (ValueError, TypeError):
            n = 1
        if n > 1:
            cmd += ["--num-broll", str(n)]
    if payload.get("seed") is not None:
        cmd += ["--seed", str(int(payload["seed"]))]

    job_id = payload.get("job_id") or ""
    dur = float(payload.get("duration") or 15)
    est = 6.0 + dur + (6.0 if fmt == "text-vo" else 0.0)

    def work() -> dict:
        _progress_start(job_id, f"Streamlined {fmt} ad", est, "rendering")
        try:
            log: list[str] = []
            _run_step(cmd, log, timeout=600)
            # Deliverable copy → Final Output/<hook>/ (folder named for the ad's
            # hook, per Sean). The edit run stays under videos/edit/ so the
            # timeline dock can still open and re-render it.
            safe_hook = re.sub(r"[^\w\s-]", "", hook).strip().rstrip(".:")[:60].strip() or run_name
            final_dir = output_root() / safe_hook
            final_dir.mkdir(parents=True, exist_ok=True)
            final_path = final_dir / f"ad_{run_name.removeprefix('streamlined_')}.mp4"
            shutil.copy2(out, final_path)
            _progress_done(job_id, output=_rel_or_abs(final_path))
            return {"output": _rel_or_abs(final_path),
                    "edit_run_output": _rel_or_abs(out),
                    "run": run_name, "log": log[-6:]}
        except Exception as e:  # noqa: BLE001
            _progress_done(job_id, error=str(getattr(e, "detail", e)))
            raise

    async with _render_semaphore():
        return await asyncio.to_thread(work)


# ─────────────────────────────────────────────────────────────────────
# Brand presets — per-team logo + colors for streamlined ads.
#
# Convention: `<team folder>/Guidelines/brand.json` holds
# {"primary": "#RRGGBB", "accent": "#RRGGBB", "logo": "<abs path>"}.
# Logos are discovered from image files under the team's Guidelines/ and
# Logos/ folders. The team folder itself is resolved client-side
# (resolveTeamFolder in app.js) — this endpoint just takes the path.
# ─────────────────────────────────────────────────────────────────────

_BRAND_DIR_NAMES = ("guidelines", "brand guidelines", "logos", "logo")
_LOGO_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _brand_dirs(team_folder: Path) -> list[Path]:
    if not team_folder.is_dir():
        return []
    return [d for d in team_folder.iterdir()
            if d.is_dir() and d.name.lower() in _BRAND_DIR_NAMES]


def _brand_json_path(team_folder: Path) -> Path:
    for d in _brand_dirs(team_folder):
        if d.name.lower() in ("guidelines", "brand guidelines"):
            return d / "brand.json"
    return team_folder / "Guidelines" / "brand.json"


@app.get("/api/brand")
async def brand_get(folder: str):
    team_folder = heal_drive_letter((folder or "").strip().strip('"'))
    if not team_folder.is_dir():
        raise HTTPException(404, f"team folder not found: {team_folder}")

    brand = None
    bj = _brand_json_path(team_folder)
    if bj.exists():
        try:
            brand = json.loads(bj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            brand = None

    logos: list[str] = []
    for d in _brand_dirs(team_folder):
        for f in sorted(d.rglob("*")):
            if (f.is_file() and f.suffix.lower() in _LOGO_EXTS
                    and not f.name.startswith("._")):
                logos.append(str(f.resolve()))
    return {"folder": str(team_folder.resolve()), "brand": brand, "logos": logos}


@app.post("/api/brand")
async def brand_save(payload: dict):
    team_folder = heal_drive_letter((payload.get("folder") or "").strip().strip('"'))
    if not team_folder.is_dir():
        raise HTTPException(404, f"team folder not found: {team_folder}")
    brand: dict = {}
    for key in ("primary", "accent"):
        val = (payload.get(key) or "").strip()
        if val:
            if not re.fullmatch(r"#?[0-9a-fA-F]{6}", val):
                raise HTTPException(400, f"{key} must be #RRGGBB")
            brand[key] = val if val.startswith("#") else "#" + val
    logo = (payload.get("logo") or "").strip()
    if logo:
        lp = heal_drive_letter(logo)
        if not lp.is_file():
            raise HTTPException(404, f"logo not found: {lp}")
        brand["logo"] = str(lp.resolve())
    bj = _brand_json_path(team_folder)
    bj.parent.mkdir(parents=True, exist_ok=True)
    bj.write_text(json.dumps(brand, indent=2), encoding="utf-8")
    return {"saved": str(bj), "brand": brand}


# ─────────────────────────────────────────────────────────────────────
# Gen AI — Veo (video) + Nano Banana (images) via the Gemini API.
#
# Thin passthroughs to helpers/veo_video.py and helpers/nano_banana.py.
# Each POST generates ONE artifact so a request can't outlive a client
# timeout; the UI loops for a batch. Veo's >1-clip cost gate is enforced
# in the UI (explicit estimate + confirm) per the CLAUDE.md rule.
# Outputs land in videos/edit/genai/<run>/ next to their .gen.json.
# ─────────────────────────────────────────────────────────────────────

_GENAI_DIR = EDIT_DIR / "genai"
# rough per-second USD estimate by Veo model tier — used only to quote the
# cost gate; real billing is Google's. ~$1.20 per 8s fast clip → $0.15/s.
_VEO_USD_PER_SEC = {
    "veo-3.1-fast-generate-preview": 0.15,
    "veo-3.1-generate-preview": 0.40,
    "veo-3.1-lite-generate-preview": 0.10,
}


def _sanitize_run(name: str, prefix: str) -> str:
    safe = re.sub(r"[^\w-]", "_", (name or "").strip())[:60]
    return safe if safe.startswith(prefix) else f"{prefix}_{safe}" if safe else \
        prefix + "_" + _dt_now_stamp()


def _dt_now_stamp() -> str:
    from datetime import datetime as _dt
    return _dt.now().strftime("%Y-%m-%d-%H-%M-%S")


def _run_genai(cmd: list[str], timeout: int = 600) -> tuple[dict, str]:
    """Run a genai helper, return (parsed --json stdout, stderr tail)."""
    import subprocess
    proc = subprocess.run(
        cmd, capture_output=True, cwd=str(PROJECT_ROOT), timeout=timeout,
        env={**os.environ, "PYTHONUTF8": "1", "COPYFILE_DISABLE": "1"})
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise HTTPException(502, f"genai failed (exit {proc.returncode}): {err[-600:]}")
    data: dict = {}
    if out:
        # helper prints the JSON object last; find the final {...} block
        try:
            start = out.rindex("{")
            data = json.loads(out[start:])
        except (ValueError, json.JSONDecodeError):
            data = {}
    return data, err[-400:]


@app.get("/api/genai/models")
async def genai_models():
    """List the video + image model ids the API currently exposes."""
    def work() -> dict:
        out: dict = {"veo": [], "image": [], "error": None}
        import subprocess
        for key, helper, flt in (("veo", "veo_video.py", "veo"),
                                  ("image", "nano_banana.py", None)):
            try:
                proc = subprocess.run(
                    [_helpers_py(), str(_HELPERS_DIR / helper), "--list-models"],
                    capture_output=True, cwd=str(PROJECT_ROOT), timeout=60,
                    env={**os.environ, "PYTHONUTF8": "1"})
                for ln in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
                    ln = ln.strip()
                    if ln:
                        out[key].append(ln.split()[0])
            except Exception as e:  # noqa: BLE001
                out["error"] = str(e)
        return out
    return await asyncio.to_thread(work)


@app.post("/api/genai/veo")
async def genai_veo(payload: dict):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    model = payload.get("model") or "veo-3.1-fast-generate-preview"
    aspect = payload.get("aspect") or "9:16"
    resolution = (payload.get("resolution") or "1080p").strip()
    negative = (payload.get("negative") or "").strip()
    image = (payload.get("image") or "").strip()
    refs = [r.strip() for r in (payload.get("reference_images") or []) if r.strip()][:3]
    duration = payload.get("duration")
    index = int(payload.get("index") or 0)
    run = _sanitize_run(payload.get("run") or "", "veo")
    out_dir = _GENAI_DIR / run
    out = out_dir / f"clip_{index}.mp4"
    job_id = payload.get("job_id") or ""
    # rough wall-time estimate by tier, scaled by clip length
    base = 75.0 if "fast" in model else 60.0 if "lite" in model else 115.0
    est = base * max(1.0, (int(duration) if duration else 8) / 8.0)

    def work() -> dict:
        _progress_start(job_id, f"Veo clip {index + 1}", est, "generating")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd = [_helpers_py(), str(_HELPERS_DIR / "veo_video.py"),
                   "--prompt", prompt, "--output", str(out),
                   "--aspect", aspect, "--model", model, "--json"]
            if resolution:
                cmd += ["--resolution", resolution]
            if negative:
                cmd += ["--negative", negative]
            if duration:
                cmd += ["--duration-seconds", str(int(duration))]
            if image:
                img = heal_drive_letter(image)
                if not img.exists():
                    raise HTTPException(404, f"first-frame image not found: {image}")
                cmd += ["--image", str(img)]
            for r in refs:
                rp = heal_drive_letter(r)
                if not rp.exists():
                    raise HTTPException(404, f"reference image not found: {r}")
                cmd += ["--reference-image", str(rp)]
            data, err = _run_genai(cmd, timeout=600)
            _progress_done(job_id, output=_rel_or_abs(out))
            return {"run": run, "kind": "veo", "index": index,
                    "output": _rel_or_abs(out), "gen": data, "log": err}
        except Exception as e:  # noqa: BLE001
            _progress_done(job_id, error=str(getattr(e, "detail", e)))
            raise

    return await asyncio.to_thread(work)


@app.post("/api/genai/nano")
async def genai_nano(payload: dict):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    model = payload.get("model") or "gemini-3.1-flash-image"
    aspect = (payload.get("aspect") or "").strip()
    size = (payload.get("size") or "").strip()
    images = [i.strip() for i in (payload.get("images") or []) if i.strip()][:14]
    index = int(payload.get("index") or 0)
    run = _sanitize_run(payload.get("run") or "", "nano")
    out_dir = _GENAI_DIR / run
    out = out_dir / f"img_{index}.png"
    job_id = payload.get("job_id") or ""
    est = 25.0 if "pro" in model else 12.0

    def work() -> dict:
        _progress_start(job_id, f"Nano Banana image {index + 1}", est, "generating")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd = [_helpers_py(), str(_HELPERS_DIR / "nano_banana.py"),
                   "--prompt", prompt, "--output", str(out),
                   "--model", model, "--json"]
            if aspect:
                cmd += ["--aspect", aspect]
            if size:
                cmd += ["--size", size]
            for im in images:
                ip = heal_drive_letter(im)
                if not ip.exists():
                    raise HTTPException(404, f"input image not found: {im}")
                cmd += ["--image", str(ip)]
            data, err = _run_genai(cmd, timeout=300)
            # nano may save .jpg for a .png request — trust gen.outputs
            saved = data.get("outputs") or [str(out)]
            _progress_done(job_id, output=_rel_or_abs(Path(saved[0])))
            return {"run": run, "kind": "nano", "index": index,
                    "outputs": [_rel_or_abs(Path(s)) for s in saved],
                    "gen": data, "log": err}
        except Exception as e:  # noqa: BLE001
            _progress_done(job_id, error=str(getattr(e, "detail", e)))
            raise

    return await asyncio.to_thread(work)


# ─────────────────────────────────────────────────────────────────────
# Manager — deterministic quality/cost review over the studio's logs.
#
# Reads videos/edit/{sync_log.jsonl, level_log.jsonl, jobs.jsonl,
# usage.log} and turns them into findings + recommendations. No LLM —
# the "deep review" button in the UI wraps this JSON into a Kino prompt.
# ─────────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path, limit: int = 2000) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _classify_job(job: dict) -> str:
    # operations entries are dicts like {"op": "ffmpeg_other", "tool": ...}
    # in newer logs, bare strings in older ones — accept both.
    ops = job.get("operations") or []
    names = {o.get("op") if isinstance(o, dict) else str(o) for o in ops}
    names.discard(None)
    if names:
        return "+".join(sorted(names)[:3])
    head = (job.get("prompt") or "")[:200].upper()
    for key, name in (
        ("VARIANT FACTORY", "variants"), ("ROLL THE DICE", "dice"),
        ("DICE", "dice"), ("STITCH", "stitch"), ("SYNC", "sync"),
        ("SPLIT", "split-hooks"), ("HEYGEN", "avatar"), ("AVATAR", "avatar"),
        ("PIPELINE", "pipeline"), ("CAPTION", "captions"), ("HOOK", "hook"),
    ):
        if key in head:
            return name
    return "chat"


@app.get("/api/manager/review")
async def manager_review():
    findings: list[dict] = []   # {severity: info|warn|error, area, msg, detail?}
    stats: dict = {}

    def add(severity: str, area: str, msg: str, detail: str = "") -> None:
        findings.append({"severity": severity, "area": area, "msg": msg, "detail": detail})

    # ── SYNC quality (sync_log.jsonl) ──
    syncs = _read_jsonl(EDIT_DIR / "sync_log.jsonl")
    # same output re-synced across sessions (and re-logged under a different
    # drive letter after the D:→E: move) → keep only the newest entry per file
    syncs = list({Path(str(s.get("output", i))).name.lower(): s
                  for i, s in enumerate(syncs)}.values())
    if syncs:
        out_of_window = [s for s in syncs if s.get("output_in_target_window") is False]
        low_pearson = [s for s in syncs
                       if s.get("match_pearson") is not None and float(s["match_pearson"]) < 0.45]
        big_offsets = [s for s in syncs
                       if abs(float(s.get("detected_offset_seconds", 0) or 0)) > 120]
        stats["sync"] = {
            "total": len(syncs),
            "out_of_audio_window": len(out_of_window),
            "ambiguous_pearson": len(low_pearson),
            "last": syncs[-1].get("timestamp"),
        }
        for s in out_of_window[-5:]:
            add("warn", "sync",
                f"audio peak {s.get('output_audio_peak_dbfs')} dBFS outside target window "
                f"[{s.get('target_peak_min_dbfs')}, {s.get('target_peak_max_dbfs')}]",
                Path(str(s.get("output", ""))).name)
        for s in low_pearson[-5:]:
            add("warn", "sync",
                f"ambiguous match — Pearson {float(s['match_pearson']):.2f} < 0.45; "
                "spot-check this pairing by ear",
                Path(str(s.get("output", ""))).name)
        for s in big_offsets[-3:]:
            add("info", "sync",
                f"offset {float(s.get('detected_offset_seconds', 0)):.1f}s is very large — "
                "device clocks likely far apart (DJI clock fix still pending)",
                Path(str(s.get("output", ""))).name)
    else:
        add("info", "sync", "no sync log entries yet")

    # ── Audio leveling (level_log.jsonl) ──
    levels = _read_jsonl(EDIT_DIR / "level_log.jsonl")
    if levels:
        off = [l for l in levels if l.get("in_target_window") is False]
        hot_gain = [l for l in levels if abs(float(l.get("gain_applied_db", 0) or 0)) > 10]
        stats["leveling"] = {"total": len(levels), "out_of_window": len(off),
                             "gain_over_10db": len(hot_gain)}
        for l in off[-5:]:
            add("warn", "leveling",
                f"output peak {l.get('output_peak_dbfs')} dBFS missed the target window",
                Path(str(l.get("output", ""))).name)
        for l in hot_gain[-3:]:
            add("info", "leveling",
                f"{float(l['gain_applied_db']):+.1f} dB gain applied — source was unusually "
                "quiet/hot, worth an ear-check",
                Path(str(l.get("output", ""))).name)

    # ── Cost + throughput (jobs.jsonl + usage.log) ──
    jobs = _read_jsonl(EDIT_DIR / "jobs.jsonl")
    if jobs:
        done = [j for j in jobs if j.get("completed_at")]
        by_op: dict[str, dict] = {}
        for j in done:
            op = _classify_job(j)
            b = by_op.setdefault(op, {"jobs": 0, "cost": 0.0, "wall_ms": 0, "turns": 0})
            b["jobs"] += 1
            b["cost"] += float(j.get("cost_usd", 0) or 0)
            b["wall_ms"] += int(j.get("wall_clock_ms", 0) or 0)
            b["turns"] += int(j.get("turns", 0) or 0)
        for op, b in by_op.items():
            b["avg_cost"] = round(b["cost"] / b["jobs"], 4)
            b["avg_wall_s"] = round(b["wall_ms"] / b["jobs"] / 1000, 1)
            b["cost"] = round(b["cost"], 2)
            del b["wall_ms"]
        total_cost = round(sum(float(j.get("cost_usd", 0) or 0) for j in done), 2)
        expensive = sorted(done, key=lambda j: -float(j.get("cost_usd", 0) or 0))[:5]
        stats["cost"] = {
            "jobs": len(done),
            "total_usd": total_cost,
            "by_operation": by_op,
            "top_jobs": [{
                "op": _classify_job(j), "cost_usd": round(float(j.get("cost_usd", 0) or 0), 2),
                "wall_s": round(int(j.get("wall_clock_ms", 0) or 0) / 1000),
                "model": j.get("model", "?"), "started_at": j.get("started_at"),
            } for j in expensive],
        }
        zero_usage = [j for j in done
                      if float(j.get("cost_usd", 0) or 0) == 0
                      and int(j.get("wall_clock_ms", 0) or 0) > 60_000]
        if zero_usage:
            add("warn", "cost",
                f"{len(zero_usage)} job(s) ran >60s but recorded $0 / 0 turns — usage "
                "reporting dropped (client closed early or stream aborted)")
        opus_heavy = [op for op, b in by_op.items()
                      if b["avg_cost"] > 1.5 and op in ("variants", "dice")]
        for op in opus_heavy:
            add("info", "cost",
                f"'{op}' averages ${by_op[op]['avg_cost']}/job on wall {by_op[op]['avg_wall_s']}s — "
                "drafting scripts on Sonnet and reserving Opus for review would cut this substantially")

    turns = _read_jsonl(EDIT_DIR / "usage.log")
    if turns:
        errs = [t for t in turns if t.get("is_error")]
        stats["turns"] = {"total": len(turns), "errors": len(errs)}
        if errs:
            add("warn", "pipeline", f"{len(errs)} turn(s) ended in an error state",
                "; ".join((t.get("prompt") or "")[:60] for t in errs[-3:]))

    # ── overall ──
    sev_rank = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: sev_rank.get(f["severity"], 3))
    return {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "stats": stats,
        "findings": findings,
        "alert_count": sum(1 for f in findings if f["severity"] in ("warn", "error")),
    }


# ─────────────────────────────────────────────────────────────────────
# HeyGen — proxy endpoints so the browser can list avatars/voices
# without ever seeing the API key. The key lives in video-use/.env;
# we shell out to the helper which already knows how to load it.
# ─────────────────────────────────────────────────────────────────────

_HEYGEN_HELPER = PROJECT_ROOT / "video-use" / "helpers" / "heygen_video.py"
_HEYGEN_PYTHON = _vu_python()
# Cache the JSON listing on disk so repeat tab-opens don't re-hit HeyGen.
_HEYGEN_CACHE_DIR = STATIC_DIR  # JSON files served as plain static assets, too


def _run_heygen_helper_to_file(extra_args: list[str], output_path: Path,
                               timeout: int = 60) -> dict:
    """Run heygen_video.py with --output set to a path and read the resulting JSON.

    Writing to disk (rather than capturing stdout) sidesteps Windows console
    cp1252 decoding issues — HeyGen returns ~3.5 MB of UTF-8 JSON for the
    avatar list, and `subprocess.run(text=True)` would corrupt non-ASCII
    avatar names on Windows even with PYTHONUTF8=1 set on the child.
    """
    import subprocess

    if not _HEYGEN_HELPER.exists():
        raise HTTPException(status_code=500, detail=f"heygen_video.py not found at {_HEYGEN_HELPER}")
    py = str(_HEYGEN_PYTHON) if _HEYGEN_PYTHON.exists() else "python"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [py, str(_HEYGEN_HELPER), *extra_args, "--output", str(output_path)],
        capture_output=True, timeout=timeout,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    if proc.returncode != 0:
        # Decode stderr defensively — fall back to latin-1 if UTF-8 fails.
        stderr_bytes = proc.stderr or b""
        try:
            msg = stderr_bytes.decode("utf-8").strip()
        except UnicodeDecodeError:
            msg = stderr_bytes.decode("latin-1", errors="replace").strip()
        msg = msg or f"exit {proc.returncode}"
        raise HTTPException(status_code=502, detail=f"HeyGen helper failed: {msg}")

    if not output_path.exists():
        raise HTTPException(status_code=502, detail=f"HeyGen helper succeeded but didn't write {output_path}")
    try:
        return json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Cached HeyGen JSON is malformed: {e}")


@app.get("/api/heygen/avatars")
async def heygen_avatars(refresh: bool = False):
    """Return HeyGen avatars (private + accessible public).

    Caches to studio/static/heygen_avatars.json. Pass ?refresh=true to force
    a re-fetch from the API.
    """
    cache = _HEYGEN_CACHE_DIR / "heygen_avatars.json"
    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass  # cache corrupt — re-fetch

    return _run_heygen_helper_to_file(["--list-avatars"], cache, timeout=60)


@app.get("/api/heygen/voices")
async def heygen_voices(refresh: bool = False):
    """Return HeyGen voices. Cached to studio/static/heygen_voices.json."""
    cache = _HEYGEN_CACHE_DIR / "heygen_voices.json"
    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    return _run_heygen_helper_to_file(["--list-voices"], cache, timeout=60)


@app.get("/api/heygen/status")
async def heygen_status():
    """Quick check that the API key is wired up. Does NOT call HeyGen."""
    env_path = PROJECT_ROOT / "video-use" / ".env"
    has_key = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("HEYGEN_API_KEY") and "=" in line:
                has_key = bool(line.split("=", 1)[1].strip().strip('"').strip("'"))
                break
    if not has_key:
        has_key = bool(os.environ.get("HEYGEN_API_KEY"))
    return {"key_configured": has_key, "helper_exists": _HEYGEN_HELPER.exists()}


_STITCH_HELPER = PROJECT_ROOT / "video-use" / "helpers" / "stitch_script.py"
_HOOK_HELPER = PROJECT_ROOT / "video-use" / "helpers" / "hook_overlay.py"
_VARIANT_HELPER = PROJECT_ROOT / "video-use" / "helpers" / "variant_factory.py"
_PYTHON_BIN = _vu_python()


@app.post("/api/stitch_script")
async def stitch_script(payload: dict):
    """Run video-use/helpers/stitch_script.py on a list of videos + a script.

    Body: {videos: [path,...], script: path, output?: path}
    Paths are project-root-relative ("videos/foo.mp4") or absolute.
    Returns the helper's JSON result on success.
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected JSON object")
    videos = payload.get("videos") or []
    script = payload.get("script") or ""
    output = payload.get("output") or ""
    if not isinstance(videos, list) or len(videos) < 2:
        raise HTTPException(400, "need at least 2 videos to stitch")
    if not script:
        raise HTTPException(400, "missing script path")
    if not _STITCH_HELPER.exists():
        raise HTTPException(500, f"helper missing: {_STITCH_HELPER}")
    if not _PYTHON_BIN.exists():
        raise HTTPException(500, f"venv python missing: {_PYTHON_BIN}")

    def _resolve(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (PROJECT_ROOT / p)

    video_paths = [_resolve(v) for v in videos]
    for v in video_paths:
        if not v.exists():
            raise HTTPException(404, f"video not found: {v}")
    script_path = _resolve(script)
    if not script_path.exists():
        raise HTTPException(404, f"script not found: {script_path}")

    cmd = [
        str(_PYTHON_BIN), str(_STITCH_HELPER),
        "--script", str(script_path),
        "--videos", *[str(v) for v in video_paths],
        "--json",
    ]
    if output:
        cmd += ["--output", str(_resolve(output))]

    # Sync subprocess. Cached transcripts make this fast; uncached transcription
    # is ~20s/video against Scribe. We stream-buffer stdout and parse the final
    # JSON block printed by the helper.
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise HTTPException(500, f"helper failed (exit {proc.returncode}):\n{stderr or stdout}")

    # The helper prints a human report then a JSON block (--json). Find the
    # last {...} payload in stdout.
    json_result: dict | None = None
    depth = 0; start = -1
    for i, ch in enumerate(stdout):
        if ch == "{":
            if depth == 0: start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = stdout[start:i + 1]
                try:
                    json_result = json.loads(candidate)
                except json.JSONDecodeError:
                    pass
                start = -1
    if json_result is None:
        raise HTTPException(500, f"could not parse helper JSON output:\n{stdout[-2000:]}")
    json_result["stdout_tail"] = stdout[-4000:]
    return json_result


@app.post("/api/hook_overlay")
async def hook_overlay(payload: dict):
    """Burn one or more on-screen hooks onto a video.

    Body: {
        video: path (project-root-relative or absolute),
        texts: [str, ...]            # 1 = single output, ≥2 = batch
        position?: "center" | "upper-third"   (default upper-third)
        duration?: number | "full"            (default 3)
        font_family?: str
        font_size?: int                       (default 96)
        text_color?: "#RRGGBB"                (default white)
        bg_color?: "#RRGGBB"                  (default black)
        bg_alpha?: int 0-255                  (default 160)
        rounded?: bool                        (default true)
        shadow?: bool                         (default false)
        output?: path                         (single mode only)
        output_prefix?: path                  (batch mode only; default = videos/edit/hook_<stamp>)
    }
    Returns the helper's JSON {variants: [...], count: N}.
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected JSON object")
    video = payload.get("video") or ""
    texts = payload.get("texts") or []
    if not video:
        raise HTTPException(400, "missing video path")
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) and t.strip() for t in texts):
        raise HTTPException(400, "texts must be a non-empty list of non-empty strings")
    if not _HOOK_HELPER.exists():
        raise HTTPException(500, f"helper missing: {_HOOK_HELPER}")
    if not _PYTHON_BIN.exists():
        raise HTTPException(500, f"venv python missing: {_PYTHON_BIN}")

    def _resolve(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (PROJECT_ROOT / p)

    video_path = _resolve(video)
    if not video_path.exists():
        raise HTTPException(404, f"video not found: {video_path}")

    batch = len(texts) > 1
    cmd = [str(_PYTHON_BIN), str(_HOOK_HELPER), str(video_path), "--json"]
    for t in texts:
        cmd += ["--text", t]
    if batch:
        prefix = payload.get("output_prefix")
        if prefix:
            prefix_path = _resolve(prefix)
        else:
            from datetime import datetime as _dt
            stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            prefix_path = EDIT_DIR / f"hook_{stamp}"
        prefix_path.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["--output-prefix", str(prefix_path)]
    else:
        out = payload.get("output")
        if out:
            out_path = _resolve(out)
        else:
            from datetime import datetime as _dt
            stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            out_path = EDIT_DIR / f"hook_{stamp}.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["--output", str(out_path)]

    # Style/position passthrough — only forward keys when the client set them.
    if "position" in payload:    cmd += ["--position", str(payload["position"])]
    if "duration" in payload:    cmd += ["--duration", str(payload["duration"])]
    if payload.get("font_family"): cmd += ["--font-family", str(payload["font_family"])]
    if "font_size" in payload:   cmd += ["--font-size", str(int(payload["font_size"]))]
    if "text_color" in payload:  cmd += ["--text-color", str(payload["text_color"])]
    if "bg_color" in payload:    cmd += ["--bg-color", str(payload["bg_color"])]
    if "bg_alpha" in payload:    cmd += ["--bg-alpha", str(int(payload["bg_alpha"]))]
    if payload.get("rounded") is False: cmd += ["--no-rounded"]
    if payload.get("shadow"):    cmd += ["--shadow"]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise HTTPException(500, f"hook_overlay failed (exit {proc.returncode}):\n{stderr or stdout}")

    # Same trailing-JSON-block parser used by /api/stitch_script.
    json_result: dict | None = None
    depth = 0; start = -1
    for i, ch in enumerate(stdout):
        if ch == "{":
            if depth == 0: start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = stdout[start:i + 1]
                try:
                    json_result = json.loads(candidate)
                except json.JSONDecodeError:
                    pass
                start = -1
    if json_result is None:
        raise HTTPException(500, f"could not parse helper JSON output:\n{stdout[-2000:]}")
    json_result["stdout_tail"] = stdout[-4000:]
    return json_result


@app.post("/api/broll_proxies")
async def broll_proxies(payload: dict):
    """Pre-build cached 1080p proxies for a b-roll folder (downscales heavy 4K
    clips ONCE, sequentially). Run this before a batch so the variant renders
    work off light proxies and don't overload the GPU. Body: {folder: path}.
    Heals stale drive letters; idempotent. Returns the helper's summary JSON.

    Runs under the SAME render semaphore so it can't overlap with active renders
    (both are heavy ffmpeg work). The helper itself proxies one clip at a time."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected JSON object")
    folder = payload.get("folder") or payload.get("broll_folder") or ""
    if not folder:
        raise HTTPException(400, "missing folder")
    folder_path = heal_drive_letter(Path(folder) if Path(folder).is_absolute()
                                    else (PROJECT_ROOT / folder))
    if not folder_path.exists() or not folder_path.is_dir():
        raise HTTPException(404, f"folder not found: {folder_path}")
    if not _VARIANT_HELPER.exists() or not _PYTHON_BIN.exists():
        raise HTTPException(500, "variant_factory helper or venv python missing")

    cmd = [str(_PYTHON_BIN), str(_VARIANT_HELPER),
           "--broll-folder", str(folder_path), "--build-proxies", "--json"]
    # Serialize proxy builds (lock) AND keep them off concurrent renders (sem).
    async with _proxy_build_lock():
        async with _render_semaphore():
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
            )
            stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise HTTPException(500, f"proxy build failed (exit {proc.returncode}):\n{stderr[-2000:]}")
    # Parse the trailing JSON summary the helper prints.
    try:
        s = stdout.rfind("{")
        return json.loads(stdout[s:]) if s >= 0 else {"ok": True, "raw": stdout[-500:]}
    except Exception:
        return {"ok": True, "raw": stdout[-500:]}


@app.post("/api/variant_factory")
async def variant_factory(payload: dict):
    """Build ONE VO + B-roll variant. Body:
        {
          broll_folder: path,
          script_text:  str,
          voice_id:     str,
          output:       path,            # optional; defaults under videos/edit/variants_<ts>/
          width?: int, height?: int, fps?: str,
          seed?:  int,
          caption_font?: str, caption_size?: int,
          caption_bg?: "#RRGGBB", caption_fg?: "#RRGGBB",
          caption_max_chars?: int, caption_min_duration?: float,
          caption_tail_pad?: float,
          caption_alignment?: int, caption_margin_v?: int,
          tts_model?, tts_stability?, tts_similarity?, tts_style?,
          no_speaker_boost?: bool,
          keep_temps?: bool
        }
    Returns the helper's JSON result.

    The orchestrator (Dice modal "VO + B-roll only" mode) is expected to
    call this endpoint N times in parallel — once per rewritten script —
    after Claude generates the N variants in one Claude pass.
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected JSON object")
    broll = payload.get("broll_folder") or ""
    script = payload.get("script_text") or ""
    voice = payload.get("voice_id") or ""
    if not broll:
        raise HTTPException(400, "missing broll_folder")
    if not script or not script.strip():
        raise HTTPException(400, "missing script_text")
    if not voice:
        raise HTTPException(400, "missing voice_id")
    if not _VARIANT_HELPER.exists():
        raise HTTPException(500, f"helper missing: {_VARIANT_HELPER}")
    if not _PYTHON_BIN.exists():
        raise HTTPException(500, f"venv python missing: {_PYTHON_BIN}")

    def _resolve(p: str) -> Path:
        pp = Path(p)
        if pp.is_absolute():
            return heal_drive_letter(pp)   # remap stale SSD drive letter if needed
        return PROJECT_ROOT / p

    broll_path = _resolve(broll)
    if not broll_path.exists() or not broll_path.is_dir():
        raise HTTPException(
            404,
            f"broll_folder not found: {broll_path}. If this is a stale drive "
            f"letter from a previous SSD mount, re-pick the b-roll folder in the "
            f"Variants modal (or asset root in the Dice config).",
        )

    out = payload.get("output")
    if out:
        out_path = _resolve(out)
    else:
        from datetime import datetime as _dt
        stamp = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
        out_path = EDIT_DIR / f"variant_{stamp}.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [
        str(_PYTHON_BIN), str(_VARIANT_HELPER),
        "--broll-folder", str(broll_path),
        "--script-text", script,
        "--voice-id", voice,
        "--output", str(out_path),
        "--json",
    ]
    # Optional passthroughs — only forward when client specified them.
    passthrough_int = ["width", "height", "seed",
                       "caption_size", "caption_max_chars",
                       "caption_alignment", "caption_margin_v",
                       "caption_gap_frames", "caption_max_lines"]
    passthrough_float = ["caption_min_duration", "caption_tail_pad",
                         "tts_stability", "tts_similarity", "tts_style",
                         "talking_head_threshold", "match_min_score"]
    passthrough_str = ["fps", "caption_font", "caption_bg", "caption_fg",
                       "tts_model", "caption_case", "hook_text", "hook_font",
                       "cta_text", "cta_font", "cta_bg", "cta_fg", "cta_mode",
                       "disclaimer_text", "match", "broll_fallback"]
    flag_map_int = {
        "width": "--width", "height": "--height", "seed": "--seed",
        "caption_size": "--caption-size",
        "caption_max_chars": "--caption-max-chars",
        "caption_alignment": "--caption-alignment",
        "caption_margin_v": "--caption-margin-v",
        "caption_gap_frames": "--caption-gap-frames",
        "caption_max_lines": "--caption-max-lines",
    }
    flag_map_float = {
        "caption_min_duration": "--caption-min-duration",
        "caption_tail_pad": "--caption-tail-pad",
        "tts_stability": "--tts-stability",
        "tts_similarity": "--tts-similarity",
        "tts_style": "--tts-style",
        "talking_head_threshold": "--talking-head-threshold",
        "match_min_score": "--match-min-score",
    }
    flag_map_str = {
        "fps": "--fps", "caption_font": "--caption-font",
        "caption_bg": "--caption-bg", "caption_fg": "--caption-fg",
        "tts_model": "--tts-model", "caption_case": "--caption-case",
        "hook_text": "--hook-text", "hook_font": "--hook-font",
        "cta_text": "--cta-text", "cta_font": "--cta-font",
        "cta_bg": "--cta-bg", "cta_fg": "--cta-fg",
        "cta_mode": "--cta-mode",
        "disclaimer_text": "--disclaimer-text",
        "match": "--match", "broll_fallback": "--broll-fallback",
    }
    for k in passthrough_int:
        if k in payload:
            cmd += [flag_map_int[k], str(int(payload[k]))]
    for k in passthrough_float:
        if k in payload:
            cmd += [flag_map_float[k], str(float(payload[k]))]
    for k in passthrough_str:
        if payload.get(k):
            cmd += [flag_map_str[k], str(payload[k])]
    if payload.get("no_speaker_boost"):
        cmd.append("--no-speaker-boost")
    if payload.get("keep_temps"):
        cmd.append("--keep-temps")
    if payload.get("exclude_talking_heads"):
        cmd.append("--exclude-talking-heads")
    # caption_shadow is tri-state from the client: True | False | omitted.
    # Omitted leaves the helper default ON. Explicit False sends the
    # --no-caption-shadow flag to disable.
    if "caption_shadow" in payload:
        cmd.append("--caption-shadow" if payload["caption_shadow"] else "--no-caption-shadow")
    ex_paths = payload.get("exclude_paths")
    if isinstance(ex_paths, list) and ex_paths:
        cmd.append("--exclude-paths")
        for p in ex_paths:
            cmd.append(str(p))

    # SERVER-SIDE concurrency gate — never run more than MAX_CONCURRENT_RENDERS
    # heavy ffmpeg/NVENC jobs at once, regardless of how many the agent fires.
    # Excess requests await here (they don't fail); this is the hard guardrail
    # against the batch-freeze crash.
    async with _render_semaphore():
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise HTTPException(
            500, f"variant_factory failed (exit {proc.returncode}):\n{stderr[-3000:] or stdout[-2000:]}"
        )

    json_result: dict | None = None
    depth = 0; start = -1
    for i, ch in enumerate(stdout):
        if ch == "{":
            if depth == 0: start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = stdout[start:i + 1]
                try:
                    json_result = json.loads(candidate)
                except json.JSONDecodeError:
                    pass
                start = -1
    if json_result is None:
        raise HTTPException(500, f"could not parse helper JSON:\n{stdout[-2000:]}")
    json_result["stderr_tail"] = stderr[-2000:]
    return json_result


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
