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

__all__ = ["ReleaseWindow", "WINDOW", "GRACE", "GIVE_UP", "session_end"]

logger = logging.getLogger("ai_coach_keyholder.release_earning")

WINDOW = timedelta(hours=2)
GRACE = timedelta(minutes=10)
GIVE_UP = timedelta(hours=6)
EARLIEST = timedelta(minutes=15)   # decided by Claude, 2026-11-16: a picture from the first 15 minutes (before the release itself) doesn't count


def session_end(cfg, confirmed: datetime) -> datetime | None:
    """When the release session that `confirmed` opened ends (free time over or the local date changes), as `release_engine` decides it;
    after that a lock that is still locked is frozen again. None when it cannot be told within a day."""
    from schedule_windows.windows import WindowKind, classify
    day, at = cfg.local(confirmed).date(), confirmed
    for _ in range(24 * 12):
        at += timedelta(minutes=5)
        if classify(cfg, at) is not WindowKind.DISCRETIONARY or cfg.local(at).date() != day:
            return at - timedelta(minutes=5) if at - timedelta(minutes=5) > confirmed else at
    return None


class ReleaseWindow:
    def __init__(self, core: CoreDatabase, *, user_id: str, last_release: Callable[[], tuple[str, datetime] | None],
                 pictures: Callable[[datetime, datetime], bool | None], local_clock: Callable[[datetime], str],
                 raise_overrun: Callable[[dict, datetime], None] | None = None,
                 stayed_locked: Callable[[datetime, datetime], bool | None] | None = None,
                 open_until: Callable[[datetime], datetime | None] | None = None) -> None:
        self._core, self.user_id = core, user_id
        self._last, self._pictures, self._clock, self._raise = last_release, pictures, local_clock, raise_overrun
        # Owner, 2026-11-16 21:50: the backstop unfroze his lock, he chose to stay locked, and at 22:38 he was asked to explain an "overrun".
        # A release never opened needs no relock picture (`stayed_locked`); the DM says until when the lock can be opened (`open_until`:
        # the release session's end, after which the lock is frozen again).
        self._stayed, self._open_until = stayed_locked, open_until
        self.enabled: Callable[[], bool] = lambda: True       # owner, 2026-11-16 (features/): the wearer's "release relock" switch

    def tick(self, now: datetime) -> None:
        try:
            self._open(now)
            self._check(now)
        except Exception:
            logger.exception("The release window pass failed; it continues.")

    def _open(self, now: datetime) -> None:
        if not self.enabled():
            return                                          # switched off: no window opens, so relocking later has no consequence
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
                                       text=self._text(confirmed, due))

        apply_transition(self._core, write=write)

    def _text(self, confirmed: datetime, due: datetime) -> str:
        try:
            until = self._open_until(confirmed) if self._open_until is not None else None
        except Exception:
            logger.exception("The release session's end could not be read; the window text leaves it out.")
            until = None
        head = (f"Your lock is open to you until {self._clock(until)}; after that it is frozen again. " if until is not None and until < due
                else "")
        return (f"{head}Your release window: 2 hours. If you open it, relock and send the verification picture in Chaster by "
                f"{self._clock(due)}. If you stay locked, nothing is needed.")

    def _check(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM release_windows WHERE user_id = ? AND outcome IS NULL AND due_at <= ?",
                                                  (self.user_id, _iso(now - GRACE)))]
        for row in rows:
            confirmed, due = _parse_iso(row["confirmed_at"]), _parse_iso(row["due_at"])
            if self._stayed is not None:
                try:
                    stayed = self._stayed(confirmed, due + GRACE)
                except Exception:
                    logger.exception("Whether the lock stayed locked could not be read; the pictures decide.")
                    stayed = None
                if stayed:
                    self._close(row, "not_opened", now)
                    continue
            try:
                seen = self._pictures(confirmed + EARLIEST, due + GRACE)
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
