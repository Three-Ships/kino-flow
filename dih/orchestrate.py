"""End-to-end DiH pipeline: brief -> script -> VO -> b-roll match -> final ad.

run_one()   builds a single video from one brief and drops it in its own folder
            under output/.
run_batch() builds up to `batch_size` (default 8) videos from the briefs in
            notes/ — your weekly deliverable in one call.

Each video's folder contains:
  brief.md · script.json · script.txt · vo.mp3 · edl.json · final.mp4 · meta.json
"""

from __future__ import annotations

import json
import random
import sys
import traceback
from datetime import datetime
from pathlib import Path

import common
from gemini_script import generate_script
from label_match import build_edl, probe_duration
from compose import render_final

# reuse the existing ElevenLabs helper
sys.path.insert(0, str(common.HELPERS))
import tts_voice  # noqa: E402


def _status(cb, stage: str, msg: str = "") -> None:
    if cb:
        cb(stage, msg)
    common.eprint(f"[{stage}] {msg}")


def _pick_music(cfg: dict) -> Path | None:
    if not cfg.get("music", {}).get("enabled", True):
        return None
    mdir = common.music_dir(cfg)
    if not mdir.is_dir():
        return None
    pool = [f for f in mdir.rglob("*")
            if f.is_file() and f.suffix.lower() in common.AUDIO_EXTS
            and not f.name.startswith("._")]
    return random.choice(pool) if pool else None


def _vo_text(script: dict) -> str:
    return " ".join(s["text"].strip() for s in script["segments"]).strip()


def run_one(cfg: dict, note_text: str, note_name: str,
            on_status=None, seed: int | None = None) -> dict:
    """Build one video. Returns a result dict (also written as meta.json)."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = common.slugify(note_name or "video")
    out_dir = common.output_dir(cfg) / f"{ts}_{slug}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "brief.md").write_text(note_text, encoding="utf-8")

    result = {"note": note_name, "folder": str(out_dir), "status": "running",
              "started_at": datetime.now().isoformat()}
    try:
        # 1. script
        _status(on_status, "script", "generating script with Gemini …")
        script = generate_script(cfg, note_text)
        (out_dir / "script.json").write_text(
            json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
        vo_text = _vo_text(script)
        (out_dir / "script.txt").write_text(vo_text, encoding="utf-8")

        # 2. voiceover
        voice_id = cfg.get("voice_id", "").strip()
        if not voice_id:
            raise SystemExit("No voice_id set in config.json — pick a voice in the UI first.")
        _status(on_status, "voiceover", "synthesizing ElevenLabs VO …")
        api_key = common.load_api_key("ELEVENLABS_API_KEY")
        if not api_key:
            raise SystemExit("ELEVENLABS_API_KEY not found in video-use/.env.")
        vo_path = out_dir / "vo.mp3"
        vs = cfg.get("voice_settings", {})
        tts_voice.synthesize(
            api_key, voice_id, vo_text, cfg.get("voice_model", "eleven_multilingual_v2"),
            vo_path, vs.get("stability", 0.5), vs.get("similarity", 0.75),
            vs.get("style", 0.0), vs.get("speaker_boost", True))
        vo_duration = probe_duration(vo_path)
        if vo_duration <= 0:
            raise SystemExit("Could not measure VO duration (is ffmpeg on PATH?).")

        # 3. b-roll EDL (label-first, CLIP fallback)
        _status(on_status, "broll", "matching b-roll to script …")
        edl, edl_report = build_edl(cfg, script, vo_duration, seed=seed)
        (out_dir / "edl.json").write_text(
            json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8")

        # 4. compose
        music = _pick_music(cfg)
        _status(on_status, "compose", "rendering final video …")
        comp = render_final(cfg, vo_path, edl, out_dir / "final.mp4", music)

        result.update({
            "status": "done",
            "title": script.get("title"),
            "platform": script.get("platform"),
            "vo_duration_s": round(vo_duration, 2),
            "segments": len(edl),
            "edl_report": edl_report,
            "compose": comp,
            "final": str(out_dir / "final.mp4"),
            "music": str(music) if music else None,
            "finished_at": datetime.now().isoformat(),
        })
    except BaseException as e:  # noqa: BLE001  (SystemExit carries our messages)
        result.update({"status": "error", "error": str(e),
                       "trace": traceback.format_exc()[-1500:],
                       "finished_at": datetime.now().isoformat()})
        _status(on_status, "error", str(e))
    (out_dir / "meta.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def collect_briefs(cfg: dict) -> list[tuple[str, str]]:
    """Return [(note_name, note_text)] from notes/, skipping README and _files."""
    ndir = common.notes_dir(cfg)
    briefs = []
    for f in sorted(ndir.glob("*")):
        if not f.is_file() or f.suffix.lower() not in (".md", ".txt"):
            continue
        if f.name.startswith("_") or f.name.lower() == "readme.md":
            continue
        briefs.append((f.stem, f.read_text(encoding="utf-8")))
    return briefs


def run_batch(cfg: dict, on_status=None, limit: int | None = None) -> list[dict]:
    briefs = collect_briefs(cfg)
    limit = limit or cfg.get("batch_size", 8)
    briefs = briefs[:limit]
    if not briefs:
        _status(on_status, "batch", "no briefs found in notes/.")
        return []
    results = []
    for i, (name, text) in enumerate(briefs, 1):
        _status(on_status, "batch", f"video {i}/{len(briefs)}: {name}")
        results.append(run_one(cfg, text, name, on_status=on_status))
    done = sum(1 for r in results if r["status"] == "done")
    _status(on_status, "batch", f"finished — {done}/{len(results)} succeeded.")
    return results


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Run the DiH auto-editor pipeline.")
    sub = ap.add_mutually_exclusive_group(required=True)
    sub.add_argument("--note", type=Path, help="build one video from this brief file")
    sub.add_argument("--batch", action="store_true", help="build all briefs in notes/")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    cfg = common.load_config()
    if args.note:
        res = run_one(cfg, args.note.read_text(encoding="utf-8"), args.note.stem)
        print(json.dumps(res, indent=2))
    else:
        res = run_batch(cfg, limit=args.limit)
        print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
