"""
system/managed_lock_source.py

ManagedLockObservationSource (v0.39 Phase E4): how a tenant's executor, release tick and rules snapshot see the tenant's lock.

Primary source: the KEYHOLDER search (`POST /keyholder/locks/search`, status `locked`) with the platform's single keyholder token, so a managed wearer
never has to give this bot a token. If that call fails and the wearer HAS connected their own Chaster account, their connection (`GET /locks`) is
used instead. Either way the result is accepted only after the pairing check:

  the tenant is ACTIVE, its pairing is CONFIRMED, the lock id is the paired one, the lock's wearer is the paired Chaster account,
  the lock's keyholder is the bot's own Chaster account, and the lock is reported `locked`.

Outcomes are written to the tenant's own `managed_lock_observations` (migration 035): `observed` (usable), `not_found` (the paired lock is no longer
among the keyholder's locked locks: unlocked, archived or gone), `mismatch` (wearer or keyholder differ). `get_latest()` returns a
`ChasterProviderObservation` ONLY for a fresh-or-not `observed` row of the CURRENT pairing; anything else gives None, and every consumer already
refuses to act without an observation (fail closed: skip, never guess). The executor still applies its own freshness, locked, keyholder and
frozen-state checks, and the release engine's secrecy rules are unchanged (the target time never leaves the release engine).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from chaster.lock_client import ChasterLockClient
from chaster.models import ChasterProviderObservation, ChasterProviderStatus
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from tenancy.models import Tenant

logger = logging.getLogger("ai_coach_keyholder.system.managed_lock_source")

__all__ = ["ManagedLockObservationSource", "verify_lock", "LockCheck"]

PROVIDER = "chaster"


class LockCheck:
    OBSERVED = "observed"
    NOT_FOUND = "not_found"
    MISMATCH = "mismatch"


def _user_id(value) -> str | None:
    return value.get("_id") if isinstance(value, dict) and isinstance(value.get("_id"), str) else None


def _time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _parse_iso(value)
    except ValueError:
        return None


def verify_lock(raw_locks: list[dict], *, lock_id: str, wearer_chaster_id: str, bot_chaster_id: str) -> tuple[str, dict | None]:
    """Pure pairing check over a provider list: (outcome, the matching raw lock or None). Never reads `combination` or any token field."""
    match = next((r for r in raw_locks if isinstance(r, dict) and r.get("_id") == lock_id), None)
    if match is None:
        return LockCheck.NOT_FOUND, None
    if _user_id(match.get("user")) != wearer_chaster_id or _user_id(match.get("keyholder")) != bot_chaster_id:
        return LockCheck.MISMATCH, match
    if match.get("status") != "locked":
        return LockCheck.NOT_FOUND, match
    return LockCheck.OBSERVED, match


class ManagedLockObservationSource:
    def __init__(
        self, db_path: str | Path, *, core: CoreDatabase, get_tenant: Callable[[], Tenant | None], wearer_user_id: str,
        keyholder_user_id: str | None, keyholder_tokens, keyholder_connections, lock_client: ChasterLockClient, clock: Clock,
        wearer_tokens=None, wearer_connections=None,
    ) -> None:
        self.db_path = Path(db_path)
        self._core = core
        self._get_tenant = get_tenant
        self._wearer = wearer_user_id
        self._kh_user = keyholder_user_id
        self._kh_tokens = keyholder_tokens
        self._kh_connections = keyholder_connections
        self._client = lock_client
        self._clock = clock
        self._w_tokens = wearer_tokens
        self._w_connections = wearer_connections

    # ------------------------------------------------------------------ the two calls the composition needs

    def get_latest(self) -> ChasterProviderObservation | None:
        tenant = self._ready_tenant()
        if tenant is None:
            return None
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM managed_lock_observations WHERE lock_id = ? ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
                               (tenant.lock_id,))
        if row is None or row["outcome"] != LockCheck.OBSERVED or row["wearer_chaster_id"] != tenant.chaster_account_id:
            return None
        if tenant.pairing_confirmed_at is not None and _parse_iso(row["fetched_at"]) < tenant.pairing_confirmed_at:
            return None                                              # older than the current pairing: not evidence about it
        return ChasterProviderObservation(
            id=row["id"], connection_id=self._wearer, provider=PROVIDER, chaster_lock_id=row["lock_id"],
            status=ChasterProviderStatus(row["status"]), fetched_at=_parse_iso(row["fetched_at"]), created_at=_parse_iso(row["created_at"]),
            is_frozen=None if row["is_frozen"] is None else bool(row["is_frozen"]), has_keyholder=True,
            end_date=_time(row["end_date"]), frozen_at=_time(row["frozen_at"]),
        )

    def refresh(self, now: datetime) -> str | None:
        """Fetches and records one observation of the paired lock. Returns the outcome, or None when nothing could be read
        (not ready, no bot account, both sources failed): then nothing is recorded and the last state simply goes stale."""
        tenant = self._ready_tenant()
        bot_id = self._bot_chaster_id()
        if tenant is None or bot_id is None:
            return None
        raw, source = self._fetch_keyholder(now), "keyholder_search"
        if raw is None:
            raw, source = self._fetch_wearer(now), "wearer_connection"
        if raw is None:
            return None
        outcome, match = verify_lock(raw, lock_id=tenant.lock_id, wearer_chaster_id=tenant.chaster_account_id, bot_chaster_id=bot_id)
        if outcome != LockCheck.OBSERVED:
            logger.warning("Tenant lock check: %s (source %s); nothing will act on this lock.", outcome, source)
        self._record(tenant, outcome, source, match, now)
        return outcome

    # ------------------------------------------------------------------ internals

    def _ready_tenant(self) -> Tenant | None:
        try:
            tenant = self._get_tenant()
        except Exception:
            logger.exception("Reading the tenant registry failed; the lock is treated as unknown.")
            return None
        return tenant if tenant is not None and tenant.lock_ready else None

    def _bot_chaster_id(self) -> str | None:
        if not self._kh_user or self._kh_connections is None:
            return None
        connection = self._kh_connections.get(self._kh_user)
        return None if connection is None else connection.chaster_account_id

    def _fetch_keyholder(self, now: datetime) -> list[dict] | None:
        if self._kh_tokens is None or not self._kh_user:
            return None
        connection = self._kh_connections.get(self._kh_user) if self._kh_connections is not None else None
        if connection is None or "keyholder" not in connection.granted_scopes:
            return None
        try:
            return self._kh_tokens.call_with_token(
                self._kh_user, now=now, operation=lambda token: self._client.search_keyholder_locks(access_token=token, status="locked"))
        except Exception as exc:
            logger.warning("The keyholder search failed (%s).", type(exc).__name__)
            return None

    def _fetch_wearer(self, now: datetime) -> list[dict] | None:
        if self._w_tokens is None or self._w_connections is None or self._w_connections.get(self._wearer) is None:
            return None
        try:
            return self._w_tokens.call_with_token(
                self._wearer, now=now, operation=lambda token: self._client.list_active_locks(access_token=token))
        except Exception as exc:
            logger.warning("The wearer's own lock read failed (%s).", type(exc).__name__)
            return None

    def _record(self, tenant: Tenant, outcome: str, source: str, match: dict | None, now: datetime) -> None:
        usable = outcome == LockCheck.OBSERVED and match is not None
        frozen = match.get("isFrozen") if usable and isinstance(match.get("isFrozen"), bool) else None
        with self._core.transaction() as tx:
            tx.execute(
                "INSERT INTO managed_lock_observations (id, lock_id, outcome, source, status, is_frozen, frozen_at, end_date, "
                "wearer_chaster_id, keyholder_chaster_id, fetched_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), tenant.lock_id, outcome, source, "locked" if usable else None,
                 None if frozen is None else int(frozen),
                 _iso(_time(match.get("frozenAt"))) if usable and _time(match.get("frozenAt")) else None,
                 _iso(_time(match.get("endDate"))) if usable and _time(match.get("endDate")) else None,
                 _user_id(match.get("user")) if match else None, _user_id(match.get("keyholder")) if match else None,
                 _iso(now), _iso(self._clock.now())))
