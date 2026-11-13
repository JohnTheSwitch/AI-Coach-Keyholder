"""
system/keyholder_lock_source.py

KeyholderLockObservationSource (owner, 2026-11-10): the owner's own system observes ONE configured lock (`RELEASE_LOCK_ID`) as its keyholder, so the
release engine can work on a lock whose wearer never connected a Chaster account to this bot. It is the single-wearer counterpart of
system/managed_lock_source.py and gives the executor, the release tick and the rules snapshot the same `get_latest()` / `refresh(now)` pair.

The read is `GET /locks/{lockId}` with the keyholder connection (verified live 2026-11-03 for both roles). It is accepted as usable only when:

  the answer is the configured lock, the connected account's `role` on it is `keyholder`, and it is reported `locked`.

Outcomes are written to `managed_lock_observations` (migration 035), whose `source` value `keyholder_search` means "read through the keyholder
connection"; that table's CHECK has no other keyholder value. Anything but `observed` makes `get_latest()` return None, and every consumer already
refuses to act without an observation (fail closed). The executor keeps its own freshness, locked, keyholder and frozen-state checks, and still only
freezes or unfreezes: nothing here can unlock a lock or change its time.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

from chaster.lock_client import ChasterLockClient
from chaster.models import ChasterProviderObservation, ChasterProviderStatus
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from system.managed_lock_source import PROVIDER, LockCheck, _time, _user_id

logger = logging.getLogger("ai_coach_keyholder.system.keyholder_lock_source")

__all__ = ["KeyholderLockObservationSource", "check_keyholder_lock"]

SOURCE = "keyholder_search"


def check_keyholder_lock(raw: object, *, lock_id: str) -> str:
    """Pure check of one `GET /locks/{id}` body: observed, mismatch (another lock, or this account is not its keyholder) or not_found (not locked)."""
    if not isinstance(raw, dict) or raw.get("_id") != lock_id or raw.get("role") != "keyholder":
        return LockCheck.MISMATCH
    if raw.get("status") != "locked":
        return LockCheck.NOT_FOUND
    return LockCheck.OBSERVED


class KeyholderLockObservationSource:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase, lock_id: str, wearer_user_id: str, keyholder_user_id: str,
                 keyholder_tokens, lock_client: ChasterLockClient, clock: Clock, resolver=None) -> None:
        """`resolver` (system/owner_lock.py, owner 2026-11-13): when given, the lock is the owner's recognised lock instead of `lock_id`."""
        self.db_path = Path(db_path)
        self._core = core
        self._fixed_lock_id = lock_id
        self._resolver = resolver
        self._wearer = wearer_user_id
        self._kh_user = keyholder_user_id
        self._tokens = keyholder_tokens
        self._client = lock_client
        self._clock = clock

    @property
    def _lock_id(self) -> str | None:
        return self._resolver.cached() if self._resolver is not None else self._fixed_lock_id

    def get_latest(self) -> ChasterProviderObservation | None:
        if self._lock_id is None:
            return None
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM managed_lock_observations WHERE lock_id = ? ORDER BY fetched_at DESC, rowid DESC LIMIT 1", (self._lock_id,))
        if row is None or row["outcome"] != LockCheck.OBSERVED:
            return None
        return ChasterProviderObservation(
            id=row["id"], connection_id=self._wearer, provider=PROVIDER, chaster_lock_id=row["lock_id"],
            status=ChasterProviderStatus(row["status"]), fetched_at=_parse_iso(row["fetched_at"]), created_at=_parse_iso(row["created_at"]),
            is_frozen=None if row["is_frozen"] is None else bool(row["is_frozen"]), has_keyholder=True,
            end_date=_time(row["end_date"]), frozen_at=_time(row["frozen_at"]),
        )

    def refresh(self, now: datetime) -> str | None:
        """Reads and records the lock once. None when nothing could be read: nothing is recorded and the last state simply goes stale."""
        if self._resolver is not None:
            self._resolver.current(now)
        if self._lock_id is None:
            return None
        lock_id = self._lock_id
        try:
            raw = self._tokens.call_with_token(self._kh_user, now=now, operation=lambda token: self._client.get_lock(access_token=token, lock_id=lock_id))
        except Exception as exc:
            logger.warning("Reading the release lock as its keyholder failed (%s).", type(exc).__name__, exc_info=True)
            return None
        outcome = check_keyholder_lock(raw, lock_id=lock_id)
        if outcome != LockCheck.OBSERVED:
            logger.warning("Release lock check: %s; nothing will act on this lock.", outcome)
        usable = outcome == LockCheck.OBSERVED
        frozen = raw.get("isFrozen") if usable and isinstance(raw.get("isFrozen"), bool) else None
        with self._core.transaction() as tx:
            tx.execute(
                "INSERT INTO managed_lock_observations (id, lock_id, outcome, source, status, is_frozen, frozen_at, end_date, "
                "wearer_chaster_id, keyholder_chaster_id, fetched_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), lock_id, outcome, SOURCE, "locked" if usable else None, None if frozen is None else int(frozen),
                 _iso(_time(raw.get("frozenAt"))) if usable and _time(raw.get("frozenAt")) else None,
                 _iso(_time(raw.get("endDate"))) if usable and _time(raw.get("endDate")) else None,
                 _user_id(raw.get("user")) if isinstance(raw, dict) else None, _user_id(raw.get("keyholder")) if isinstance(raw, dict) else None,
                 _iso(now), _iso(self._clock.now())))
        return outcome
