"""Convert copied Slack notes into brief files under notes/.

Today the tool ingests DiH's Slack notes via the folder route (paste in the UI,
or drop files in notes/). This is a convenience for when you copy several Slack
messages at once: paste them into a text file separated by a line of `---`, and
each block becomes its own brief.

    python slack_import.py --from pasted.txt
    type pasted.txt | python slack_import.py -

Going live later: once the DiH Slack connector is authorized (you're a guest in
their workspace), set `slack.mode` = "connector" and `slack.channel` in
config.json. The ingestion point is collect_briefs()/notes/ — a connector pull
just needs to write the same brief files here, so nothing downstream changes.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import common


def split_blocks(text: str) -> list[str]:
    raw = [b.strip() for b in text.replace("\r\n", "\n").split("\n---\n")]
    return [b for b in raw if b]


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Import Slack notes into notes/.")
    ap.add_argument("--from", dest="src", default="-",
                    help="text file of notes (blocks split by a line '---'), or '-' for stdin")
    args = ap.parse_args()

    text = sys.stdin.read() if args.src == "-" else Path(args.src).read_text(encoding="utf-8")
    blocks = split_blocks(text)
    if not blocks:
        common.eprint("no note blocks found")
        return 1

    cfg = common.load_config()
    ndir = common.notes_dir(cfg)
    ndir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i, block in enumerate(blocks, 1):
        first = block.splitlines()[0][:40]
        name = f"{stamp}-{i:02d}-{common.slugify(first)}.md"
        (ndir / name).write_text(block, encoding="utf-8")
        print(f"wrote notes/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
