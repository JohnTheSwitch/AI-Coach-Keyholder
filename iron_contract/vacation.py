"""
iron_contract/vacation.py

Scheduled vacations, the pause mode (v0.39 Phase G6; table `vacations`, migration 041). A wearer schedules one while UNLOCKED (the guard is in
iron_contract.guard, applied by the application). During it the game is paused, not switched off:
- no check-in is scheduled or sent, and open ones are closed as `skipped` (no deadline turns into a miss);
- a local day that lies ENTIRELY inside the vacation is not evaluated (no pool progress, no token, the clean-day streak neither grows nor breaks);
- a confirmed release cannot raise the Progressive Conditioning factor.
Penalty windows, incidents, the token ledger, the rules and every safety path are untouched.

Rules: local dates in APP_TIMEZONE, inclusive; at most MAX_VACATION_DAYS days; starting today at the earliest (a vacation starting today begins NOW, not at
the past midnight, so it never excuses what already happened) and at most MAX_LEAD_DAYS ahead; one scheduled or running vacation at a time. `vacation end`
ends a running vacation at once (always allowed: it only makes things stricter); `vacation cancel` drops one that has not started (needs unlocked).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

__all__ = ["VacationService", "Vacation", "ScheduleOutcome", "MAX_VACATION_DAYS", "MAX_LEAD_DAYS"]

MAX_VACATION_DAYS = 14
MAX_LEAD_DAYS = 90


class ScheduleOutcome(StrEnum):
    SCHEDULED = "scheduled"
    INVALID = "invalid"              # end before start
    IN_THE_PAST = "in_the_past"
    TOO_LONG = "too_long"
    TOO_FAR_AHEAD = "too_far_ahead"
    ALREADY_PLANNED = "already_planned"


@dataclass(frozen=True)
class Vacation:
    id: str
    start_date: date
    end_date: date
    start_at: datetime
    end_at: datetime
    ended_early: bool

    def running(self, at: datetime) -> bool:
        return self.start_at <= at < self.end_at


def _row(r) -> Vacation:
    return Vacation(id=r["id"], start_date=date.fromisoformat(r["start_date"]), end_date=date.fromisoformat(r["end_date"]),
                    start_at=_parse_iso(r["start_at"]), end_at=_parse_iso(r["end_at"]), ended_early=bool(r["ended_early"]))


class VacationService:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, timezone_name: str = "Europe/Prague") -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._tz = ZoneInfo(timezone_name)

    def _midnight(self, day: date) -> datetime:
        return datetime.combine(day, time(0, 0), tzinfo=self._tz)

    # ------------------------------------------------------------------ wearer actions

    def schedule(self, user_id: str, start: date, end: date, *, now: datetime) -> tuple[ScheduleOutcome, Vacation | None]:
        today = now.astimezone(self._tz).date()
        if end < start:
            return ScheduleOutcome.INVALID, None
        if start < today:
            return ScheduleOutcome.IN_THE_PAST, None
        if (end - start).days + 1 > MAX_VACATION_DAYS:
            return ScheduleOutcome.TOO_LONG, None
        if (start - today).days > MAX_LEAD_DAYS:
            return ScheduleOutcome.TOO_FAR_AHEAD, None
        start_at = max(self._midnight(start), now)
        end_at = self._midnight(end + timedelta(days=1))

        def write(tx: Transaction, _s: object):
            if self._open_in(tx, user_id, now) is not None:
                return ScheduleOutcome.ALREADY_PLANNED, None
            vid = str(uuid.uuid4())
            tx.execute("INSERT INTO vacations (id, user_id, start_date, end_date, start_at, end_at, status, ended_early, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'scheduled', 0, ?, ?)",
                       (vid, user_id, start.isoformat(), end.isoformat(), _iso(start_at), _iso(end_at), _iso(now), _iso(now)))
            return ScheduleOutcome.SCHEDULED, self._open_in(tx, user_id, now)

        return apply_transition(self._core, write=write)

    def cancel_upcoming(self, user_id: str, *, now: datetime) -> bool:
        """Drops a vacation that has not started yet."""
        def write(tx: Transaction, _s: object) -> bool:
            v = self._open_in(tx, user_id, now)
            if v is None or v.start_at <= now:
                return False
            tx.execute("UPDATE vacations SET status = 'cancelled', updated_at = ? WHERE id = ?", (_iso(now), v.id))
            return True

        return apply_transition(self._core, write=write)

    def end_now(self, user_id: str, *, now: datetime) -> bool:
        """Ends a running vacation at once."""
        def write(tx: Transaction, _s: object) -> bool:
            v = self._open_in(tx, user_id, now)
            if v is None or not v.running(now):
                return False
            tx.execute("UPDATE vacations SET end_at = ?, ended_early = 1, updated_at = ? WHERE id = ?", (_iso(now), _iso(now), v.id))
            return True

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ reads

    def current_or_next(self, user_id: str, now: datetime) -> Vacation | None:
        with self._core.transaction() as tx:
            return self._open_in(tx, user_id, now)

    def is_paused(self, user_id: str, at: datetime) -> bool:
        with self._core.transaction() as tx:
            return self.running_id_in(tx, user_id, at) is not None

    @staticmethod
    def running_id_in(tx: Transaction, user_id: str, at: datetime) -> str | None:
        row = tx.fetch_one("SELECT id FROM vacations WHERE user_id = ? AND status = 'scheduled' AND start_at <= ? AND end_at > ? LIMIT 1",
                           (user_id, _iso(at), _iso(at)))
        return None if row is None else row["id"]

    def covers(self, user_id: str, start: datetime, end: datetime) -> bool:
        """True when [start, end) lies entirely inside one vacation (a fully paused day)."""
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM vacations WHERE user_id = ? AND status = 'scheduled' AND start_at <= ? AND end_at >= ? LIMIT 1",
                                (user_id, _iso(start), _iso(end))) is not None

    @staticmethod
    def _open_in(tx: Transaction, user_id: str, now: datetime) -> Vacation | None:
        row = tx.fetch_one("SELECT * FROM vacations WHERE user_id = ? AND status = 'scheduled' AND end_at > ? ORDER BY start_at LIMIT 1",
                           (user_id, _iso(now)))
        return None if row is None else _row(row)
