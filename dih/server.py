"""Lightweight local web app for the Dog is Human auto-editor.

Runs on http://127.0.0.1:8770 (separate from the main studio on 8765 — it never
touches that server). Serves a simple queue UI: add briefs, pick a voice, run one
or all 8, watch progress, preview/download finished videos.

Start it with start-dih.bat (creates its own venv). Endpoints are all under /api.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import common
import orchestrate

app = FastAPI(title="DiH Auto-Editor")
STATIC = common.DIH_ROOT / "static"

# ── job runner (one at a time, background thread) ────────────────────────────
JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()
_worker: threading.Thread | None = None
_queue: list[str] = []


def _set(job_id: str, **kw):
    with JOB_LOCK:
        JOBS[job_id].update(kw)


def _status_cb(job_id: str):
    def cb(stage: str, msg: str):
        with JOB_LOCK:
            JOBS[job_id]["stage"] = stage
            JOBS[job_id]["message"] = msg
            JOBS[job_id].setdefault("log", []).append({"t": time.time(), "stage": stage, "msg": msg})
    return cb


def _run_worker():
    global _worker
    while True:
        with JOB_LOCK:
            if not _queue:
                _worker = None
                return
            job_id = _queue.pop(0)
            job = JOBS[job_id]
            job["status"] = "running"
        cfg = common.load_config()
        try:
            res = orchestrate.run_one(cfg, job["note_text"], job["note_name"],
                                      on_status=_status_cb(job_id))
            _set(job_id, status=res["status"], result=res)
        except Exception as e:  # noqa: BLE001
            _set(job_id, status="error", message=str(e))


def _enqueue(note_name: str, note_text: str) -> str:
    global _worker
    job_id = uuid.uuid4().hex[:12]
    with JOB_LOCK:
        JOBS[job_id] = {"id": job_id, "note_name": note_name, "note_text": note_text,
                        "status": "queued", "stage": "queued", "message": "", "log": []}
        _queue.append(job_id)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, daemon=True)
            _worker.start()
    return job_id


# ── models ───────────────────────────────────────────────────────────────────
class NoteIn(BaseModel):
    title: str
    text: str


class RunIn(BaseModel):
    name: str | None = None   # None => run all briefs


class ConfigPatch(BaseModel):
    patch: dict


# ── routes ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def get_config():
    return common.load_config()


@app.post("/api/config")
def set_config(body: ConfigPatch):
    cfg = common.load_config()

    def deep(d, p):
        for k, v in p.items():
            if isinstance(v, dict) and isinstance(d.get(k), dict):
                deep(d[k], v)
            else:
                d[k] = v
    deep(cfg, body.patch)
    common.save_config(cfg)
    return cfg


@app.get("/api/voices")
def voices():
    import sys
    sys.path.insert(0, str(common.HELPERS))
    import tts_voice
    key = common.load_api_key("ELEVENLABS_API_KEY")
    if not key:
        raise HTTPException(400, "ELEVENLABS_API_KEY not found in video-use/.env")
    try:
        vs = tts_voice.list_voices(key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"ElevenLabs error: {e}")
    return [{"voice_id": v.get("voice_id"), "name": v.get("name"),
             "category": v.get("category")} for v in vs]


@app.get("/api/labels")
def labels():
    cfg = common.load_config()
    out = []
    for lb in common.list_labels(cfg):
        out.append({"label": lb, "clips": len(common.clips_for_label(cfg, lb))})
    return out


@app.get("/api/knowledge")
def knowledge():
    cfg = common.load_config()
    root = common.knowledge_dir(cfg)
    if not root.is_dir():
        return []
    return [f.name for f in sorted(root.iterdir())
            if f.is_file() and f.suffix.lower() in common.DOC_EXTS
            and f.name.lower() != "readme.md"]


@app.get("/api/notes")
def notes():
    cfg = common.load_config()
    return [{"name": n, "text": t} for n, t in orchestrate.collect_briefs(cfg)]


@app.post("/api/notes")
def add_note(body: NoteIn):
    cfg = common.load_config()
    slug = common.slugify(body.title)
    path = common.notes_dir(cfg) / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.text, encoding="utf-8")
    return {"name": slug, "path": str(path)}


@app.delete("/api/notes/{name}")
def del_note(name: str):
    cfg = common.load_config()
    for ext in (".md", ".txt"):
        p = common.notes_dir(cfg) / f"{name}{ext}"
        if p.exists():
            p.unlink()
            return {"deleted": name}
    raise HTTPException(404, "note not found")


@app.post("/api/run")
def run(body: RunIn):
    cfg = common.load_config()
    briefs = orchestrate.collect_briefs(cfg)
    if body.name:
        briefs = [(n, t) for n, t in briefs if n == body.name]
        if not briefs:
            raise HTTPException(404, "brief not found")
    else:
        briefs = briefs[:cfg.get("batch_size", 8)]
    job_ids = [_enqueue(n, t) for n, t in briefs]
    return {"jobs": job_ids}


@app.get("/api/jobs")
def jobs():
    with JOB_LOCK:
        return [{k: v for k, v in j.items() if k != "note_text"} for j in JOBS.values()]


@app.get("/api/outputs")
def outputs():
    cfg = common.load_config()
    root = common.output_dir(cfg)
    if not root.is_dir():
        return []
    items = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta = d / "meta.json"
        final = d / "final.mp4"
        info = {"folder": d.name, "has_final": final.exists()}
        if meta.exists():
            try:
                info["meta"] = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                pass
        items.append(info)
    return items


@app.get("/file")
def file(path: str):
    """Serve a file from inside the DiH tree only (previews/downloads)."""
    p = Path(path).resolve()
    if common.DIH_ROOT not in p.parents and p != common.DIH_ROOT:
        raise HTTPException(403, "outside DiH root")
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(p))


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8770)
