"""
visuals/style.py

Owner, 2026-11-17 ("I want all of these"): how a message looks on Discord. Pure, imports no channel package.

- `KIND_STYLE`: notification kind -> colour, icon and heading. A notice of a listed kind goes to Discord as a coloured card: the heading
  (icon + name) is the message text, so a phone notification still says what it is, and the card carries the whole text.
- `style_for(kind, text, offer)`: the EmbedSpec for one notice, or None (an unknown kind, or a text too long for a card, stays plain).
  An offer (a notice with buttons) of a task kind also gets a task card image.
- `bar(done, total)`: a text progress bar for counts the wearer already sees as numbers.

Never drawn, by design: the Trust score or level, and anything about when the release comes (philosophy 2.6, release_engine/README.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from application.models import EmbedSpec

__all__ = ["COLORS", "KIND_STYLE", "Style", "style_for", "bar", "heading_for", "TASK_KINDS"]

# Colour names an EmbedSpec may carry; the Discord adapter maps them (unknown names fall back to Discord's default).
COLORS: dict[str, int] = {
    "green": 0x2ECC71, "red": 0xE74C3C, "blue": 0x3498DB, "purple": 0x9B59B6, "grey": 0x95A5A6, "gold": 0xF1C40F, "orange": 0xE67E22,
    # Owner, 2026-11-23 (a more feminine keyholder): her own colours.
    "rose": 0xE84393, "lilac": 0xB39DDB, "blush": 0xF8A5C2,
}


@dataclass(frozen=True)
class Style:
    color: str
    icon: str
    title: str


KIND_STYLE: dict[str, Style] = {
    # erotic and obedience
    "obedience_task": Style("rose", "\U0001F5DD️", "Obedience task"),          # owner, 2026-11-23: her colours (rose, lilac, blush)
    "temptation": Style("rose", "\U0001F48B", "Temptation"),
    "protocol": Style("rose", "\U0001F4DC", "Protocol"),
    # tasks and training
    "task_board": Style("gold", "\U0001F4CB", "Task board"),
    "training": Style("orange", "\U0001F3CB️", "Training"),
    "recovery_task": Style("gold", "\U0001F9F9", "Recovery task"),
    "lines": Style("gold", "✍️", "Lines"),
    "custom_tasks": Style("gold", "\U0001F4DD", "Your tasks"),
    # check-ins and routine
    "check_in": Style("lilac", "\U0001F514", "Check-in"),
    "commitment_ask": Style("lilac", "\U0001F91D", "Commitment"),
    "weekly_review": Style("lilac", "\U0001F4C5", "Your week"),
    "health_pattern": Style("blue", "\U0001FA7A", "Health"),
    "settings_change": Style("grey", "⚙️", "Settings"),
    # problems
    "check_in_warning": Style("red", "⚠️", "Warning"),
    "checkin_missed": Style("red", "⚠️", "Missed check-in"),
    "missed": Style("red", "⚠️", "Missed"),
    "insufficient": Style("red", "⚠️", "Not enough"),
    "infraction_wearer": Style("red", "❓", "What happened?"),
    "penalty_warning": Style("red", "⚠️", "Warning"),
    "penalty_window": Style("red", "⛓️", "Penalty Window"),
    "exemption_overdue": Style("red", "⏰", "Overdue"),
    # exemptions and openings
    "exemption_decision": Style("grey", "\U0001F4E8", "Exemption"),
    "exemption_opening": Style("grey", "\U0001F513", "Opening"),
    "exemption_ended": Style("grey", "\U0001F512", "Exemption ended"),
    "hygiene": Style("grey", "\U0001F6BF", "Hygiene opening"),
    "hygiene_opening": Style("grey", "\U0001F6BF", "Hygiene opening"),
    "hygiene_health": Style("grey", "\U0001FA7A", "Health opening"),
    # rewards and release
    "token": Style("green", "\U0001FA99", "Token"),
    "release": Style("green", "\U0001F511", "Release"),
    "release_window": Style("green", "\U0001F511", "Release window"),
    "graduation": Style("green", "\U0001F393", "Supervision"),
    "proof_check": Style("lilac", "\U0001F4F7", "Spot check"),
    "peer_review": Style("lilac", "\U0001F465", "Photo review"),
}

# An offer of these (a notice with buttons) also gets a task card image.
TASK_KINDS = frozenset({"obedience_task", "temptation", "task_board", "recovery_task", "training"})

MAX_CARD_TEXT = 4000           # Discord's embed description limit is 4096
_DECLINED = re.compile(r"^\s*(declined|not passed|rejected|failed)\b", re.IGNORECASE)
_GOOD = re.compile(r"^\s*(approved|verified|your task was verified|board task verified|accepted|good)\b", re.IGNORECASE)
_BOLD = re.compile(r"\*\*([^*]{4,}?)\*\*")          # "**1.**" is a list number, not a name
_REWARD = re.compile(r"\+\d+(?:\.\d+)? toward your next token", re.IGNORECASE)


def heading_for(kind: str) -> str | None:
    style = KIND_STYLE.get(kind)
    return None if style is None else f"{style.icon} {style.title}"


def _color(style: Style, text: str) -> str:
    """A decided outcome shows as what it is, whatever its kind: declined red, approved or verified green."""
    if _DECLINED.search(text):
        return "red"
    if _GOOD.search(text):
        return "green"
    return style.color


def _headline(text: str) -> str:
    """The task's name for its card: the first bold part, else the line after an introduction ending in ':', else the first sentence."""
    bold = _BOLD.search(text)
    if bold:
        first = bold.group(1)
    else:
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        body = lines[1] if len(lines) > 1 and lines[0].endswith(":") else (lines[0] if lines else "")
        first = re.split(r"(?<=[.!?])\s", body, maxsplit=1)[0]
    first = first.replace("**", "").strip()
    return first if len(first) <= 90 else first[:87].rstrip() + "..."


def task_card(kind: str, text: str) -> dict:
    style = KIND_STYLE[kind]
    rewards = _REWARD.findall(text)
    if kind == "task_board":                  # a list of tasks: the board is the headline, and it has more than one reward
        return {"type": "task", "kind": kind, "label": style.title.upper(), "color": style.color, "headline": "This week's task board",
                "note": f"{len(rewards)} tasks, each with a reward" if len(rewards) > 1 else ""}
    return {"type": "task", "kind": kind, "label": style.title.upper(), "color": style.color, "headline": _headline(text),
            "note": rewards[0] if len(rewards) == 1 else ""}


def style_for(kind: str, text: str, *, offer: bool = False) -> EmbedSpec | None:
    style = KIND_STYLE.get(kind)
    if style is None or not isinstance(text, str) or not text.strip() or len(text) > MAX_CARD_TEXT:
        return None
    card = task_card(kind, text) if offer and kind in TASK_KINDS else None
    return EmbedSpec(title=f"{style.icon} {style.title}", description=text, color=_color(style, text), card=card)


def bar(done: float, total: float, width: int = 10) -> str:
    """'▰▰▰▱▱▱▱▱▱▱ 3/10'. A total of 0 or less draws an empty bar."""
    if total <= 0:
        return "▱" * width
    filled = max(0, min(width, round(width * done / total)))
    return "▰" * filled + "▱" * (width - filled)
