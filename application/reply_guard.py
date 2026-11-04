"""
application/reply_guard.py

Fixed rules for what a model's reply may and must contain, applied after the model and independent of it. Both came from reviewing real use:

- A plea to be released got weak, improvised answers, and none named the way out that exists. That cannot depend on how a model feels today, so a fixed line is added (philosophy 2.3).
- A model once printed part of its own prompt, personality numbers included. A reply that shows such a marker is replaced rather than edited.
"""

from __future__ import annotations

import re
import unicodedata

from supervision.checkin_evaluator import mentions_safety_issue

__all__ = ["finish_reply", "needs_care_line", "reveals_internals", "CARE_LINE", "REFUSED_REPLY"]

CARE_LINE = ("If this is about your health or safety, that comes first and you never need my permission: use your emergency option. "
             "If life needs a break from the device, you can ask me for an exemption.")
REFUSED_REPLY = "That's not something I can share. What's on your mind?"
MAX_REPLY = 1950                                    # a Discord message holds 2000 characters

_PLEA = re.compile(
    r"\b(unlock me|let me out|get me out|take it off|i (just )?(can'?t|cannot) (take|do|stand|handle|bear) (it|this|that)|i (just )?(can'?t|cannot) anymore"
    r"|odemkni|pust me|nevydrzim|nezvladam|uz nemuzu|uz to nedam)\b")
_INTERNAL_MARKERS = ("authoritative application state", "system context (verified", "verified chaster data", "lock details:", "personality / present")
_INTERNAL_SETTINGS = re.compile(r"\b(warmth|assertiveness|teasing|verbosity|formality)\s*[:=]?\s*[01]\.\d")


def _fold(text: str) -> str:
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(ch))
    return plain.replace("’", "'")


def needs_care_line(user_message: str) -> bool:
    return isinstance(user_message, str) and (_PLEA.search(_fold(user_message)) is not None or mentions_safety_issue(user_message))


def reveals_internals(reply: str) -> bool:
    folded = reply.lower()
    return any(marker in folded for marker in _INTERNAL_MARKERS) or _INTERNAL_SETTINGS.search(folded) is not None


def finish_reply(reply: str, user_message: str) -> str:
    text = REFUSED_REPLY if reveals_internals(reply) else reply
    if not needs_care_line(user_message):
        return text
    room = MAX_REPLY - len(CARE_LINE) - 2
    if len(text) > room:
        text = text[:max(room - 1, 0)].rsplit(" ", 1)[0].rstrip() + "…"
    return f"{text}\n\n{CARE_LINE}"
