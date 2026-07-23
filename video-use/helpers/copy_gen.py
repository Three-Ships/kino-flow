"""Generate ad copy locally via Ollama — the $0/token replacement for the
Opus copywriting steps in variant_factory.py and streamlined_ad.py.

Why this exists
---------------
Telemetry (videos/edit/jobs.jsonl) shows the copy-generation jobs — "invent an
angle, write a hook + bullets + CTA" — are the recurring $2–4.50 Opus jobs.
The actual output is a few hundred words; the cost is context, not thinking.
A local model does this for free. Claude Code stays the orchestrator and only
calls out to this helper for the wordsmithing.

Contract (matches the studio helper pattern)
--------------------------------------------
Reads a local Ollama endpoint (OpenAI-compatible, no key, no network egress).
Prints strict JSON on stdout so the caller can parse it deterministically.

Usage
-----
    # a single "bullets" ad piece
    python helpers/copy_gen.py --format bullets \\
        --brand path/to/brand_guidelines.md \\
        --angle problem-first \\
        --hook "Buy One, Get One 40% off + $200 off your entire purchase" \\
        --cta  "Tap below to claim your deal" --json

    # N original VO scripts, one per angle (variant-factory style)
    python helpers/copy_gen.py --format script \\
        --brand brand.md --count 3 --words 63 --json

Formats
-------
    bullets  — {"bullets": [".."], "hook": "..", "cta": ".."}
    hook     — {"hook": ".."}
    script   — {"scripts": [{"angle": "..", "hook": "..", "body": "..", "cta": ".."}]}
    vo       — {"hook": "..", "cta": ".."}   (text-vo format)

Environment
-----------
    OLLAMA_HOST   default http://127.0.0.1:11434
    COPY_MODEL    default qwen3:14b   (Phi-4 14B is a good 12GB-VRAM alt)

Exit codes: 0 ok · 2 usage · 3 ollama unreachable · 4 model returned junk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

def _env(name: str, default: str) -> str:
    """env var, else video-use/.env (when installed there), else default."""
    v = os.environ.get(name)
    if v:
        return v
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


OLLAMA_HOST = _env("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
COPY_MODEL = _env("COPY_MODEL", "qwen2.5:7b")

VALID_FORMATS = {"bullets", "hook", "script", "vo"}


def die(msg: str, code: int = 2) -> None:
    sys.exit(f"copy_gen: {msg}") if code == 2 else sys.exit(code)


def fail(msg: str, code: int) -> None:
    sys.stderr.write(f"copy_gen: {msg}\n")
    sys.exit(code)


# ── Ollama call ────────────────────────────────────────────────────────────

def ollama_chat(system: str, user: str, *, model: str, temperature: float,
                fmt_json: bool = True, timeout: int = 120) -> str:
    """One-shot chat completion against the local Ollama server."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": temperature},
    }
    if fmt_json:
        payload["format"] = "json"  # constrain the model to emit valid JSON

    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        fail(f"could not reach Ollama at {OLLAMA_HOST} ({e}). "
             f"Is it running?  `ollama serve` then `ollama pull {model}`.", 3)
    except Exception as e:  # noqa: BLE001
        fail(f"ollama request failed: {e}", 3)
    return (data.get("message") or {}).get("content", "").strip()


def parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Some models wrap JSON in prose or fences — salvage the outer object.
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
        fail(f"model did not return parseable JSON. Got:\n{raw[:400]}", 4)
    return {}


# ── prompt building ─────────────────────────────────────────────────────────

SYSTEM = (
    "You are a senior direct-response copywriter for short-form video ads. "
    "You write tight, scroll-stopping, brand-compliant copy. You NEVER invent "
    "claims, prices, guarantees, or offers that are not supported by the brand "
    "guidelines provided. You always answer with a single valid JSON object and "
    "nothing else — no markdown, no commentary."
)


def read_brand(path: str | None) -> str:
    if not path:
        return "(No brand guidelines supplied — use general best practice and make NO specific claims, prices, or offers.)"
    p = Path(path)
    if not p.exists():
        die(f"brand file not found: {path}")
    return p.read_text(encoding="utf-8", errors="replace")[:12000]


