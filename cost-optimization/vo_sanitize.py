"""Deterministic text scrubber for ElevenLabs VO — prevents garbled/"non-human"
audio caused by unspeakable tokens (®, ™, em-dashes, smart quotes, emoji, symbols).

Why this exists
---------------
The variant that came out garbled had this in its TTS text:
    "...Renewal by Andersen's Fibrex® windows... forty percent off — plus no..."
ElevenLabs slurs/glitches on ® and em-dashes. The Kino prompt asks the model to
scrub these, but that's a SOFT instruction — Opus tended to honor it, Sonnet let
them through. This makes the scrub DETERMINISTIC and model-independent: run every
VO string through sanitize_for_tts() right before the API call and the problem
can't recur regardless of which model wrote the copy.

Wire-in (Claude Code — video-use/ is edit-restricted, so this is staged here):
    In video-use/helpers/tts_voice.py, inside synthesize(), before building the
    payload (the `"text": text` line), add:
        from vo_sanitize import sanitize_for_tts   # same helpers/ dir
        text = sanitize_for_tts(text)
    That covers variants, streamlined ads, and every other VO path in one spot.

Design notes
------------
- Conservative: it does NOT rewrite words, only removes/normalizes characters that
  TTS mishandles. Copy that's already clean passes through unchanged.
- Preserves sentence punctuation (. , ? ! : ; ) that drives TTS pacing.
- Em/en dashes -> comma (a natural spoken pause), matching how a person reads them.
"""

from __future__ import annotations

import re
import unicodedata

# Symbols to delete outright (spoken as nothing).
_DROP = {
    "®",  # ®
    "™",  # ™
    "©",  # ©
    "℠",  # ℠
    "​", "﻿",  # zero-width / BOM
    "*",       # asterisks (disclaimer markers) shouldn't be spoken
    "#",       # hashes
    "_", "~", "^", "|", "`",
}

# Character -> spoken-friendly replacement.
_REPLACE = {
    "—": ", ",   # — em dash  -> comma pause
    "–": ", ",   # – en dash  -> comma pause
    "―": ", ",   # ― horizontal bar
    "…": ". ",   # … ellipsis -> sentence stop
    "‘": "'", "’": "'",          # ' '  smart single quotes
    "“": '"', "”": '"',          # " "  smart double quotes
    "′": "'", "″": '"',          # prime marks
    " ": " ",                          # non-breaking space
    "&": " and ",
    "%": " percent",
    "+": " plus ",
    "=": " equals ",
    "½": " one half", "¼": " one quarter", "¾": " three quarters",
}


def sanitize_for_tts(text: str) -> str:
    """Return `text` made safe for ElevenLabs: unspeakable symbols removed or
    converted, unicode normalized to ASCII, whitespace/punctuation tidied."""
    if not text:
        return ""

    # 1) explicit replacements (before normalization so we control the mapping)
    for src, dst in _REPLACE.items():
        text = text.replace(src, dst)

    # 2) drop the delete-set characters
    text = "".join("" if ch in _DROP else ch for ch in text)

    # 3) normalize accents to ASCII (café -> cafe) and strip any remaining
    #    non-ASCII (emoji, exotic symbols) that would confuse TTS.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")

    # 4) tidy whitespace and spacing around punctuation
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)   # no space before punctuation
    text = re.sub(r"([,.!?;:])(?=[^\s])", r"\1 ", text)  # ensure space after
    text = re.sub(r",\s*,", ",", text)             # collapse doubled commas
    text = re.sub(r"\s{2,}", " ", text)

    return text.strip()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(sanitize_for_tts(" ".join(sys.argv[1:])))
    else:  # tiny self-test
        cases = [
            ("Renewal by Andersen's Fibrex® windows — seal tight.",
             "Renewal by Andersen's Fibrex windows, seal tight."),
            ("Buy one, get one 40% off — plus no money down…",
             "Buy one, get one 40 percent off, plus no money down."),
            ("Save $200 & more #deal “now”",
             'Save $200 and more deal "now"'),
            ("clean copy stays the same.", "clean copy stays the same."),
        ]
        ok = True
        for src, want in cases:
            got = sanitize_for_tts(src)
            flag = "OK " if got == want else "FAIL"
            if got != want:
                ok = False
            print(f"[{flag}] {src!r}\n       -> {got!r}")
        print("\nALL PASS" if ok else "\nSOME FAILED")
