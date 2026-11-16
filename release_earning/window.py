"""
release_earning/window.py

The release window (owner, 2026-11-15/16; story idea P4, autonomous queue B3). When a release is confirmed he has 2 hours to relock and send a
verification picture in Chaster. On time = nothing. No picture by the window + 10 minutes = he is asked to explain on the usual path
(RELEASE_WINDOW_OVERRUN, warning level, 20 lines); unexplained = a Warning. An unreadable Chaster is retried and, after GIVE_UP, dropped
(unknown never counts against him). No lock action: the existing re-freeze is unchanged. Owns `release_windows` (migration 083).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from infrastructure.database import Database as CoreDatabase, Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction

__all__ = ["ReleaseWindow", "WINDOW", "GRACE", "GIVE_UP"]

logger = logging.getLogger("ai_coach_keyholder.release_earning")

WINDOW = timedelta(hours=2)
GRACE = timedelta(minutes=10)
GIVE_UP = timedelta(hours=6)


class ReleaseWindow:
    def __init__(self, core: CoreDatabase, *, user_id: str, last_release: Callable[[], tuple[str, datetime] | None],
                 pictures: Callable[[datetime, datetime], bool | None], local_clock: Callable[[datetime], str],
                 raise_overrun: Callable[[dict, datetime], None] | None = None) -> None:
        self._core, self.user_id = core, user_id
        self._last, self._pictures, self._clock, self._raise = last_release, pictures, local_clock, raise_overrun

    def tick(self, now: datetime) -> None:
        try:
            self._open(now)
            self._check(now)
        except Exception:
            logger.exception("The release window pass failed; it continues.")

    def _open(self, now: datetime) -> None:
        last = self._last()
        if last is None:
            return
        auth_id, confirmed = last
        if now - confirmed > WINDOW + GIVE_UP:
            return                                          # an old release (before this existed) is never checked
        due = confirmed + WINDOW

        def write(tx: Transaction, _s: object) -> None:
            if tx.execute("INSERT OR IGNORE INTO release_windows (authorization_id, user_id, confirmed_at, due_at, created_at) VALUES (?, ?, ?, ?, ?)",
                          (auth_id, self.user_id, _iso(confirmed), _iso(due), _iso(now))).rowcount:
                write_event(tx, DomainEvent(event_type="release_earning.window_opened", source_module="release_earning", occurred_at=now,
                                            payload={"authorization_id": auth_id}))
                enqueue_in_transaction(tx, user_id=self.user_id, kind="release_window", dedupe_key=f"release_window:{auth_id}", now=now, immediate=True,
                                       text=f"Your release window: 2 hours. Relock and send the verification picture in Chaster by {self._clock(due)}.")

        apply_transition(self._core, write=write)

    def _check(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM release_windows WHERE user_id = ? AND outcome IS NULL AND due_at <= ?",
                                                  (self.user_id, _iso(now - GRACE)))]
        for row in rows:
            confirmed, due = _parse_iso(row["confirmed_at"]), _parse_iso(row["due_at"])
            try:
                seen = self._pictures(confirmed, due + GRACE)
            except Exception:
                logger.exception("The release pictures could not be read; tried again later.")
                seen = None
            if seen is None:
                if now - due > GIVE_UP:
                    self._close(row, "unknown", now)
                continue
            if seen:
                self._close(row, "picture", now)
                continue
            if self._close(row, "asked", now) and self._raise is not None:
                self._raise(row, now)

    def _close(self, row: dict, outcome: str, now: datetime) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE release_windows SET outcome = ?, checked_at = ? WHERE authorization_id = ? AND outcome IS NULL",
                              (outcome, _iso(now), row["authorization_id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="release_earning.window_checked", source_module="release_earning", occurred_at=now,
                                            payload={"authorization_id": row["authorization_id"], "outcome": outcome}))
            return bool(done)

        return bool(apply_transition(self._core, write=write))

    async def loop(self, clock, *, interval_seconds: float = 300.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.tick, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The release window pass failed; it continues.")
            await asyncio.sleep(interval_seconds)
