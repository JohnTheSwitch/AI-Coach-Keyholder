"""
application/review_consent_query.py

Phase J1 (Zero-Command UX): recognises a plain-language question or wish about COMMUNITY PHOTO REVIEW, e.g. "who can see my photos?",
"stop community review", "I don't want strangers to see my pictures", "kdo vidí moje fotky?", "komunitní hodnocení". The reply is the
consent overview with buttons (system/review_consent_dm.py); nothing is decided by the text itself: every decision is a
button press. Precise on purpose, like the other matchers: a missed phrasing still reaches the conversation model.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["is_review_consent_query"]

MAX_CONSENT_QUERY_CHARACTERS = 120
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_PHOTO_PREFIXES = ("photo", "picture", "pic", "image", "selfie", "fotk", "fotograf", "obrazk", "snimk")
_WHO_SEES = ("who", "kdo", "strangers", "everyone", "anyone", "public", "verej", "cizi", "vsichni", "komunit", "community")
_SEE_PREFIXES = ("see", "seeing", "sees", "view", "vid", "uvid", "kouk", "hodnot", "review", "vote", "hlasuj")


def _tokens(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf")
    return [t for t in _TOKEN_SPLIT.split(ascii_text) if t]


def is_review_consent_query(message: str) -> bool:
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_CONSENT_QUERY_CHARACTERS:
        return False
    tokens = _tokens(message)
    if not tokens:
        return False
    pairs = list(zip(tokens, tokens[1:]))
    if ("community", "review") in pairs or ("peer", "review") in pairs or ("peer", "verification") in pairs:
        return True
    if any(t.startswith("komunitn") for t in tokens) and any(t.startswith(("hodnoc", "kontrol", "overen")) for t in tokens):
        return True                                                             # komunitni hodnoceni / kontrola / overeni
    has_photo = any(t.startswith(_PHOTO_PREFIXES) for t in tokens)
    has_who = any(t.startswith(_WHO_SEES) for t in tokens)
    has_see = any(t.startswith(_SEE_PREFIXES) for t in tokens)
    return has_photo and has_who and has_see                                    # who can see my photos / kdo vidi moje fotky
