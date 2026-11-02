"""
system/lock_pairing.py

LockPairingService (v0.39 Phase E4): binds ONE managed Chaster lock to a tenant, safely.

1. The OWNER pairs (`python -m cli tenants pair <tenant> <lock id>`): a live keyholder search must show the lock as `locked` with the bot's own
   Chaster account as keyholder; the lock's wearer account is taken from that same response (never typed by hand).
2. The pairing only becomes usable once it is CONFIRMED:
   - automatically, when the wearer has connected their own Chaster account and it IS the lock's wearer (proof by OAuth), or
   - by the wearer in their own DM: `lock` shows what was paired, `confirm lock` confirms it, `reject lock` removes the pairing.
Until then (and after `reject lock` or a re-pairing) the tenant's observation source returns nothing, so nothing can act on the lock.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from chaster.lock_client import ChasterLockClient
from chaster.managed_locks import LockRole, parse_lock
from infrastructure.clock import Clock
from system.managed_lock_source import LockCheck, verify_lock
from tenancy.models import Tenant
from tenancy.registry import InvalidTenantInput, TenantRegistry

logger = logging.getLogger("ai_coach_keyholder.system.lock_pairing")

__all__ = ["LockPairingService", "PairOutcome", "PairResult", "wearer_account_lookup"]


def wearer_account_lookup(tenant_dir) -> Callable[[Tenant], str | None]:
    """Reads the Chaster account id of the wearer's OWN connection from the tenant's database (no token is decrypted)."""
    from pathlib import Path

    from infrastructure.database import Database as CoreDatabase
    from tenancy.registry import resolve_db_path

    def lookup(tenant: Tenant) -> str | None:
        path = resolve_db_path(tenant, Path(tenant_dir))
        if not path.exists():
            return None
        with CoreDatabase(path).transaction() as tx:
            row = tx.fetch_one(
                "SELECT c.chaster_account_id AS a FROM chaster_connections c JOIN user_channel_identities i ON i.user_account_id = c.user_id "
                "WHERE i.channel = 'discord' AND i.external_id = ? AND c.connection_status = 'active'", (tenant.discord_id,))
        return None if row is None else row["a"]

    return lookup


class PairOutcome(StrEnum):
    PAIRED_AWAITING_WEARER = "paired_awaiting_wearer"
    PAIRED_CONFIRMED = "paired_confirmed"            # the wearer's own connected account proved it
    NOT_A_TENANT = "not_a_tenant"                    # unknown, not active, or tenant zero
    UNAVAILABLE = "unavailable"                      # the keyholder search could not be done
    NOT_MANAGED = "not_managed"                      # the lock is not a locked lock held by the bot
    ALREADY_PAIRED_ELSEWHERE = "already_paired_elsewhere"
    INVALID = "invalid"


@dataclass(frozen=True)
class PairResult:
    outcome: PairOutcome
    title: str | None = None
    wearer_username: str | None = None


class LockPairingService:
    def __init__(self, *, registry: TenantRegistry, lock_client: ChasterLockClient, keyholder_tokens, keyholder_connections,
                 keyholder_user_id: str | None, clock: Clock,
                 wearer_account_of: Callable[[Tenant], str | None] = lambda tenant: None) -> None:
        self._registry = registry
        self._client = lock_client
        self._tokens = keyholder_tokens
        self._connections = keyholder_connections
        self._kh_user = keyholder_user_id
        self._clock = clock
        self._wearer_account_of = wearer_account_of

    def pair(self, tenant_id: str, lock_id: str, *, actor: str) -> PairResult:
        tenant = self._registry.get(tenant_id)
        if tenant is None or tenant.is_tenant_zero or not tenant.is_managed:
            return PairResult(PairOutcome.NOT_A_TENANT)
        found = self._find(lock_id)
        if found is None:
            return PairResult(PairOutcome.UNAVAILABLE)
        raw, bot_id = found
        if raw is None or raw.get("status") != "locked" or (raw.get("keyholder") or {}).get("_id") != bot_id:
            return PairResult(PairOutcome.NOT_MANAGED)
        lock = parse_lock(raw, LockRole.KEYHOLDER)
        if lock is None or not lock.wearer_chaster_id:
            return PairResult(PairOutcome.NOT_MANAGED)
        now = self._clock.now()
        try:
            self._registry.pair_lock(tenant_id, lock_id=lock_id, chaster_account_id=lock.wearer_chaster_id, actor=actor, now=now)
        except sqlite3.IntegrityError:
            return PairResult(PairOutcome.ALREADY_PAIRED_ELSEWHERE)
        except InvalidTenantInput:
            return PairResult(PairOutcome.INVALID)
        own_account = self._safe_wearer_account(self._registry.get(tenant_id))
        if own_account is not None and own_account == lock.wearer_chaster_id:
            self._registry.confirm_pairing(tenant_id, actor="wearer_oauth", now=now)
            return PairResult(PairOutcome.PAIRED_CONFIRMED, lock.title, lock.wearer_username)
        return PairResult(PairOutcome.PAIRED_AWAITING_WEARER, lock.title, lock.wearer_username)

    def describe(self, tenant: Tenant) -> PairResult | None:
        """What the wearer is asked to confirm: the paired lock's title and Chaster username, read live. None if nothing is paired."""
        if not tenant.lock_id:
            return None
        found = self._find(tenant.lock_id)
        if found is None:
            return PairResult(PairOutcome.UNAVAILABLE)
        raw, bot_id = found
        outcome, match = verify_lock([raw] if raw else [], lock_id=tenant.lock_id, wearer_chaster_id=tenant.chaster_account_id or "",
                                     bot_chaster_id=bot_id)
        if outcome != LockCheck.OBSERVED:
            return PairResult(PairOutcome.NOT_MANAGED)
        lock = parse_lock(match, LockRole.KEYHOLDER)
        return PairResult(PairOutcome.PAIRED_AWAITING_WEARER, lock.title, lock.wearer_username)

    def confirm_by_wearer(self, tenant: Tenant) -> bool:
        return self._registry.confirm_pairing(tenant.id, actor=f"wearer:{tenant.discord_id}", now=self._clock.now())

    def reject_by_wearer(self, tenant: Tenant) -> bool:
        return self._registry.unpair(tenant.id, actor=f"wearer:{tenant.discord_id}", now=self._clock.now())

    # ------------------------------------------------------------------ internals

    def _find(self, lock_id: str) -> tuple[dict | None, str] | None:
        """(the raw lock with that id among the keyholder's LOCKED locks or None, the bot's Chaster id); None when the search failed."""
        if self._tokens is None or self._connections is None or not self._kh_user:
            return None
        connection = self._connections.get(self._kh_user)
        if connection is None or "keyholder" not in connection.granted_scopes:
            return None
        try:
            raw_locks = self._tokens.call_with_token(
                self._kh_user, now=self._clock.now(),
                operation=lambda token: self._client.search_keyholder_locks(access_token=token, status="locked"))
        except Exception as exc:
            logger.warning("The keyholder search for pairing failed (%s).", type(exc).__name__)
            return None
        return next((r for r in raw_locks if isinstance(r, dict) and r.get("_id") == lock_id), None), connection.chaster_account_id

    def _safe_wearer_account(self, tenant: Tenant | None) -> str | None:
        if tenant is None:
            return None
        try:
            return self._wearer_account_of(tenant)
        except Exception:
            logger.exception("Reading the wearer's own Chaster account failed; the wearer must confirm instead.")
            return None
