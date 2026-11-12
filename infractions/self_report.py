"""
infractions/self_report.py

Owner, 2026-11-12 (option A): when the wearer owns up by themselves, it becomes a finding marked as self-disclosed. Live, the wearer wrote that they had
edged during a hygiene opening, and nothing happened.

`is_hygiene_confession` is deterministic, never a model's reading, and deliberately broad: a match only puts a question card under the reply
("Did you just tell me ...?"), and only the wearer's own "Yes" decides anything (`InfractionSignals.offer_self_report`). A false match costs one "No".
English and Czech (diacritics folded).
"""

from __future__ import annotations

import re

from infrastructure.text import plain_text

__all__ = ["is_hygiene_confession"]

# The opening: hygiene, the shower, being opened / unlocked for it.
_OPENING = re.compile(
    r"\b(hygien\w*|shower\w*|bath\w*|opening|unlocked|opened|open|sprch\w*|sprs\w*|vana|vane|koupel\w*|odemcen\w*|otevren\w*|otevrel\w*)\b", re.IGNORECASE)
# What it was used for instead.
_ACT = re.compile(
    r"\b(edg(e|ed|es|ing)|cum(med)?|came|orgasm\w*|masturbat\w*|jerk(ed|ing)? (myself )?off|wank\w*|strok(e|ed|ing)( myself| it)?|"
    r"touch(ed|ing)? (myself|my (cock|dick|penis))|play(ed|ing)? with (myself|it)|rubb(ed|ing) (myself|one out)|"
    r"honil|vyhonil|zahonil|udelal jsem se|vystrikl|semenil|hral jsem si|dotykal jsem se)\b", re.IGNORECASE)


def is_hygiene_confession(message: str) -> bool:
    if not isinstance(message, str) or not message.strip() or len(message) > 1000:
        return False
    text = plain_text(message)
    return _OPENING.search(text) is not None and _ACT.search(text) is not None
