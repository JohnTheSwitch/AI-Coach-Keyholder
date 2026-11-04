"""
infractions/lock_claims.py

The `EPISTEMIC_MISREPRESENTATION` detector: compares each lock report with what Chaster shows. The question is never "does the report match right now" but "was it
false when it was made". Chaster's `unlockedAt` answers that: a lock that ended after the report was true at the time, and a report that simply aged is never a lie.

Only one outcome can become a signal: the wearer said locked, every lock they wear is positively unlocked, and all of them ended before the report was made.
Everything uncertain is left alone (2.12): a failed read waits for the next pass, a lock without `unlockedAt` proves nothing, and an account with no listed lock
gives nothing to compare. An emergency-unlocked lock, an exemption, an Emergency Override or a vacation excuse the report (3.8).

"Unlocked" while Chaster shows locked is the opposite of a concealment: someone may be telling the truth about a device their provider cannot see. The keyholder
hears about it and nothing else happens.

Reports older than the first pass are the baseline and are never judged, the same rule the community review watch applies.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from chaster.managed_locks import ManagedLock
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso
from infractions.excuses import Excuses
from infractions.signals import InfractionSignals, SignalKind
from lock_state.models import LockReportStatus
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.infractions.lock_claims")

__all__ = ["LockClaimWatch", "CLOCK_MARGIN", "CURRENT_CLAIM", "MISMATCH_DETAIL", "MISMATCH_TEXT", "UNLOCKED_NOTE"]

CLOCK_MARGIN = timedelta(minutes=5)         # a report this close to the lock's end is a race between two clocks, not a claim
CURRENT_CLAIM = timedelta(hours=1)          # only a recent report can be compared with Chaster's present state

MISMATCH_DETAIL = "A lock report said locked, but Chaster shows every worn lock had already ended."
MISMATCH_TEXT = ("You reported yourself as locked, but Chaster shows your lock had already ended by then. If there was a health or safety reason, tell me below and "
                 "that's the end of it. Otherwise it goes to your keyholder to look at.")
UNLOCKED_NOTE = "For your information: the wearer reported being unlocked while Chaster shows a lock as locked. Nothing was recorded against anyone."


class LockClaimWatch:
    def __init__(self, db_path: str | Path | None = None, *, core: CoreDatabase | None = None, user_id: str, owner_user_id: str | None, trust_domain: str,
                 signals: InfractionSignals, excuses: Excuses, list_worn_locks: Callable[[datetime], tuple[tuple[ManagedLock, ...], list[str]]]) -> None:
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._user = user_id
        self._owner = owner_user_id
        self._domain = trust_domain
        self._signals = signals
        self._excuses = excuses
        self._list = list_worn_locks
        self._armed_at: datetime | None = None

    def poll(self, now: datetime) -> list[str]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT r.id, r.status, r.reported_at FROM lock_reports r LEFT JOIN lock_claim_checks c ON c.report_id = r.id "
                                "WHERE r.user_id = ? AND c.report_id IS NULL ORDER BY r.sequence_number", (self._user,))
            judged_before = tx.fetch_one("SELECT 1 FROM lock_claim_checks c JOIN lock_reports r ON r.id = c.report_id WHERE r.user_id = ? LIMIT 1",
                                         (self._user,)) is not None
        if self._armed_at is None:
            self._armed_at = now
        if not judged_before:
            older = [row for row in rows if parse_iso(row["reported_at"]) < self._armed_at]
            for row in older:
                self._record(row["id"], "baseline", now)
            rows = [row for row in rows if row not in older]
        if not rows:
            return []
        locks, problems = self._list(now)
        if problems:
            return list(problems)                       # judged on a complete read or not at all
        for row in rows:
            try:
                self._check(row, locks, now)
            except Exception:
                logger.exception("Could not check a lock report; it stays for the next pass.")
                problems = [*problems, "check failed"]
        return list(problems)

    def signal_window(self, subject_id: str) -> tuple[datetime, datetime]:
        with self._core.transaction() as tx:
            reported_at = parse_iso(tx.fetch_one("SELECT reported_at FROM lock_reports WHERE id = ?", (subject_id,))["reported_at"])
        return reported_at, reported_at

    # ------------------------------------------------------------------ internals

    def _check(self, row, locks: tuple[ManagedLock, ...], now: datetime) -> None:
        reported_at = parse_iso(row["reported_at"])
        locked = [lock for lock in locks if lock.status == "locked"]
        if row["status"] == LockReportStatus.UNLOCKED_USER_REPORTED.value:
            if locked and now - reported_at <= CURRENT_CLAIM:
                self._record(row["id"], "unlocked_while_locked", now, note=True)
            else:
                self._record(row["id"], "stale" if locked else "consistent", now)
            return
        if locked:
            self._record(row["id"], "consistent", now)
            return
        if any(lock.emergency_unlocked for lock in locks):
            self._record(row["id"], "excused", now)
            return
        ended = [_ended_at(lock) for lock in locks]
        if not ended or None in ended or any(lock.status != "unlocked" for lock in locks):
            self._record(row["id"], "stale", now)
            return
        if reported_at <= max(ended) + CLOCK_MARGIN:
            self._record(row["id"], "stale", now)       # it was true, or too close to call, when it was made
            return
        if self._excuses.reason(self._user, reported_at, reported_at) is not None:
            self._record(row["id"], "excused", now)
            return
        self._record(row["id"], "contradicted", now, raises=True)

    def _record(self, report_id: str, outcome: str, now: datetime, *, raises: bool = False, note: bool = False) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT OR IGNORE INTO lock_claim_checks (report_id, outcome, checked_at) VALUES (?, ?, ?)", (report_id, outcome, _iso(now)))
            if raises:
                self._signals.raise_in_transaction(tx, user_id=self._user, kind=SignalKind.LOCK_CLAIM, subject_id=report_id, trust_domain=self._domain,
                                                   detail=MISMATCH_DETAIL, wearer_text=MISMATCH_TEXT, now=now)
            if note and self._owner:
                enqueue_in_transaction(tx, user_id=self._owner, kind="lock_claim_note", dedupe_key=f"lock_claim:note:{report_id}", text=UNLOCKED_NOTE, now=now)

        apply_transition(self._core, write=write)


def _ended_at(lock: ManagedLock) -> datetime | None:
    try:
        return parse_iso(lock.unlocked_at) if lock.unlocked_at else None
    except (ValueError, TypeError):
        return None
