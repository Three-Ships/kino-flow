"""Generate VO audio from a script using ElevenLabs TTS.

Reads a plain-text script (or `--text` literal), calls the ElevenLabs
text-to-speech endpoint with a chosen voice ID, and writes a WAV/MP3 to
the output path. Cloned voices work the same as preset voices — you just
pass their voice ID.

Look up voice IDs:
    python helpers/tts_voice.py --list-voices            # all voices
    python helpers/tts_voice.py --list-voices --json     # machine-readable
    python helpers/tts_voice.py --list-voices --mine     # only your cloned voices

Generate VO:
    python helpers/tts_voice.py --script script.txt --voice <voice_id> --output vo.wav
    python helpers/tts_voice.py --text  "Hello there." --voice <voice_id> --output vo.mp3
    python helpers/tts_voice.py --script script.txt --voice <voice_id> --output vo.wav \\
        --model eleven_multilingual_v2 --stability 0.5 --similarity 0.75 --style 0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


API_BASE = "https://api.elevenlabs.io/v1"


def load_api_key() -> str:
    """Same lookup convention as transcribe.py — keep in sync."""
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


def list_voices(api_key: str, only_mine: bool = False) -> list[dict]:
    r = requests.get(
        f"{API_BASE}/voices",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    voices = r.json().get("voices", [])
    if only_mine:
        # `category` is "cloned" for user-cloned voices, "premade" for stock.
        voices = [v for v in voices if v.get("category") == "cloned"]
    return voices


def synthesize(
    api_key: str,
    voice_id: str,
    text: str,
    model: str,
    output: Path,
    stability: float,
    similarity: float,
    style: float,
    speaker_boost: bool,
    output_format_q: str | None = None,
) -> int:
    """Stream-download the synthesized audio to `output`. Returns bytes written.

    `output_format_q` accepts ElevenLabs format strings like:
      mp3_22050_32 (Free), mp3_44100_64 (Starter), mp3_44100_128 (Creator),
      mp3_44100_192 (Creator+), pcm_16000/pcm_22050/pcm_24000 (Starter+),
      pcm_44100 (Pro+). When None, picks a tier-friendly default based on the
      output extension.
    """
    fmt = output.suffix.lower().lstrip(".")
    if fmt not in ("wav", "mp3"):
        raise SystemExit(f"output must be .wav or .mp3 (got .{fmt})")
    accept = "audio/wav" if fmt == "wav" else "audio/mpeg"
    if output_format_q is None:
        # Defaults that work on Starter tier. Bump via --output-format if your
        # account is Creator+ and you want higher quality.
        output_format_q = "pcm_22050" if fmt == "wav" else "mp3_44100_64"

    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability":         stability,
            "similarity_boost":  similarity,
            "style":             style,
            "use_speaker_boost": speaker_boost,
        },
    }

    url = f"{API_BASE}/text-to-speech/{voice_id}?output_format={output_format_q}"
    r = requests.post(
        url,
        headers={
            "xi-api-key":  api_key,
            "Accept":      accept,
            "Content-Type": "application/json",
        },
        json=payload,
        stream=True,
        timeout=600,
    )
    if r.status_code != 200:
        sys.stderr.write(f"ElevenLabs TTS error {r.status_code}: {r.text}\n")
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
    ap = argparse.ArgumentParser(description="Generate VO audio via ElevenLabs TTS.")
    ap.add_argument("--list-voices", action="store_true", help="list available voices and exit")
    ap.add_argument("--mine", action="store_true", help="with --list-voices, show only your cloned voices")

    src = ap.add_mutually_exclusive_group()
    src.add_argument("--script", help="path to a plain-text script file")
    src.add_argument("--text",   help="literal text to synthesize")

    ap.add_argument("--voice", help="voice ID (your cloned voice's ID, or a preset's)")
    ap.add_argument("--output", help="output path (.wav or .mp3)")

    ap.add_argument("--model", default="eleven_multilingual_v2",
                    help="model_id (default: eleven_multilingual_v2; turbo: eleven_turbo_v2_5)")
    ap.add_argument("--stability",  type=float, default=0.5, help="0..1 (default 0.5)")
    ap.add_argument("--similarity", type=float, default=0.75, help="0..1 (default 0.75)")
    ap.add_argument("--style",      type=float, default=0.0,  help="0..1 (default 0.0; raise for more expressiveness)")
    ap.add_argument("--no-speaker-boost", action="store_true", help="disable speaker boost")
    ap.add_argument("--output-format", default=None,
                    help="ElevenLabs format string (e.g. mp3_44100_64, mp3_44100_128, pcm_22050). "
                         "Default picks a tier-friendly variant based on output extension.")

    ap.add_argument("--json", dest="emit_json", action="store_true", help="machine-readable result on stdout")
    args = ap.parse_args()

    api_key = load_api_key()

    if args.list_voices:
        voices = list_voices(api_key, only_mine=args.mine)
        if args.emit_json:
            print(json.dumps(voices, indent=2))
        else:
            print(f"{'NAME':32}  {'CATEGORY':10}  VOICE_ID")
            print("-" * 80)
            for v in voices:
                print(f"{v.get('name','?')[:32]:32}  {v.get('category','?')[:10]:10}  {v.get('voice_id','?')}")
        return 0

    if not args.voice or not args.output:
        ap.error("--voice and --output are required for synthesis (or use --list-voices)")
    if not args.script and not args.text:
        ap.error("provide --script or --text")

    text = (
        Path(args.script).read_text(encoding="utf-8")
        if args.script else args.text
    ).strip()
    if not text:
        raise SystemExit("script/text is empty after trimming")

    output = Path(args.output).resolve()
    bytes_written = synthesize(
        api_key,
        args.voice,
        text,
        args.model,
        output,
        args.stability,
        args.similarity,
        args.style,
        not args.no_speaker_boost,
        args.output_format,
    )

    result = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "output":      str(output),
        "voice_id":    args.voice,
        "model":       args.model,
        "char_count":  len(text),
        "bytes":       bytes_written,
        "settings": {
            "stability":     args.stability,
            "similarity":    args.similarity,
            "style":         args.style,
            "speaker_boost": not args.no_speaker_boost,
        },
    }
    if args.emit_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"OK — VO written to {output} ({bytes_written:,} bytes, {len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
