"""
coach/safety.py

The deterministic content check on everything the model proposes (v0.39 Phase J3). A goal or commitment is about the user's own development
(philosophy 2.10). It may never touch the regime, and it may never ask for something that could hurt the user. Plain word lists, English and
Czech, diacritics ignored. A false refusal costs one card; the conversation simply carries on.
"""

from __future__ import annotations

import re
import unicodedata

from supervision.checkin_evaluator import mentions_safety_issue

__all__ = ["content_refusal", "fold"]

_REGIME = re.compile(
    r"\b(un)?lock(s|ed|ing)?\b|\bkeyholders?\b|\bchaster\b|\bchastity\b|\bcage\w*\b|\bfr[eo]ez\w*\b|\bfrozen\b|\bunfreeze\w*\b|\brelease\w*\b|"
    r"\bextension\w*\b|\bextend\w*\b|\btokens?\b|\btrust\w*\b|\bscores?\b|\bpenalt\w*\b|\bpunish\w*\b|\bincidents?\b|\brules?\b|\bconsent\w*\b|"
    r"\bverification\w*\b|\bhygiene\b|\bemergency\b|\bsafeword\w*\b|\boverride\w*\b|\bcheck-?ins?\b|"
    r"\bzam[ck]\w*\b|\bzamek\b|\bodem[ck]\w*\b|\bklic\w*\b|\bzmraz\w*\b|\brozmraz\w*\b|\buvolnen\w*\b|\btrest\w*\b|\bduver\w*\b|\bpravidl\w*\b|"
    r"\bsouhlas\w*\b|\bnouz\w*\b",
    re.IGNORECASE,
)
_RISK = re.compile(
    r"\bstarv\w*\b|\bfast(s|ing)\b|\bskip\w*\s+(meals?|food|eating|sleep)\b|\b(no|without)\s+(sleep\w*|food|water|eating|drinking|rest)\b|"
    r"\ball[- ]night\b|\bsleep\s+deprivation\b|\blaxative\w*\b|\bpurg\w*\b|\boverdose\w*\b|\bpills?\b|\bdrugs?\b|\balcohol\b|"
    r"\bhladov\w*\b|\bbez\s+(spanku|jidla|vody|piti)\b|\bnespat\b|\bprasky?\b|\bprasek\b|\bdrog\w*\b|\balkohol\w*\b",
    re.IGNORECASE,
)


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def content_refusal(texts: tuple[str, ...]) -> str | None:
    """A reason code, or None when the text may be shown to the user as a proposal."""
    for text in texts:
        folded = fold(text)
        if _REGIME.search(folded):
            return "regime_topic"
        if _RISK.search(folded) or mentions_safety_issue(text):
            return "unsafe_content"
    return None
