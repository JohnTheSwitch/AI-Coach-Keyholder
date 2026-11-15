"""
system/health_pattern.py

Gap fix G2 (owner, 2026-11-15): health uses are counted, never punished by themselves (philosophy 3.9, v1.17: frequency may be counted, shown
in the weekly review and lead to a question, never by itself evidence). A health use is any of:

  - a health hygiene opening ("It's a health need"),
  - a health exemption (category health, or approved by code as a health need),
  - the daily obedience task's "I can't now (health or safety)" button,
  - a detector question cleared because the answer named a health or safety need.

From HEALTH_ASK_FROM uses in the last 7 days, the keyholder asks once a week whether something is going on. The answer is free and is never
judged; nothing is restricted. Read-only over the owning modules' getters; writes only a notification.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.time_format import iso as _iso
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.system.health_pattern")

__all__ = ["HealthPattern", "health_uses", "HEALTH_ASK_FROM", "WINDOW"]

HEALTH_ASK_FROM = 3
WINDOW = timedelta(days=7)
_WHAT = {"opening": "health opening", "exemption": "health exemption", "task": "task cancelled for health", "question": "question excused by health"}


def health_uses(core: CoreDatabase, user_id: str, since: datetime, until: datetime) -> list[dict]:
    """[{"at": iso, "what": opening | exemption | task | question}], oldest first."""
    from exemptions.service import get_health_uses
    from infractions.signals import get_health_excuses
    from obedience.service import get_health_cancels
    lo, hi = _iso(since), _iso(until)
    uses = ([{"at": at, "what": what} for at, what in get_health_uses(core, user_id, lo, hi)]
            + [{"at": at, "what": "task"} for at in get_health_cancels(core, user_id, lo, hi)]
            + [{"at": at, "what": "question"} for at in get_health_excuses(core, user_id, lo, hi)])
    return sorted(uses, key=lambda u: u["at"])


def describe(uses: list[dict]) -> str:
    counts: dict[str, int] = {}
    for u in uses:
        counts[u["what"]] = counts.get(u["what"], 0) + 1
    return ", ".join(f"{n} {_WHAT[w]}{'s' if n != 1 and not _WHAT[w].endswith('health') else ''}" for w, n in counts.items())


class HealthPattern:
    def __init__(self, core: CoreDatabase, *, user_id: str, iso_week: Callable[[datetime], str], paused: Callable[[datetime], bool] = lambda _n: False) -> None:
        self._core = core
        self.user_id = user_id
        self._week = iso_week
        self._paused = paused

    def tick(self, now: datetime) -> bool:
        """True when the question was queued now."""
        if self._paused(now):
            return False
        uses = health_uses(self._core, self.user_id, now - WINDOW, now)
        if len(uses) < HEALTH_ASK_FROM:
            return False
        text = (f"That's {len(uses)} health uses in the last 7 days ({describe(uses)}). A health need always comes first and never counts "
                "against you. Is something going on that I should know about? Tell me in a message.")
        week = self._week(now)

        def write(tx: Transaction, _s: object) -> bool:
            return bool(enqueue_in_transaction(tx, user_id=self.user_id, kind="health_pattern", dedupe_key=f"health_pattern:{self.user_id}:{week}",
                                               now=now, text=text))

        queued = apply_transition(self._core, write=write)
        if queued:
            decision("health_pattern", "Health uses reached the weekly question; asked once, nothing restricted.", uses=len(uses))
        return bool(queued)

    async def loop(self, clock, *, interval_seconds: float = 900.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.tick, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The health pattern pass failed; it continues.")
            await asyncio.sleep(interval_seconds)


def week_label(local: Callable[[datetime], datetime]) -> Callable[[datetime], str]:
    def label(now: datetime) -> str:
        d: date = local(now).date()
        year, week, _ = d.isocalendar()
        return f"{year}-W{week:02d}"
    return label
