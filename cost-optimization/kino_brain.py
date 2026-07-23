"""Kino Brain — a growing, per-team memory Kino reads before writing ads.

The model doesn't retrain; instead it consults an accumulating knowledge store each
run, so output improves as you feed it. Three inputs, one file store per team:

  notes.md       freeform lessons/rules you write ("question-hooks beat statements",
                 "never say 'affordable'", "BOGO converts best in winter").
  results.jsonl  one record per ad you ran: hook, angle, script, your rating/verdict,
                 and (optional) real metrics (CTR/ROAS/spend). Append-only.
  winners/       (optional) full winning script files, if you'd rather drop files.

`compile` turns all of that into a compact, size-bounded briefing block that gets
injected into the Kino prompt: PROVEN WINNERS (top performers to emulate), AVOID
(documented duds), and YOUR NOTES. Bounded size keeps prompt cost flat as the brain
grows; phase 3 can swap the top-N ranking for semantic retrieval.

All three feedback signals share one record shape:
  - "my own notes/ratings"  -> rating / verdict / note fields
  - "real ad metrics"       -> metrics{} field (fed by the Meta Ads connector later)
  - "just winning scripts"  -> a record with a high rating + the script text

Usage
-----
  # log a result after a run (rating and/or metrics, both optional)
  python kino_brain.py add-result --brain BRAIN/ --team windows \\
      --angle problem-first --hook "Your AC is fine, your windows aren't." \\
      --rating 5 --metric ctr=2.4 --metric roas=3.1 --note "contrast hook, strong"

  # jot a freeform lesson
  python kino_brain.py add-note --brain BRAIN/ --text "Question hooks beat statements."

  # build the briefing the Kino prompt injects
  python kino_brain.py compile --brain BRAIN/ --json

Store lives per team, e.g.  <asset_root>/<team>/kino_brain/.
Pure stdlib; no deps.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _brain(dirpath: str) -> Path:
    p = Path(dirpath)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _results_file(brain: Path) -> Path:
    return brain / "results.jsonl"


def _notes_file(brain: Path) -> Path:
    return brain / "notes.md"


def _load_results(brain: Path) -> list[dict]:
    f = _results_file(brain)
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_add_result(args) -> None:
    brain = _brain(args.brain)
    metrics = {}
    for m in args.metric or []:
        if "=" in m:
            k, v = m.split("=", 1)
            try:
                metrics[k.strip()] = float(v)
            except ValueError:
                metrics[k.strip()] = v.strip()
    rec = {
        "date": _now(),
        "team": args.team or "",
        "angle": args.angle or "",
        "hook": args.hook or "",
        "script": args.script or "",
        "rating": args.rating,          # 1-5 or None
        "verdict": args.verdict or "",  # winner | dud | neutral | ""
        "metrics": metrics,             # {} unless provided
        "note": args.note or "",
    }
    with _results_file(brain).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps({"ok": True, "added": rec}, ensure_ascii=False))


def cmd_add_note(args) -> None:
    brain = _brain(args.brain)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _notes_file(brain).open("a", encoding="utf-8") as f:
        f.write(f"- ({stamp}) {args.text.strip()}\n")
    print(json.dumps({"ok": True}, ensure_ascii=False))


def _score(rec: dict) -> float:
    """Rank results: prefer real metrics, else rating, else verdict."""
    m = rec.get("metrics") or {}
    if "roas" in m and isinstance(m["roas"], (int, float)):
        return 100 + float(m["roas"])          # ROAS dominates when present
    if "ctr" in m and isinstance(m["ctr"], (int, float)):
        return 50 + float(m["ctr"])
    if isinstance(rec.get("rating"), (int, float)):
        return float(rec["rating"])
    v = (rec.get("verdict") or "").lower()
    return {"winner": 4.5, "neutral": 3.0, "dud": 1.0}.get(v, 3.0)


def cmd_compile(args) -> None:
    brain = _brain(args.brain)
    results = _load_results(brain)
    notes = ""
    if _notes_file(brain).exists():
        notes = _notes_file(brain).read_text(encoding="utf-8").strip()

    ranked = sorted(results, key=_score, reverse=True)
    winners = [r for r in ranked if _score(r) >= 4.0][: args.max_winners]
    duds = [r for r in ranked if _score(r) <= 2.0][-args.max_duds:]

    def _fmt_win(r: dict) -> str:
        bits = []
        if r.get("hook"):   bits.append(f'hook: "{r["hook"].strip()}"')
        if r.get("angle"):  bits.append(f'angle: {r["angle"]}')
        m = r.get("metrics") or {}
        if m:               bits.append("metrics: " + ", ".join(f"{k}={v}" for k, v in m.items()))
        elif r.get("rating") is not None: bits.append(f'rating: {r["rating"]}/5')
        if r.get("note"):   bits.append(f'why: {r["note"].strip()}')
        return "  - " + " | ".join(bits)

    lines = ["━━━ KINO BRAIN — learn from what has worked for THIS team ━━━"]
    if winners:
        lines.append("\nPROVEN WINNERS (emulate the hook style, angle, and rhythm — do NOT copy verbatim):")
        lines += [_fmt_win(r) for r in winners]
    if duds:
        lines.append("\nAVOID (these underperformed):")
        lines += [f'  - "{(r.get("hook") or "").strip()}"'
                  + (f' ({r["note"].strip()})' if r.get("note") else "") for r in duds]
    if notes:
        note_block = notes[: args.max_notes_chars]
        lines.append("\nYOUR NOTES / RULES (hard guidance):\n" + note_block)
    if not (winners or duds or notes):
        lines.append("\n(No brain entries yet — write brand-safe copy and start logging results to teach Kino.)")

    briefing = "\n".join(lines)
    if args.json:
        print(json.dumps({
            "briefing": briefing,
            "counts": {"results": len(results), "winners": len(winners),
                       "duds": len(duds), "has_notes": bool(notes)},
        }, ensure_ascii=False, indent=2))
    else:
        print(briefing)


def main() -> None:
    ap = argparse.ArgumentParser(description="Kino Brain — per-team learning memory.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-result", help="log an ad's result")
    a.add_argument("--brain", required=True)
    a.add_argument("--team", default="")
    a.add_argument("--angle", default="")
    a.add_argument("--hook", default="")
    a.add_argument("--script", default="")
    a.add_argument("--rating", type=float, default=None, help="1-5")
    a.add_argument("--verdict", default="", choices=["", "winner", "neutral", "dud"])
    a.add_argument("--metric", action="append", help="key=value, e.g. ctr=2.4 (repeatable)")
    a.add_argument("--note", default="")
    a.set_defaults(func=cmd_add_result)

    n = sub.add_parser("add-note", help="append a freeform lesson")
    n.add_argument("--brain", required=True)
    n.add_argument("--text", required=True)
    n.set_defaults(func=cmd_add_note)

    c = sub.add_parser("compile", help="build the Kino briefing block")
    c.add_argument("--brain", required=True)
    c.add_argument("--max-winners", type=int, default=6)
    c.add_argument("--max-duds", type=int, default=4)
    c.add_argument("--max-notes-chars", type=int, default=2000)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_compile)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
