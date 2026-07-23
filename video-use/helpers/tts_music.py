"""Generate a music bed using ElevenLabs Music.

The ElevenLabs Music API (eleven_music) takes a free-form text prompt
describing the track plus a target length and returns an audio file.
This helper writes the result to disk so it can be picked as a music bed
in the studio's pipeline composer.

Usage:
    python helpers/tts_music.py --prompt "uplifting acoustic instrumental, warm" \\
        --duration 60 --output music.mp3
    python helpers/tts_music.py --prompt-file prompt.txt --duration 90 --output bed.wav --json

The endpoint is in beta — if it returns a non-200, the response body is
printed and the helper exits non-zero so the operator sees the error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


API_URL = "https://api.elevenlabs.io/v1/music"


def load_api_key() -> str:
    """Same lookup convention as transcribe.py / tts_voice.py."""
    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "ELEVENLABS_API_KEY":
                    return v.strip().strip('"').strip("'")
    v = os.environ.get("ELEVENLABS_API_KEY", "")
    if not v:
        sys.exit("ELEVENLABS_API_KEY not found in .env or environment")
    return v


def compose(api_key: str, prompt: str, duration_s: float, output: Path) -> int:
    fmt = output.suffix.lower().lstrip(".")
    if fmt not in ("wav", "mp3"):
        raise SystemExit(f"output must be .wav or .mp3 (got .{fmt})")
    accept = "audio/wav" if fmt == "wav" else "audio/mpeg"
    output_format_q = "pcm_44100" if fmt == "wav" else "mp3_44100_192"

    payload = {
        "prompt": prompt,
        "music_length_ms": int(round(duration_s * 1000)),
    }

    r = requests.post(
        f"{API_URL}?output_format={output_format_q}",
        headers={
            "xi-api-key":   api_key,
            "Accept":       accept,
            "Content-Type": "application/json",
        },
        json=payload,
        stream=True,
        timeout=600,
    )
    if r.status_code != 200:
        sys.stderr.write(f"ElevenLabs Music error {r.status_code}: {r.text}\n")
        r.raise_for_status()

    total = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
                total += len(chunk)
    if total == 0:
        raise SystemExit("ElevenLabs returned 0 bytes")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a music bed via ElevenLabs Music.")

    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt",      help="prompt describing the track")
    src.add_argument("--prompt-file", help="path to a file containing the prompt")

    ap.add_argument("--duration", type=float, required=True, help="target duration in seconds (max ~300 per API)")
    ap.add_argument("--output",   required=True, help="output path (.wav or .mp3)")
    ap.add_argument("--json", dest="emit_json", action="store_true", help="machine-readable result on stdout")
    args = ap.parse_args()

    if args.duration <= 0 or args.duration > 600:
        ap.error("--duration must be between 0 and 600 seconds")

    prompt = (
        Path(args.prompt_file).read_text(encoding="utf-8")
        if args.prompt_file else args.prompt
    ).strip()
    if not prompt:
        raise SystemExit("prompt is empty")

    api_key = load_api_key()
    output = Path(args.output).resolve()
    bytes_written = compose(api_key, prompt, args.duration, output)

    result = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "output":      str(output),
        "duration_s":  args.duration,
        "prompt":      prompt,
        "bytes":       bytes_written,
    }
    if args.emit_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"OK — music bed written to {output} ({bytes_written:,} bytes, {args.duration}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
