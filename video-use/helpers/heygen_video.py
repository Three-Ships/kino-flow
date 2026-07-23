"""Generate avatar talking-head videos via HeyGen API.

Reads HEYGEN_API_KEY from `video-use/.env` (preferred) or environment.

Three modes:

  Listing (cache for the studio UI):
    python helpers/heygen_video.py --list-avatars            # private + public, JSON
    python helpers/heygen_video.py --list-voices             # all voices, JSON
    python helpers/heygen_video.py --list-avatars --output studio/static/heygen_avatars.json
    python helpers/heygen_video.py --list-voices  --output studio/static/heygen_voices.json

  Generate a full talking-head segment:
    python helpers/heygen_video.py \\
      --script script.txt \\
      --avatar-id a78e96535de64bd4bbf758c1ec0eb90a \\
      --voice-id <voice_id> \\
      --output videos/edit/heygen_segment.mp4 \\
      --width 1920 --height 1080 \\
      --background "#0e1a26"

  Generate a transparent-background overlay (for PIP composite over your footage):
    python helpers/heygen_video.py \\
      --script "Welcome back to..." \\
      --avatar-id a78e96535de64bd4bbf758c1ec0eb90a \\
      --voice-id <voice_id> \\
      --output videos/edit/heygen_overlay.webm \\
      --transparent

Notes:
- The Creator plan's API limit is enforced server-side. If you hit a quota
  error this exits non-zero with the HeyGen response body printed to stderr.
- Polling cadence is 8s with a 12-min hard timeout. A 30-60s clip typically
  finishes in 60-180s of wall time.
- Transparent background renders as VP9/WebM with alpha; for h264 PIP work,
  do the matte composite in ffmpeg downstream.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


API_BASE = "https://api.heygen.com"
POLL_INTERVAL_S = 8
POLL_TIMEOUT_S  = 12 * 60   # 12 min — generous for short clips, fails fast on stuck jobs


def load_api_key() -> str:
    """Match tts_voice.py / transcribe.py lookup order."""
    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "HEYGEN_API_KEY":
                    return v.strip().strip('"').strip("'")
    v = os.environ.get("HEYGEN_API_KEY", "")
    if not v:
        sys.exit("HEYGEN_API_KEY not found in video-use/.env or environment")
    return v


def _request(method: str, path: str, key: str, body: dict | None = None,
             timeout: int = 60) -> dict:
    url = API_BASE + path
    headers = {
        "X-Api-Key": key,
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HeyGen {method} {path} → HTTP {e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"HeyGen {method} {path} → network error: {e.reason}") from None


def list_avatars(key: str) -> dict:
    """Returns {"avatars": [...], "talking_photos": [...]} including public ones.

    HeyGen v2/avatars returns the user's accessible avatars — on Creator+ this
    includes public Heygen-provided avatars alongside private/cloned ones.
    """
    return _request("GET", "/v2/avatars", key).get("data", {})


def list_voices(key: str) -> dict:
    return _request("GET", "/v2/voices", key).get("data", {})


def submit_video(key: str, *, script: str, avatar_id: str, voice_id: str,
                 width: int = 1920, height: int = 1080,
                 background: str = "#000000",
                 avatar_style: str = "normal",
                 transparent: bool = False,
                 voice_speed: float | None = None) -> str:
    """Returns a HeyGen video_id."""
    voice_block: dict = {
        "type": "text",
        "input_text": script,
        "voice_id": voice_id,
    }
    if voice_speed is not None:
        voice_block["speed"] = voice_speed

    if transparent:
        bg_block = {"type": "color", "value": "transparent"}
    else:
        bg_block = {"type": "color", "value": background}

    body = {
        "video_inputs": [
            {
                "character": {
                    "type": "avatar",
                    "avatar_id": avatar_id,
                    "avatar_style": avatar_style,
                },
                "voice": voice_block,
                "background": bg_block,
            }
        ],
        "dimension": {"width": width, "height": height},
    }

    res = _request("POST", "/v2/video/generate", key, body=body)
    if res.get("error"):
        raise RuntimeError(f"HeyGen submit error: {res['error']}")
    video_id = res.get("data", {}).get("video_id")
    if not video_id:
        raise RuntimeError(f"No video_id in submit response: {res}")
    return video_id


def poll_until_ready(key: str, video_id: str) -> dict:
    """Block until the video is completed or failed. Returns the final status data."""
    deadline = time.time() + POLL_TIMEOUT_S
    last_status = None
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        res = _request("GET", f"/v1/video_status.get?video_id={video_id}", key)
        d = res.get("data", {}) or {}
        s = d.get("status")
        if s != last_status:
            print(f"[heygen] status={s}", file=sys.stderr, flush=True)
            last_status = s
        if s == "completed":
            return d
        if s in ("failed", "error"):
            err = d.get("error") or d
            raise RuntimeError(f"HeyGen render failed: {err}")
    raise RuntimeError(f"HeyGen poll timed out after {POLL_TIMEOUT_S}s (video_id={video_id})")


def download(url: str, output: Path, *, chunk: int = 1 << 16) -> int:
    """Stream-download to disk. Returns byte count."""
    output.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    req = urllib.request.Request(url, headers={"User-Agent": "veditor-studio/1"})
    with urllib.request.urlopen(req, timeout=300) as r, open(output, "wb") as f:
        while True:
            buf = r.read(chunk)
            if not buf:
                break
            f.write(buf)
            n += len(buf)
    return n


def generate(key: str, *, script: str, avatar_id: str, voice_id: str,
             output: Path, **kwargs) -> dict:
    print(f"[heygen] submitting render: avatar={avatar_id} voice={voice_id} → {output}",
          file=sys.stderr, flush=True)
    video_id = submit_video(key, script=script, avatar_id=avatar_id,
                            voice_id=voice_id, **kwargs)
    print(f"[heygen] video_id={video_id} (polling every {POLL_INTERVAL_S}s)",
          file=sys.stderr, flush=True)
    final = poll_until_ready(key, video_id)
    video_url = final.get("video_url")
    if not video_url:
        raise RuntimeError(f"completed but no video_url: {final}")
    bytes_written = download(video_url, output)
    return {
        "video_id":     video_id,
        "output":       str(output),
        "bytes":        bytes_written,
        "duration":     final.get("duration"),
        "thumbnail":    final.get("thumbnail_url"),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


def write_or_print(payload: dict, output: str | None) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
        print(f"wrote {output}", file=sys.stderr)
    else:
        print(text)


def main() -> None:
    ap = argparse.ArgumentParser(description="HeyGen video generator")
    # listing modes
    ap.add_argument("--list-avatars", action="store_true",
                    help="Fetch avatars (private + accessible public) as JSON")
    ap.add_argument("--list-voices", action="store_true",
                    help="Fetch voices as JSON")
    # generation
    ap.add_argument("--script", help="Path to script .txt OR literal text")
    ap.add_argument("--text", help="Literal script text (alternative to --script)")
    ap.add_argument("--avatar-id")
    ap.add_argument("--voice-id")
    ap.add_argument("--output", help="Output path. For listings, writes JSON; for generation, writes MP4/WebM.")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--background", default="#0e1a26",
                    help='Background color hex (e.g. "#0e1a26"). Ignored when --transparent.')
    ap.add_argument("--avatar-style", default="normal", choices=["normal", "circle", "closeUp"])
    ap.add_argument("--transparent", action="store_true",
                    help="Render with transparent background (WebM/VP9 with alpha) — for PIP overlay.")
    ap.add_argument("--voice-speed", type=float, default=None,
                    help="Voice speed multiplier (0.5-1.5). Default: HeyGen voice default.")
    ap.add_argument("--json", action="store_true",
                    help="Print structured JSON result to stdout after generate.")
    args = ap.parse_args()

    key = load_api_key()

    # Listing modes — API call, then write/print as JSON.
    if args.list_avatars:
        write_or_print(list_avatars(key), args.output)
        return
    if args.list_voices:
        write_or_print(list_voices(key), args.output)
        return

    # Generation mode requires script + avatar + voice + output.
    if not args.output:
        ap.error("--output is required for generation")
    if not args.avatar_id:
        ap.error("--avatar-id is required for generation")
    if not args.voice_id:
        ap.error("--voice-id is required for generation")

    # Resolve script text: --text wins, else --script (file or literal).
    script_text = args.text
    if script_text is None:
        if not args.script:
            ap.error("--script (path or text) or --text is required for generation")
        p = Path(args.script)
        script_text = p.read_text(encoding="utf-8") if p.exists() else args.script
    script_text = script_text.strip()
    if not script_text:
        ap.error("script text is empty")

    res = generate(
        key,
        script=script_text,
        avatar_id=args.avatar_id,
        voice_id=args.voice_id,
        output=Path(args.output),
        width=args.width,
        height=args.height,
        background=args.background,
        avatar_style=args.avatar_style,
        transparent=args.transparent,
        voice_speed=args.voice_speed,
    )
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"OK: {res['output']} ({res['bytes']:,} bytes)")


if __name__ == "__main__":
    main()