def build_user(args, brand: str) -> str:
    fmt = args.format
    lines = [f"BRAND GUIDELINES:\n{brand}\n"]

    if fmt == "bullets":
        fixed = []
        if args.hook:
            fixed.append(f'Use this EXACT hook (do not change): "{args.hook}"')
        else:
            fixed.append("Write one benefit-framed hook header.")
        if args.cta:
            fixed.append(f'Use this EXACT CTA (do not change): "{args.cta}"')
        else:
            fixed.append("Write one CTA line.")
        lines.append(
            f'TASK: Write a "{args.angle}" angle ad. '
            f'Write {args.min_bullets}-{args.max_bullets} punchy selling-point '
            f'bullets, each ≤{args.max_chars} characters, brand-compliant. '
            + " ".join(fixed) +
            '\nReturn JSON: {"hook": "..", "bullets": ["..",".."], "cta": ".."}'
        )
    elif fmt == "hook":
        lines.append(
            f'TASK: Write ONE scroll-stopping "{args.angle}" hook line '
            f'(≤{args.max_words} words). Return JSON: {{"hook": ".."}}'
        )
    elif fmt == "vo":
        lines.append(
            f'TASK: Write a text-VO ad. One scroll-stopping hook (≤10 words, '
            f'read aloud) and one CTA line (read aloud). '
            f'Return JSON: {{"hook": "..", "cta": ".."}}'
        )
    elif fmt == "script":
        lines.append(
            f'TASK: Invent {args.count} ORIGINAL short VO ad scripts, each a '
            f'DIFFERENT angle (e.g. problem-first, social-proof, FOMO, '
            f'benefit-first, contrarian). Each script ≈{args.words} words '
            f'(hook + body + CTA), natural spoken pace. Brand-compliant, no '
            f'invented claims. Return JSON: '
            f'{{"scripts": [{{"angle": "..", "hook": "..", "body": "..", "cta": ".."}}]}}'
        )
    return "\n".join(lines)


# ── validation ──────────────────────────────────────────────────────────────

def validate(fmt: str, obj: dict, args) -> dict:
    if fmt == "bullets":
        b = [str(x).strip() for x in obj.get("bullets", []) if str(x).strip()]
        if not b:
            fail("model returned no bullets", 4)
        obj["bullets"] = b[: args.max_bullets]
        obj.setdefault("hook", args.hook or "")
        obj.setdefault("cta", args.cta or "")
    elif fmt == "hook":
        if not obj.get("hook"):
            fail("model returned no hook", 4)
    elif fmt == "vo":
        if not (obj.get("hook") and obj.get("cta")):
            fail("model returned incomplete vo copy", 4)
    elif fmt == "script":
        s = obj.get("scripts", [])
        if not s:
            fail("model returned no scripts", 4)
        obj["scripts"] = s[: args.count]
    return obj


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate ad copy locally via Ollama.")
    ap.add_argument("--format", required=True, choices=sorted(VALID_FORMATS))
    ap.add_argument("--brand", help="path to brand guidelines (.md/.txt)")
    ap.add_argument("--angle", default="benefit-first",
                    help="angle for bullets/hook (problem-first, social-proof, ...)")
    ap.add_argument("--hook", default="", help="exact hook to keep verbatim")
    ap.add_argument("--cta", default="", help="exact CTA to keep verbatim")
    ap.add_argument("--count", type=int, default=3, help="script count")
    ap.add_argument("--words", type=int, default=63, help="target words per script")
    ap.add_argument("--min-bullets", type=int, default=3, dest="min_bullets")
    ap.add_argument("--max-bullets", type=int, default=5, dest="max_bullets")
    ap.add_argument("--max-chars", type=int, default=42, dest="max_chars")
    ap.add_argument("--max-words", type=int, default=10, dest="max_words")
    ap.add_argument("--model", default=COPY_MODEL, help=f"Ollama model (default {COPY_MODEL})")
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--json", action="store_true", help="print JSON (default anyway)")
    args = ap.parse_args()

    brand = read_brand(args.brand)
    user = build_user(args, brand)
    raw = ollama_chat(SYSTEM, user, model=args.model, temperature=args.temperature)
    obj = validate(args.format, parse_json(raw), args)

    obj["_meta"] = {"model": args.model, "format": args.format, "engine": "ollama"}
    print(json.dumps(obj, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
