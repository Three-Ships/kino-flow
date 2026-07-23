"""Generate / edit images with Google Nano Banana (Gemini image models).

Text-to-image, image editing, multi-reference composition (up to 14
reference images), and text rendering — via the official Gemini API
generateContent endpoint. Writes a provenance sidecar (<output>.gen.json).

READ FIRST: docs/NANO_BANANA_PROMPTING.md — the studio's distilled
prompting guide (generation formula, editing rules, text rendering,
creative-director controls).

Typical studio uses:
  - character stills for Veo ingredients / first frames
  - product hero shots from a reference photo
  - thumbnail / poster frames with rendered text (quote the exact text!)

Auth: GEMINI_API_KEY in video-use/.env.
Model default: gemini-3-pro-image-preview (Nano Banana Pro) — override
with --model or GEMINI_IMAGE_MODEL env. Run --list-models if Google has
renamed things (e.g. gemini-3.1-flash-image = Nano Banana 2, cheaper).

Usage:
    python helpers/nano_banana.py --list-models
    python helpers/nano_banana.py --prompt "..." --output out.png
    python helpers/nano_banana.py --prompt "..." --aspect 9:16 --size 2K --output out.png
    python helpers/nano_banana.py --prompt "Remove the man from the photo" \\
        --image base.png --output edited.png
    python helpers/nano_banana.py --prompt "Place this product on a marble counter..." \\
        --image product.jpg --image style_ref.jpg --output hero.png --json
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
# verified live 2026-07-13: gemini-3-pro-image (NB Pro, stable),
# gemini-3.1-flash-image (NB 2, cheap tier), gemini-3.1-flash-lite-image
DEFAULT_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3-pro-image")
FLASH_MODEL = "gemini-3.1-flash-image"
MAX_REF_IMAGES = 14

EXT_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def load_api_key() -> str:
    """Same lookup convention as tts_voice.py / veo_video.py — keep in sync."""
    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "GEMINI_API_KEY":
                    return v.strip().strip('"').strip("'")
    v = os.environ.get("GEMINI_API_KEY", "")
    if not v:
        sys.exit("GEMINI_API_KEY not found in video-use/.env or environment. "
                 "Get one at https://aistudio.google.com/apikey and add: GEMINI_API_KEY=...")
    return v


def _headers(api_key: str) -> dict:
    return {"x-goog-api-key": api_key, "Content-Type": "application/json"}


def list_models(api_key: str) -> list[dict]:
    models, page_token = [], None
    while True:
        params = {"pageSize": 200}
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(f"{API_BASE}/models", headers=_headers(api_key),
                         params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        models += data.get("models", [])
        page_token = data.get("nextPageToken")
        if not page_token:
            return models


def generate(api_key: str, model: str, prompt: str, output: Path,
             images: list[Path], aspect: str | None, size: str | None) -> dict:
    parts: list[dict] = [{"text": prompt}]
    for img in images[:MAX_REF_IMAGES]:
        mime = mimetypes.guess_type(str(img))[0] or "image/png"
        parts.append({"inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(img.read_bytes()).decode(),
        }})

    body: dict = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    image_config: dict = {}
    if aspect:
        image_config["aspectRatio"] = aspect
    if size:
        image_config["imageSize"] = size
    if image_config:
        body["generationConfig"]["imageConfig"] = image_config

    r = requests.post(f"{API_BASE}/models/{model}:generateContent",
                      headers=_headers(api_key), json=body, timeout=300)
    if r.status_code == 404:
        sys.exit(f"model '{model}' not found (404). Run --list-models to see "
                 "current image model names, then pass --model.")
    if r.status_code == 400 and "imageConfig" in r.text:
        # older image models reject imageConfig — retry without it
        body["generationConfig"].pop("imageConfig", None)
        r = requests.post(f"{API_BASE}/models/{model}:generateContent",
                          headers=_headers(api_key), json=body, timeout=300)
    r.raise_for_status()
    data = r.json()

    cands = data.get("candidates") or []
    if not cands:
        sys.exit(f"no candidates returned (safety block?): {json.dumps(data)[:800]}")

    saved: list[str] = []
    texts: list[str] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    idx = 0
    for part in (cands[0].get("content") or {}).get("parts", []):
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            ext = EXT_BY_MIME.get(mime, ".png")
            path = output if idx == 0 else output.with_stem(f"{output.stem}_{idx + 1}")
            if path.suffix.lower() != ext:
                path = path.with_suffix(ext)
            path.write_bytes(base64.b64decode(inline["data"]))
            saved.append(str(path))
            idx += 1
        elif part.get("text"):
            texts.append(part["text"])

    if not saved:
        sys.exit("response contained no image data. Model text was: "
                 + " ".join(texts)[:500])

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "helper": "nano_banana.py",
        "model": model,
        "prompt": prompt,
        "reference_images": [str(p) for p in images],
        "aspect": aspect,
        "size": size,
        "outputs": saved,
        "model_text": " ".join(texts)[:1000] or None,
    }
    Path(saved[0] + ".gen.json").write_text(json.dumps(result, indent=2),
                                            encoding="utf-8")
    for s in saved:
        print(f"wrote: {s}", file=sys.stderr)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", help="Image prompt (follow docs/NANO_BANANA_PROMPTING.md)")
    ap.add_argument("--output", type=Path, help="Output image path (.png)")
    ap.add_argument("--image", type=Path, action="append", default=[],
                    help="Reference/base image — repeatable, up to 14")
    ap.add_argument("--aspect", default=None,
                    help="Aspect ratio: 1:1, 3:2, 2:3, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9")
    ap.add_argument("--size", default=None, choices=["1K", "2K", "4K"],
                    help="Output resolution class (Pro/NB2 models)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Image model id (default {DEFAULT_MODEL})")
    ap.add_argument("--list-models", action="store_true",
                    help="List available image-capable models and exit")
    ap.add_argument("--json", dest="emit_json", action="store_true",
                    help="Print structured result on stdout")
    args = ap.parse_args()

    api_key = load_api_key()

    if args.list_models:
        for m in list_models(api_key):
            name = m.get("name", "")
            if "image" in name.lower() or "imagen" in name.lower():
                print(f"{name.removeprefix('models/'):44s} {m.get('displayName', '')}")
        return

    if not args.prompt or not args.output:
        ap.error("--prompt and --output are required (or use --list-models)")
    for img in args.image:
        if not img.exists():
            sys.exit(f"image not found: {img}")

    result = generate(api_key, args.model, args.prompt, args.output,
                      args.image, args.aspect, args.size)
    if args.emit_json:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
