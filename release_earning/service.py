"""
release_earning/service.py

Earn the release (owner, 2026-11-15). A token alone no longer schedules a release: first a release task has to be verified.

- On a training day, the day's training session is the release task (verified in Chaster like every session).
- Otherwise: 20 lines "I earn my release." (lines/service.py: by hand on paper, shown as a Chaster task with community review; typed in the DM
  only when the task slot stayed taken, as for any lines).
- A task is earned once per release: what counts is a verified session or completed release lines SINCE the last token spent on a release
  (a release that failed and was refunded keeps what was earned).
- Rejected or missed: the token stays where it is and nothing is lost. Rejected lines are written again; a missed training day means release
  lines the next day.

Nothing here touches a token, a plan or a lock: the release tick asks `hold(now)` before it plans a release and waits while it gives a reason.
Health openings, health exemptions and the emergency option never go through the release tick, so they never wait for this. A failure to read
anything here holds nothing (it never blocks a release by accident, the same rule as release debt).

No table of its own: it reads lines (`lines.service`), training outcomes (`obedience.service`) and the last spend (`release_engine`) through the
callables the wiring passes in.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from infrastructure.decision_log import decision

logger = logging.getLogger("ai_coach_keyholder.release_earning")

__all__ = ["ReleaseEarning", "WAIT_TRAINING", "WAIT_LINES"]

WAIT_TRAINING = "release_waiting_for_training"
WAIT_LINES = "release_waiting_for_lines"


class ReleaseEarning:
    def __init__(self, *, user_id: str, last_spend_at: Callable[[], str | None], training_verified_since: Callable[[str], bool],
                 training_today: Callable[[datetime], bool], release_lines_done_since: Callable[[str], bool],
                 assign_release_lines: Callable[[str, datetime], bool], remind: Callable[[datetime], None] | None = None) -> None:
        """`last_spend_at()`: ISO time of the last token spent on a release that was not refunded (None: never). `assign_release_lines(since, now)`:
        assigns the release lines unless some were already assigned since then (True when it assigned them now)."""
        self.user_id = user_id
        self._last_spend = last_spend_at
        self._trained = training_verified_since
        self._training_today = training_today
        self._lines_done = release_lines_done_since
        self._assign = assign_release_lines
        # Owner, 2026-11-25 (proposal 2): his lines were assigned at 05:29 and nothing reminded him for 39 hours while the token waited. While
        # the release waits only on lines, `remind(now)` may say so (once a day, in free time: the wiring decides).
        self._remind = remind

    def hold(self, now: datetime) -> str | None:
        """None: the release is earned (or this could not be read). Otherwise why it waits."""
        try:
            since = self._last_spend() or ""
            if self._trained(since) or self._lines_done(since):
                return None
            if self._training_today(now):
                return WAIT_TRAINING
            if self._assign(since, now):
                decision("release_earning", "A token is ready: 20 release lines assigned before the release is planned.")
            elif self._remind is not None:
                try:
                    self._remind(now)
                except Exception:
                    logger.exception("The release-lines reminder failed; nothing else changes.")
            return WAIT_LINES
        except Exception:
            logger.exception("Could not tell whether the release was earned; it is not held for it.")
            return None
