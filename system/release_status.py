"""
system/release_status.py

"What is holding my release?" answered from facts (owner, 2026-11-16, Proposal A). At 16:04 that day the model, asked "what is holding my
lock" and "am I under supervision?", invented reasons. A whole question of that kind is now answered here, before any model, from the
release engine's own hard vetoes, the token balance and the supervision state:

- each hold that is on, in plain words, with what ends it (supervision: about when, if every check-in is answered; a rough guide, never a
  promise and never a score);
- whether a token is ready.

Read-only: it changes nothing and never acts on a lock. Composition layer only (no state of its own).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta
from typing import Callable

from application.models import OutgoingMessage
from infrastructure.text import plain_text

logger = logging.getLogger("ai_coach_keyholder.release_status")

__all__ = ["ReleaseStatus", "is_release_question", "ANSWER_HOURS"]

# Local hours at which the estimate assumes an answered check-in (three in Supervision Mode plus the morning report).
ANSWER_HOURS = (time(10, 0), time(13, 0), time(16, 0), time(19, 0))
_DAYS_AHEAD = 14

# Live, 2026-11-16 19:32: "When will I get unlocked?" and "Ok, why am I still locked?" were missed (no "get unlocked", a leading "Ok,").
_FILLER = re.compile(r"^((ok|okay|so|and|but|hey|well|mistress|please|then|alright|um|hmm)\b[\s,.!?]*)+")
_QUESTION = re.compile(
    r"(what(s| is) (holding|keeping|blocking|stopping) (me|my|the) (release|lock|unlock|from being (released|unlocked))( back)?"
    r"|why (am i|i am|arent i|am i not) (not )?(unlocked|released|still locked|locked)( still)?( yet)?"
    r"|why (cant|can not|cannot|dont|do not) i (get|have|be) (a |my )?(release|released|unlocked)( yet)?"
    r"|when (will|can|do|am) (you|i) (unlock me|release me|let me out|be (unlocked|released)|get(ting)? (a |my )?(release|released|unlocked|out))( please)?"
    r"|(will|can) i (get|be) (unlocked|released)( soon| today| tomorrow)?"
    r"|am i (under|in) supervision( mode)?|is supervision (on|active)|when (does|will) (the )?supervision end"
    r"|co (mi )?(drzi|blokuje) (uvolneni|odemceni)|kdy (me )?(odemknes|pustis)|jsem pod dohledem)"
)
_EDGES = re.compile(r"^[\s,.!?]+|[\s,.!?]+$")


def is_release_question(message: str) -> bool:
    if not isinstance(message, str) or len(message) > 120:
        return False
    text = re.sub(r"\s+", " ", _EDGES.sub("", plain_text(message).replace("'", "").replace("’", "")))
    text = _EDGES.sub("", _FILLER.sub("", text))
    return bool(_QUESTION.fullmatch(text))


class ReleaseStatus:
    def __init__(self, *, user_id: str, holds: Callable[[], tuple[list[str], int]], supervision_domains: Callable[[], list[str]],
                 supervision_end: Callable[[str, datetime, list[datetime]], datetime | None], window_config) -> None:
        self._user = user_id
        self._holds = holds
        self._domains = supervision_domains
        self._end = supervision_end
        self._cfg = window_config

    def answer(self, user_id: str, text: str, now: datetime) -> OutgoingMessage | None:
        if user_id != self._user or not is_release_question(text):
            return None
        held, tokens = self._holds()
        lines: list[str] = []
        if "supervision_active" in held:
            lines.append("- **Supervision:** your recent check-in record is still too weak. " + self._supervision_line(now))
        if "penalty_window_active" in held:
            lines.append("- **A penalty window** is running. It ends on its own when its time is up.")
        if "release_debt" in held:
            lines.append("- **Lines you owe:** write them first; then they stop holding it.")
        token = ("You have a token ready." if tokens == 1 else f"You have {tokens} tokens ready." if tokens > 1
                 else "You don't have a token yet; it builds with clean days.")
        if not lines:
            body = ("Nothing holds your release right now. " + token +
                    (" You still earn it on the day: the day's training or your release lines first." if tokens else ""))
            return OutgoingMessage(text=body)
        return OutgoingMessage(text="What holds your release right now:\n" + "\n".join(lines) + "\n\n" + token)

    def _supervision_line(self, now: datetime) -> str:
        try:
            ends = [e for e in (self._end(d, now, self._future(now)) for d in self._domains()) if e is not None]
        except Exception:
            logger.exception("The supervision estimate could not be read; the answer leaves it out.")
            ends = []
        if len(ends) != len(self._domains()) or not ends:
            return "It ends once most of your recent check-ins are answered; every miss pushes it back."
        when = self._cfg.local(max(ends))
        return (f"If you answer every check-in, it should end about **{when.strftime('%A')} {self._part(when)}** "
                f"({when.strftime('%d %b').lstrip('0')}). Every miss pushes it back.")

    def _future(self, now: datetime) -> list[datetime]:
        today = self._cfg.local(now).date()
        return [self._cfg.to_utc(today + timedelta(days=k), t) for k in range(_DAYS_AHEAD) for t in ANSWER_HOURS]

    @staticmethod
    def _part(local: datetime) -> str:
        return "morning" if local.hour < 12 else "afternoon" if local.hour < 17 else "evening" if local.hour < 23 else "night"
