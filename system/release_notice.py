"""
system/release_notice.py

The Communication-First release flow (v0.39 Phase J2). A release is never held back silently. When every condition for a release is met (the
secret plan is due, no veto, the barrier confirmed, a discretionary window, a token), and ONLY then, so the plan's secret timing is never
hinted at earlier:

  1. the user is told they are eligible today, BEFORE anything else happens;
  2. if today's check-in is incomplete, that same message prompts for it, and the release waits;
  3. the reply releases them on the next cycle, whatever a rubric makes of it: submitting is what counts.

Never a trap: an unanswered prompt expires as `skipped` (no Trust effect), so ignoring a release costs nothing. Never a silent wait: if the
prompt cannot be delivered at all, or a check-in does not apply today, the release simply goes ahead; if the day's window closes while it
waits, the user is told it moved. The 72-hour backstop never passes through here. Each message goes out once per local day
(`release_notices`, migration 045). No text carries a time, a target or a token count.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from supervision.checkins import CheckInService, SendOutcome

logger = logging.getLogger("ai_coach_keyholder.system.release_notice")

__all__ = ["ReleaseNotifier", "ELIGIBLE_NOW", "CHECK_IN_PROMPT", "AFTER_CHECK_IN", "WINDOW_CLOSED"]

ELIGIBLE_NOW = "You're eligible for release today! Releasing you now."
CHECK_IN_PROMPT = "You're eligible for release today! Please complete your quick daily check-in first to execute the unfreeze. Just reply here: how did today go?"
AFTER_CHECK_IN = "Thanks, check-in received. Releasing you now."
WINDOW_CLOSED = "Today's release window has closed, so your release moves to the next open window. Nothing else is needed from you."


class ReleaseNotifier:
    def __init__(self, core: CoreDatabase, *, user_id: str, send: Callable[[str], SendOutcome], today: Callable[[datetime], date],
                 check_ins: CheckInService | None = None) -> None:
        self._core = core
        self._user = user_id
        self._send = send
        self._today = today
        self._check_ins = check_ins

    def clear_to_release(self, now: datetime) -> bool:
        """Called when every release condition is met, BEFORE the token is spent. True means go ahead, False means wait for the check-in."""
        day = self._today(now).isoformat()
        row = self._row(day)
        gate = self._check_ins
        if gate is None or not gate.release_check_in_required(now) or gate.daily_check_in_complete(now):
            if row is None or row["eligible_at"] is None:
                self._tell(ELIGIBLE_NOW)
                self._mark(day, now, "eligible_at", "ready_at")
            elif row["ready_at"] is None:
                self._tell(AFTER_CHECK_IN)
                self._mark(day, now, "ready_at")
            return True
        if row is not None and row["prompted_at"] is not None:
            return False                                 # already asked today: the prompt is open
        outcome = gate.open_release_check_in(CHECK_IN_PROMPT, now)
        if outcome is SendOutcome.UNDELIVERABLE:
            logger.warning("The release prompt could not be delivered; releasing anyway, a hold nobody can be told about is a silent block.")
            self._mark(day, now, "eligible_at")
            return True
        self._mark(day, now, "eligible_at", "prompted_at")
        return False

    def window_closed(self, now: datetime) -> None:
        """The day's window ended while the release was announced: say so, once, instead of leaving them waiting."""
        day = self._today(now).isoformat()
        row = self._row(day)
        if row is not None and row["eligible_at"] is not None and row["moved_at"] is None:
            self._tell(WINDOW_CLOSED)
            self._mark(day, now, "moved_at")

    # ------------------------------------------------------------------ internals

    def _tell(self, text: str) -> None:
        try:
            self._send(text)
        except Exception:                                # a notice must never be able to stop a release
            logger.exception("Sending a release notice failed.")

    def _row(self, day: str):
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT * FROM release_notices WHERE user_id = ? AND local_date = ?", (self._user, day))

    def _mark(self, day: str, now: datetime, *columns: str) -> None:
        assignments = ", ".join(f"{c} = ?" for c in columns)            # fixed column names from this module only

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT OR IGNORE INTO release_notices (user_id, local_date) VALUES (?, ?)", (self._user, day))
            tx.execute(f"UPDATE release_notices SET {assignments} WHERE user_id = ? AND local_date = ?",
                       (*[_iso(now)] * len(columns), self._user, day))

        apply_transition(self._core, write=write)
