"""
proof_checks/contradictions.py

Contradictions (owner, 2026-11-13; honesty and proof, slice 4; migration 064). A fact that contradicts a report becomes a QUESTION to the wearer
(the infractions path: explain, health clears it in code, the judge decides, one appeal), never an automatic verdict. Unexplained, it is a false
report in the Honesty domain: Trust and a Warning, never a Penalty Window (`FALSE_REPORT`).

First kind, bedtime: a bedtime commitment that names a time ("Sleep by 23:00", "in bed before 11 pm") reported as kept, while the wearer wrote to
the bot more than BEDTIME_GRACE after that time and before quiet hours ended. Looked at once, after the night is over. A bedtime without a
stated time ("go to bed 30 minutes earlier") cannot be contradicted and is only recorded as `no_time`. A message about health, pain or an
emergency never counts. Only the wearer's own messages count, never the bot's.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.time_format import iso as _iso
from supervision.checkin_evaluator import mentions_safety_issue

logger = logging.getLogger("ai_coach_keyholder.proof_checks.contradictions")

__all__ = ["BedtimeContradictions", "stated_bedtime", "BEDTIME_GRACE", "LOOKBACK_DAYS"]

BEDTIME_GRACE = timedelta(minutes=15)
LOOKBACK_DAYS = 3
_HHMM = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b")
_HOUR = re.compile(r"\b(?:by|before|at|until|till|do|v|nejpozdeji)\s+(\d{1,2})\s*(am|pm|h|hod\w*)?\b")
_AMPM = re.compile(r"\b(\d{1,2})\s*(am|pm)\b")


def stated_bedtime(text: str) -> tuple[int, int] | None:
    """(hour, minute) of the bedtime a commitment names, on a 24-hour clock with evening hours preferred, or None."""
    lowered = text.lower()
    for pattern in (_HHMM, _AMPM, _HOUR):
        match = pattern.search(lowered)
        if match is None:
            continue
        hour = int(match.group(1))
        minute = int(match.group(2)) if pattern is _HHMM else 0
        suffix = match.group(len(match.groups()))
        if hour > 23 or minute > 59:
            return None
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        elif suffix not in ("am", "pm") and 6 <= hour <= 11:
            hour += 12                                        # "by 11" or "11:30" for a bedtime means the evening
        return hour, minute
    return None


class BedtimeContradictions:
    def __init__(self, core: CoreDatabase, *, user_id: str, honesty_domain: str, kept_night: Callable[[date], list[dict]],
                 messages_between: Callable[[datetime, datetime], list[tuple[datetime, str]]], to_utc: Callable[[date, time], datetime],
                 morning_after: Callable[[date], datetime | None], today: Callable[[datetime], date], local_clock: Callable[[datetime], str],
                 raise_question: Callable[..., bool]) -> None:
        self._core = core
        self.user_id = user_id
        self._domain = honesty_domain
        self._kept_night = kept_night
        self._messages = messages_between
        self._to_utc = to_utc
        self._morning_after = morning_after
        self._today = today
        self._clock = local_clock
        self._raise = raise_question

    def run(self, now: datetime) -> int:
        """Looks at every kept bedtime commitment whose night is over and that was not looked at yet. Returns how many questions were raised."""
        since = self._today(now) - timedelta(days=LOOKBACK_DAYS)
        with self._core.transaction() as tx:
            done = {r["commitment_id"] for r in tx.fetch_all("SELECT commitment_id FROM contradiction_checks WHERE user_id = ?", (self.user_id,))}
        raised = 0
        for row in self._kept_night(since):
            if row["id"] in done:
                continue
            try:
                raised += self._check(row, now)
            except Exception:
                logger.exception("A bedtime contradiction check failed; it is retried on the next pass.")
        return raised

    def _check(self, row: dict, now: datetime) -> int:
        day = date.fromisoformat(row["for_date"])
        morning = self._morning_after(day)
        if morning is None or now < morning:
            return 0                                                  # the night is not over yet
        bedtime = stated_bedtime(row["text"])
        if bedtime is None:
            self._record(row["id"], "no_time", None, now)
            return 0
        hour, minute = bedtime
        at = self._to_utc(day if hour >= 12 else day + timedelta(days=1), time(hour, minute))
        late = [(t, text) for t, text in self._messages(at + BEDTIME_GRACE, morning) if not mentions_safety_issue(text)]
        if not late:
            self._record(row["id"], "none", None, now)
            return 0
        first = late[0][0]
        stated = f"{hour:02d}:{minute:02d}"
        asked = self._raise(subject_id=row["id"], now=now,
                            detail=f"A kept bedtime commitment is contradicted: the wearer wrote to the bot at {self._clock(first)}, after {stated}.",
                            wearer_text=(f"You marked \"{row['text']}\" as kept, but you wrote to me at {self._clock(first)} that night, after "
                                         f"{stated}. What happened?"),
                            facts=f"Commitment: {row['text']} ({row['for_date']}). Reported kept. {len(late)} message(s) after {stated}, the first at "
                                  f"{self._clock(first)}.", window=(at, morning))
        self._record(row["id"], "found", first, now)
        decision("proof_checks", "A kept bedtime commitment is contradicted by messages that night: the wearer is asked.", commitment_id=row["id"],
                 messages=len(late))
        return 1 if asked else 0

    def _record(self, commitment_id: str, outcome: str, message_at: datetime | None, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT OR IGNORE INTO contradiction_checks (commitment_id, user_id, checked_at, outcome, message_at) VALUES (?, ?, ?, ?, ?)",
                       (commitment_id, self.user_id, _iso(now), outcome, _iso(message_at) if message_at else None))

        apply_transition(self._core, write=write)
