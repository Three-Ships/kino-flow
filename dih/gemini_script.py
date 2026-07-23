"""Generate a Dog is Human ad script with Gemini.

Input: the brief (a notes file / pasted Slack note) + everything in knowledge/.
Output: a structured script — an ordered list of spoken segments, each already
tagged with the best-fitting b-roll label from the labels that actually exist in
your broll/ library. That tag is what the matcher uses (label-first); a blank tag
means "no good label, use the visual CLIP fallback".

Uses the Gemini REST API (generateContent) with structured JSON output, so no
extra SDK is needed — just `requests` (already a video-use dependency).

CLI:
    python gemini_script.py --note notes/itchy.md --json
    python gemini_script.py --note-text "Platform: TikTok ..." --out script.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

import common

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "platform": {"type": "string"},
        "estimated_seconds": {"type": "number"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": ["hook", "body", "cta"]},
                    "text": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["role", "text", "label"],
            },
        },
    },
    "required": ["title", "platform", "segments"],
}


def build_prompt(cfg: dict, note_text: str, labels: list[str]) -> str:
    knowledge = common.load_knowledge(cfg)
    v = cfg.get("video", {})
    target = v.get("target_seconds", 22)
    lo, hi = v.get("min_seconds", 12), v.get("max_seconds", 34)
    label_list = "\n".join(f"- {l}" for l in labels) or "(none defined yet)"
    platform = note_text_platform(note_text) or "social"

    return f"""You are the scriptwriter for {cfg.get('client', 'the client')}, writing a short
direct-response video ad for {platform} (Meta / TikTok).

Write ONE tight voiceover script. It will be read by an AI voice and shown over
b-roll footage, so every line must be speakable and pair with a visual.

=== BRAND & PRODUCT KNOWLEDGE (learn the voice, facts, and compliance rules) ===
{knowledge or '(no knowledge docs provided yet)'}

=== THIS VIDEO'S BRIEF (from the client) ===
{note_text}

=== AVAILABLE B-ROLL LABELS (you may ONLY choose from these) ===
{label_list}

=== RULES ===
- Structure: exactly one "hook" segment first, then 2-5 "body" segments, then one "cta" segment last.
- Total spoken length should be about {target}s (never below {lo}s or above {hi}s). Roughly 2.7 words/second.
- Each segment is ONE short spoken sentence (about 6-16 words).
- For each segment, pick the single best-fitting `label` from the AVAILABLE list
  that matches what should be ON SCREEN while that line is spoken. If NOTHING in
  the list fits, set label to "" (empty string) — do not invent labels.
- Obey every compliance / do-not-say rule in the knowledge and the brief.
- No emojis, no hashtags, no stage directions in the `text` — spoken words only.
- Match the brand voice: warm, plain, confident, no hype or medical claims.

Return JSON matching the schema."""


def note_text_platform(note_text: str) -> str:
    for line in note_text.splitlines():
        if line.lower().strip().startswith("platform"):
            return line.split(":", 1)[-1].strip()
    return ""


def generate_script(cfg: dict, note_text: str, api_key: str | None = None) -> dict:
    api_key = api_key or common.load_api_key("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not found in video-use/.env or environment.")

    labels = common.list_labels(cfg, only_with_clips=False)
    prompt = build_prompt(cfg, note_text, labels)
    model = cfg.get("gemini_model", "gemini-3.5-flash")

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    r = requests.post(
        f"{API_BASE}/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    if r.status_code != 200:
        raise SystemExit(f"Gemini error {r.status_code}: {r.text[:800]}")
    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise SystemExit(f"Unexpected Gemini response: {json.dumps(data)[:800]} ({e})")
    script = json.loads(text)
    return _clean(script, labels, cfg)


def _clean(script: dict, labels: list[str], cfg: dict) -> dict:
    """Keep only valid labels; guarantee hook-first, cta-last ordering exists."""
    label_set = set(labels)
    segs = []
    for s in script.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        label = (s.get("label") or "").strip()
        if label and label not in label_set:
            label = ""   # unknown -> fallback
        role = s.get("role", "body")
        segs.append({"role": role, "text": text, "label": label})
    if not segs:
        raise SystemExit("Gemini returned no usable segments.")
    script["segments"] = segs
    script.setdefault("title", "dih-video")
    return script


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a DiH ad script with Gemini.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--note", type=Path, help="path to a notes file (the brief)")
    src.add_argument("--note-text", help="the brief as a literal string")
    ap.add_argument("--out", type=Path, help="write the script JSON here")
    ap.add_argument("--json", action="store_true", help="print script JSON to stdout")
    args = ap.parse_args()

    cfg = common.load_config()
    note_text = args.note.read_text(encoding="utf-8") if args.note else args.note_text
    script = generate_script(cfg, note_text)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(script, indent=2), encoding="utf-8")
        common.eprint(f"wrote {args.out}")
    if args.json or not args.out:
        print(json.dumps(script, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
