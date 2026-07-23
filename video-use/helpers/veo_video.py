"""Generate video clips with Google Veo via the official Gemini API.

Text-to-video or image-to-video (first-frame conditioning). Kicks off a
long-running generation, polls until done, downloads the MP4, and writes
a provenance sidecar (<output>.gen.json) with the prompt + parameters so
runs are reproducible and auditable.

READ FIRST: docs/VEO_PROMPTING.md — the studio's distilled Veo-for-ads
prompting guide. Prompts must follow its 7-part anatomy (shot type,
action, setting, character, camera movement, style, audio).

COST: Veo bills per second of output (roughly $0.15/s fast tier,
$0.40+/s standard — verify current pricing). An 8 s clip is real money;
batches multiply it. The orchestrator must state estimated cost and get
user approval before any multi-clip batch.

Auth: GEMINI_API_KEY in video-use/.env (same convention as ElevenLabs).
Model default: veo-3.1-generate-preview — override with --model or
GEMINI_VEO_MODEL env. If Google renames models, run --list-models.

Usage:
    python helpers/veo_video.py --list-models
    python helpers/veo_video.py --prompt "..." --output clip.mp4
    python helpers/veo_video.py --prompt "..." --image first_frame.png \\
        --aspect 9:16 --resolution 1080p --output clip.mp4
    python helpers/veo_video.py --prompt "..." --negative "cartoon, low quality" \\
        --model veo-3.1-fast-generate-preview --output clip.mp4 --json
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = os.environ.get("GEMINI_VEO_MODEL", "veo-3.1-generate-preview")
POLL_INTERVAL_S = 10
POLL_TIMEOUT_S = 900


def load_api_key() -> str:
    """Same lookup convention as tts_voice.py / transcribe.py — keep in sync."""
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


def _encode_image(path: Path) -> dict:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return {"bytesBase64Encoded": base64.b64encode(path.read_bytes()).decode(),
            "mimeType": mime}


def _find_video_payloads(node, out: list) -> None:
    """Recursively collect video URIs / inline bytes from the operation
    response. Google has shuffled the exact response shape between Veo
    versions (generatedSamples vs generatedVideos), so we search rather
    than hardcode a path."""
    if isinstance(node, dict):
        uri = node.get("uri") or node.get("videoUri")
        if isinstance(uri, str) and uri.startswith("http"):
            out.append({"uri": uri})
        b64 = node.get("bytesBase64Encoded")
        if isinstance(b64, str) and len(b64) > 10_000:
            out.append({"b64": b64})
        for v in node.values():
            _find_video_payloads(v, out)
    elif isinstance(node, list):
        for v in node:
            _find_video_payloads(v, out)


def generate(api_key: str, model: str, prompt: str, output: Path,
             image: Path | None, aspect: str, resolution: str | None,
             negative: str | None, duration_s: int | None,
             reference_images: list[Path] | None = None) -> dict:
    instance: dict = {"prompt": prompt}
    if image:
        instance["image"] = _encode_image(image)
    if reference_images:
        # Veo 3.1 subject conditioning: up to 3 images of one person/
        # character/product; appearance is preserved in the output. This is
        # the API-legal stand-in for Flow's @me avatars (which have no API).
        instance["referenceImages"] = [
            {"image": _encode_image(p), "referenceType": "asset"}
            for p in reference_images[:3]
        ]
    parameters: dict = {"aspectRatio": aspect}
    if resolution:
        parameters["resolution"] = resolution
    if negative:
        parameters["negativePrompt"] = negative
    if duration_s:
        parameters["durationSeconds"] = duration_s

    r = requests.post(
        f"{API_BASE}/models/{model}:predictLongRunning",
        headers=_headers(api_key),
        json={"instances": [instance], "parameters": parameters},
        timeout=60,
    )
    if r.status_code == 404:
        sys.exit(f"model '{model}' not found (404). Run --list-models to see "
                 "current Veo model names, then pass --model.")
    if r.status_code >= 400:
        sys.exit(f"Veo API {r.status_code}: {r.text[:1500]}")
    r.raise_for_status()
    op_name = r.json().get("name")
    if not op_name:
        sys.exit(f"no operation name in response: {r.text[:500]}")
    print(f"operation started: {op_name}", file=sys.stderr)

    deadline = time.time() + POLL_TIMEOUT_S
    op: dict = {}
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        pr = requests.get(f"{API_BASE}/{op_name}", headers=_headers(api_key), timeout=30)
        pr.raise_for_status()
        op = pr.json()
        if op.get("done"):
            break
        print("…generating", file=sys.stderr)
    else:
        sys.exit(f"generation did not finish within {POLL_TIMEOUT_S}s (operation: {op_name})")

    if "error" in op:
        sys.exit(f"generation failed: {json.dumps(op['error'])[:800]}")

    payloads: list[dict] = []
    _find_video_payloads(op.get("response", {}), payloads)
    if not payloads:
        sys.exit(f"operation finished but no video found in response: "
                 f"{json.dumps(op)[:800]}")

    output.parent.mkdir(parents=True, exist_ok=True)
    p = payloads[0]
    if "uri" in p:
        with requests.get(p["uri"], headers={"x-goog-api-key": api_key},
                          stream=True, timeout=300, allow_redirects=True) as dr:
            dr.raise_for_status()
            with output.open("wb") as f:
                for chunk in dr.iter_content(1 << 20):
                    f.write(chunk)
    else:
        output.write_bytes(base64.b64decode(p["b64"]))

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "helper": "veo_video.py",
        "model": model,
        "prompt": prompt,
        "negative_prompt": negative,
        "image_input": str(image) if image else None,
        "reference_images": [str(p) for p in (reference_images or [])],
        "aspect": aspect,
        "resolution": resolution,
        "duration_seconds": duration_s,
        "operation": op_name,
        "output": str(output),
        "bytes": output.stat().st_size,
    }
    output.with_suffix(output.suffix + ".gen.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    print(f"wrote: {output} ({output.stat().st_size // 1024} KB)", file=sys.stderr)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", help="Veo prompt (follow docs/VEO_PROMPTING.md anatomy)")
    ap.add_argument("--output", type=Path, help="Output MP4 path")
    ap.add_argument("--image", type=Path, help="First-frame image (image-to-video)")
    ap.add_argument("--reference-image", type=Path, action="append", default=[],
                    dest="reference_images",
                    help="Subject reference image (person/character/product) — "
                         "repeatable, up to 3. Preserves the subject's appearance. "
                         "Requires personGeneration allow_adult for people.")
    ap.add_argument("--aspect", default="9:16", choices=["16:9", "9:16"],
                    help="Aspect ratio (default 9:16 — the studio's ad default)")
    ap.add_argument("--resolution", default="1080p",
                    help="720p or 1080p (default 1080p); omit-able via ''")
    ap.add_argument("--negative", help="Negative prompt (what to avoid)")
    ap.add_argument("--duration-seconds", type=int, default=None,
                    help="Clip length where the model supports it (Veo 3.1: 4/6/8)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Veo model id (default {DEFAULT_MODEL})")
    ap.add_argument("--list-models", action="store_true",
                    help="List available video-capable models and exit")
    ap.add_argument("--json", dest="emit_json", action="store_true",
                    help="Print structured result on stdout")
    args = ap.parse_args()

    api_key = load_api_key()

    if args.list_models:
        for m in list_models(api_key):
            name = m.get("name", "")
            if "veo" in name.lower():
                print(f"{name.removeprefix('models/'):40s} {m.get('displayName', '')}")
        return

    if not args.prompt or not args.output:
        ap.error("--prompt and --output are required (or use --list-models)")
    if args.image and not args.image.exists():
        sys.exit(f"image not found: {args.image}")

    for ref in args.reference_images:
        if not ref.exists():
            sys.exit(f"reference image not found: {ref}")

    result = generate(api_key, args.model, args.prompt, args.output,
                      args.image, args.aspect, args.resolution or None,
                      args.negative, args.duration_seconds,
                      reference_images=args.reference_images)
    if args.emit_json:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
