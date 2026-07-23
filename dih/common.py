"""Shared helpers for the Dog is Human (DiH) auto-editor.

Small, dependency-light utilities used by every stage: config loading, API-key
lookup (reuses the video-use/.env convention), path resolution, b-roll label
discovery, knowledge-doc loading, and slugify. Keep this file boring and stable.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# ── locations ────────────────────────────────────────────────────────────────
DIH_ROOT = Path(__file__).resolve().parent          # E:\Claude Veditor\dih
VEDITOR_ROOT = DIH_ROOT.parent                       # E:\Claude Veditor
HELPERS = VEDITOR_ROOT / "video-use" / "helpers"     # existing helper scripts
ENV_FILE = VEDITOR_ROOT / "video-use" / ".env"       # ELEVENLABS_API_KEY, GEMINI_API_KEY

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
DOC_EXTS = {".md", ".txt", ".pdf"}


def load_config() -> dict:
    return json.loads((DIH_ROOT / "config.json").read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    (DIH_ROOT / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _p(cfg: dict, key: str, default: str) -> Path:
    """Resolve a config path relative to the DiH root."""
    return (DIH_ROOT / cfg.get("paths", {}).get(key, default)).resolve()


def notes_dir(cfg: dict) -> Path:   return _p(cfg, "notes", "notes")
def knowledge_dir(cfg: dict) -> Path: return _p(cfg, "knowledge", "knowledge")
def output_dir(cfg: dict) -> Path:  return _p(cfg, "output", "output")


def broll_dir(cfg: dict) -> Path:
    return (DIH_ROOT / cfg.get("broll", {}).get("folder", "broll")).resolve()


def music_dir(cfg: dict) -> Path:
    return (DIH_ROOT / cfg.get("music", {}).get("folder", "music")).resolve()


# ── secrets ──────────────────────────────────────────────────────────────────
def load_api_key(name: str) -> str:
    """Read a key from video-use/.env, then the environment. Same convention as
    the existing helpers (tts_voice.py / veo_video.py)."""
    for candidate in (ENV_FILE, Path(".env")):
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    return os.environ.get(name, "")


# ── b-roll labels ────────────────────────────────────────────────────────────
def _has_clips(folder: Path) -> bool:
    return any(
        f.is_file() and f.suffix.lower() in (VIDEO_EXTS | IMAGE_EXTS)
        and not f.name.startswith("._")
        for f in folder.rglob("*")
    )


def list_labels(cfg: dict, only_with_clips: bool = False) -> list[str]:
    """Every immediate sub-folder of the b-roll dir is a label. Optionally hide
    empty labels (no footage yet)."""
    root = broll_dir(cfg)
    if not root.is_dir():
        return []
    labels = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        if only_with_clips and not _has_clips(d):
            continue
        labels.append(d.name)
    return labels


def clips_for_label(cfg: dict, label: str) -> list[Path]:
    folder = broll_dir(cfg) / label
    if not folder.is_dir():
        return []
    return sorted(
        f for f in folder.rglob("*")
        if f.is_file() and f.suffix.lower() in (VIDEO_EXTS | IMAGE_EXTS)
        and not f.name.startswith("._")
    )


# ── knowledge docs ───────────────────────────────────────────────────────────
def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except Exception:
            return f"[could not read {path.name}: install pypdf to extract PDF text]"
    try:
        reader = PdfReader(str(path))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception as e:  # noqa: BLE001
        return f"[could not read {path.name}: {e}]"


def load_knowledge(cfg: dict, max_chars: int = 40_000) -> str:
    """Concatenate all knowledge docs into one reference block for the prompt."""
    root = knowledge_dir(cfg)
    if not root.is_dir():
        return ""
    chunks: list[str] = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in DOC_EXTS:
            continue
        if f.name.lower() == "readme.md":
            continue
        if f.suffix.lower() == ".pdf":
            text = _read_pdf(f)
        else:
            text = f.read_text(encoding="utf-8", errors="replace")
        chunks.append(f"### {f.relative_to(root)}\n{text.strip()}")
    blob = "\n\n".join(chunks)
    return blob[:max_chars]


# ── misc ─────────────────────────────────────────────────────────────────────
def slugify(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (s or "video")[:maxlen]


def eprint(*args) -> None:
    print(*args, file=sys.stderr, flush=True)
