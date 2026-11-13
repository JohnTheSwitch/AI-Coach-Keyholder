"""
system/owner_lock.py

OwnerLockResolver (owner, 2026-11-13): "on its profile there's a premade lock that I can lock into and it recognizes my account."

The keyholder account publishes a shared lock; the owner's main account joins it. With a lock id set to `auto` (RELEASE_LOCK_ID,
PENALTY_FREEZE_LOCK_ID, EXEMPTION_LOCK_ID), the system finds that lock by itself instead of a fixed id that had to be edited for every new lock:

- it lists the locks the connected keyholder account holds (`POST /keyholder/locks/search`, status locked, read-only);
- it takes the ones worn by the owner's own Chaster account (OWNER_CHASTER_ACCOUNT_ID) that are not test locks, and of those the newest;
- a lock worn by ANYONE ELSE is never acted on: the owner is told once per lock (someone joined the shared lock; release them in Chaster);
- several of the owner's locks: the newest is used and the owner is told once; none: nothing acts (every consumer already refuses to act
  without a lock), and the decision log says so.

The choice is kept in memory and refreshed at most every RESOLVE_EVERY; a failed search keeps the last choice (every consumer still checks
the lock's role and state on each read). After a restart the last lock observed for the owner is used until the first search. Nothing here
writes to Chaster.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from chaster.managed_locks import LockRole, parse_lock
from infrastructure.database import Database as CoreDatabase
from infrastructure.decision_log import decision

logger = logging.getLogger("ai_coach_keyholder.system.owner_lock")

__all__ = ["AUTO", "RESOLVE_EVERY", "OwnerLockResolver", "is_auto"]

AUTO = "auto"
RESOLVE_EVERY = timedelta(minutes=10)


def is_auto(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() == AUTO


def _started(raw: dict) -> str:
    for key in ("startDate", "createdAt"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class OwnerLockResolver:
    def __init__(self, core: CoreDatabase, *, owner_chaster_id: str, keyholder_user_id: str, keyholder_tokens, lock_client,
                 clock, notify: Callable[[str, str], None] | None = None) -> None:
        """`notify(key, text)` tells the owner (deduplicated by key by the caller's queue)."""
        self._core = core
        self._owner = owner_chaster_id.strip()
        self._kh_user = keyholder_user_id
        self._tokens = keyholder_tokens
        self._client = lock_client
        self._clock = clock
        self._notify = notify
        self._choice: str | None = None
        self._resolved_at: datetime | None = None
        self._told: set[str] = set()
        self._searched = False                  # after the first successful search, "none" really means none (no fallback to the database)

    # ------------------------------------------------------------------ reads

    def cached(self) -> str | None:
        """The current choice without any network call (after a restart: the last lock observed for the owner)."""
        if self._choice is None and not self._searched:
            with self._core.transaction() as tx:
                row = tx.fetch_one("SELECT lock_id FROM managed_lock_observations WHERE wearer_chaster_id = ? AND outcome = 'observed' "
                                   "ORDER BY fetched_at DESC, rowid DESC LIMIT 1", (self._owner,))
            self._choice = row["lock_id"] if row is not None else None
        return self._choice

    def current(self, now: datetime | None = None) -> str | None:
        """The owner's lock; searches again when the last search is older than RESOLVE_EVERY. Never raises."""
        now = now or self._clock.now()
        if self._resolved_at is None or now - self._resolved_at >= RESOLVE_EVERY:
            self._resolved_at = now
            try:
                self._resolve(now)
            except Exception:
                logger.warning("Finding the owner's lock failed; keeping the last one.", exc_info=True)
        return self.cached()

    # ------------------------------------------------------------------ internals

    def _resolve(self, now: datetime) -> None:
        raws = self._tokens.call_with_token(self._kh_user, now=now, operation=lambda token: self._client.search_keyholder_locks(access_token=token, status="locked"))
        self._searched = True
        mine: list[tuple[str, str]] = []
        for raw in raws or []:
            lock = parse_lock(raw, LockRole.KEYHOLDER)
            if lock is None:
                continue
            if lock.wearer_chaster_id != self._owner:
                self._tell(f"owner_lock:foreign:{lock.lock_id}",
                           f"Someone else ({lock.wearer_username or 'unknown account'}) is locked in a lock your keyholder account holds. The system "
                           "never acts on it. Please release them in Chaster.")
                continue
            if lock.is_test_lock is True:
                continue
            mine.append((_started(raw), lock.lock_id))
        if not mine:
            if self._choice is not None:
                decision("chaster", "The owner's lock is no longer among the active locks the keyholder holds: nothing acts on a lock now.",
                         previous_lock_id=self._choice)
            self._choice = None
            return
        mine.sort()
        chosen = mine[-1][1]
        if len(mine) > 1:
            self._tell(f"owner_lock:several:{chosen}", "You wear more than one active lock held by your keyholder account. The system now uses "
                                                       "the newest one.")
        if chosen != self._choice:
            decision("chaster", "The owner's lock was recognised.", lock_id=chosen, previous_lock_id=self._choice, candidates=len(mine))
        self._choice = chosen

    def _tell(self, key: str, text: str) -> None:
        if key in self._told:
            return
        self._told.add(key)
        decision("chaster", text, key=key)
        if self._notify is not None:
            try:
                self._notify(key, text)
            except Exception:
                logger.warning("Telling the owner about a lock failed.", exc_info=True)
