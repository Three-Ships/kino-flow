"""Fetch free, royalty-free B-roll from Pexels / Pixabay — the $0 gap-filler for
lines the local library can't cover (broll_match.py flags these as needs_fallback).

Real 4K footage, no watermark, no attribution required. Downloads into the BROLL
folder so broll_match.py re-embeds and uses it on the next pass. Writes a `.src.json`
provenance sidecar next to each clip (provider, id, url, query, license) — mirrors
the `.gen.json` convention for generative assets. Never delete the sidecars.

Usage
-----
    python helpers/broll_fetch.py --query "window installation crew" \\
        --orientation portrait --count 1 --output BROLL/ --json

    # trim each download to a target length (needs ffmpeg)
    python helpers/broll_fetch.py --query "cozy living room" --dur 4 \\
        --output BROLL/ --provider pexels

Providers
---------
    auto     (default) try Pexels, then Pixabay
    pexels   requires PEXELS_API_KEY
    pixabay  requires PIXABAY_API_KEY

Keys are read from env or video-use/.env (PEXELS_API_KEY / PIXABAY_API_KEY).
Both APIs are free — get keys at pexels.com/api and pixabay.com/api/docs.

Exit codes: 0 ok · 2 usage · 3 no key · 4 no results.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"  # video-use/.env when installed


def die(msg: str, code: int = 2) -> None:
    sys.stderr.write(f"broll_fetch: {msg}\n")
    sys.exit(code)


def load_key(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _get(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download(url: str, dest: Path, timeout: int = 120) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "veditor-broll-fetch"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)


# ── providers ────────────────────────────────────────────────────────────────

def search_pexels(query: str, orientation: str, count: int) -> list[dict]:
    key = load_key("PEXELS_API_KEY")
    if not key:
        return []
    params = {"query": query, "per_page": max(count * 2, 5)}
    if orientation in ("portrait", "landscape", "square"):
        params["orientation"] = orientation
    url = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode(params)
    try:
        data = _get(url, headers={"Authorization": key})
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"broll_fetch: pexels error ({e})\n")
        return []
    out = []
    for v in data.get("videos", []):
        files = sorted(v.get("video_files", []),
                       key=lambda f: (f.get("width") or 0) * (f.get("height") or 0),
                       reverse=True)
        if not files:
            continue
        best = files[0]
        out.append({
            "provider": "pexels", "id": v.get("id"),
            "url": best.get("link"), "width": best.get("width"),
            "height": best.get("height"), "duration": v.get("duration"),
            "page": v.get("url"), "license": "Pexels License (free, no attribution)",
        })
    return out


def search_pixabay(query: str, orientation: str, count: int) -> list[dict]:
    key = load_key("PIXABAY_API_KEY")
    if not key:
        return []
    params = {"key": key, "q": query, "per_page": max(count * 2, 5)}
    url = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode(params)
    try:
        data = _get(url, timeout=30)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"broll_fetch: pixabay error ({e})\n")
        return []
    out = []
    for v in data.get("hits", []):
        streams = v.get("videos", {})
        best = max(streams.values(),
                   key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
                   default=None)
        if not best or not best.get("url"):
            continue
        out.append({
            "provider": "pixabay", "id": v.get("id"),
            "url": best.get("url"), "width": best.get("width"),
            "height": best.get("height"), "duration": v.get("duration"),
            "page": v.get("pageURL"),
            "license": "Pixabay Content License (free, no attribution)",
        })
    return out


def _matches_orientation(item: dict, orientation: str) -> bool:
    w, h = item.get("width") or 0, item.get("height") or 0
    if not w or not h or orientation == "any":
        return True
    if orientation == "portrait":
        return h >= w
    if orientation == "landscape":
        return w >= h
    return True


def trim_clip(src: Path, dur: float) -> None:
    tmp = src.with_suffix(".trim" + src.suffix)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-t", f"{dur:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", str(tmp)],
        capture_output=True)
    if r.returncode == 0 and tmp.exists():
        tmp.replace(src)
    else:
        sys.stderr.write("broll_fetch: trim failed, keeping full clip\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch free B-roll from Pexels/Pixabay.")
    ap.add_argument("--query", required=True, help="search terms")
    ap.add_argument("--output", required=True, type=Path, help="BROLL folder to save into")
    ap.add_argument("--provider", choices=["auto", "pexels", "pixabay"], default="auto")
    ap.add_argument("--orientation", choices=["portrait", "landscape", "square", "any"],
                    default="portrait")
    ap.add_argument("--count", type=int, default=1, help="clips to download")
    ap.add_argument("--dur", type=float, default=0.0, help="trim each clip to N seconds")
    ap.add_argument("--json", action="store_true", help="print downloaded file records")
    args = ap.parse_args()

    out_dir = args.output.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    if args.provider in ("auto", "pexels"):
        results += search_pexels(args.query, args.orientation, args.count)
    if args.provider in ("auto", "pixabay") and len(results) < args.count:
        results += search_pixabay(args.query, args.orientation, args.count)

    if not results:
        if not (load_key("PEXELS_API_KEY") or load_key("PIXABAY_API_KEY")):
            die("no PEXELS_API_KEY or PIXABAY_API_KEY found (env or video-use/.env). "
                "Both are free: pexels.com/api, pixabay.com/api/docs", 3)
        die(f"no results for '{args.query}'", 4)

    results = [r for r in results if _matches_orientation(r, args.orientation)] or results
    picked = results[: args.count]

    saved = []
    slug = "".join(c if c.isalnum() else "_" for c in args.query)[:40]
    for i, item in enumerate(picked):
        ext = ".mp4"
        dest = out_dir / f"stock_{item['provider']}_{slug}_{item['id']}{ext}"
        try:
            _download(item["url"], dest)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"broll_fetch: download failed for {item['url']} ({e})\n")
            continue
        if args.dur > 0:
            trim_clip(dest, args.dur)
        sidecar = dest.with_suffix(dest.suffix + ".src.json")
        record = {**item, "query": args.query, "file": str(dest),
                  "orientation": args.orientation}
        sidecar.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        saved.append(record)
        sys.stderr.write(f"broll_fetch: saved {dest.name}  "
                         f"({item.get('width')}x{item.get('height')}, {item['provider']})\n")

    if not saved:
        die("nothing downloaded", 4)
    if args.json:
        print(json.dumps(saved, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
