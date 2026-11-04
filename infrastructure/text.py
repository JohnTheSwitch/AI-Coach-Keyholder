"""
infrastructure/text.py

Normalising what people actually type, shared by every deterministic matcher. Keyboards differ: a Czech layout gives `´` for an apostrophe, phones give `’`, and some people use a backtick.
`´` needs care because Unicode NFKD turns it into a space plus a combining mark, so "I´ve" silently becomes "i ve" and no pattern matches.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["plain_text"]

_APOSTROPHES = re.compile("[´`’‘ʼʹ′]")


def plain_text(text: str) -> str:
    """Lower case, one kind of apostrophe, no diacritics: what a matcher should look at. Apostrophes go first, for the reason above."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", _APOSTROPHES.sub("'", text).lower()) if not unicodedata.combining(ch))
