"""
tenancy/registry.py

TenantRegistry (v0.39 Phase E1): the lifecycle of managed people, kept in the platform database. Domain-service operations only (never reachable
from a model or a conversation path); every change is a compare-and-set transition that also writes an audit row to `tenant_events` in the same
transaction. Nothing here creates or touches a tenant database file: that is the tenant factory (E2).

Lifecycle:  invite (keyholder) -> INVITED -> accept (the wearer, from their own Discord DM) -> ACTIVE <-> SUSPENDED (keyholder pause)
            any non-removed tenant -> REMOVED by the keyholder, or by the wearer leaving. Both exits are unilateral and immediate: a REMOVED or
            SUSPENDED tenant is not "managed", so its polling and execution stop at once. Tenant zero (the owner) can be neither removed nor left.
Cap:        at most `cap` non-removed tenants (tenant zero included); an invitation beyond that is refused.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from tenancy.models import DEFAULT_TENANT_CAP, TENANT_ZERO_ID, RemovedBy, Tenant, TenantStatus

__all__ = ["TenantRegistry", "TenantError", "TenantCapReached", "InvalidTenantInput", "resolve_db_path"]

_DISCORD_ID = re.compile(r"^[0-9]{5,25}$")
_CHASTER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TenantError(RuntimeError):
    """A refused lifecycle operation (never carries personal data beyond ids)."""


class TenantCapReached(TenantError):
    pass


class InvalidTenantInput(ValueError):
    pass


def resolve_db_path(tenant: Tenant, base_dir: str | Path) -> Path:
    """Tenant zero keeps the existing database wherever it already is; every other tenant lives in `base_dir`."""
    if tenant.is_tenant_zero:
        return Path(tenant.db_file)
    name = tenant.db_file
    if "/" in name or "\\" in name or name.startswith("."):
        raise InvalidTenantInput("tenant database file names are plain file names")
    return Path(base_dir) / name


class TenantRegistry:
    def __init__(self, core: CoreDatabase, *, cap: int = DEFAULT_TENANT_CAP) -> None:
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise ValueError("cap must be a positive int")
        self._core = core
        self._cap = cap

    # ------------------------------------------------------------------ setup

    def bootstrap_tenant_zero(self, *, db_file: str | Path, display_name: str = "Owner", discord_id: str | None = None,
                              now: datetime) -> Tenant:
        """Registers the existing system as `tenant_0`, unchanged and idempotent (a second call may only fill in a missing Discord id)."""
        db_text = str(db_file)
        if not db_text.strip():
            raise InvalidTenantInput("db_file must be non-empty")
        discord = self._discord(discord_id) if discord_id else None

        def write(tx: Transaction, _s: object) -> None:
            row = tx.fetch_one("SELECT * FROM tenants WHERE id = ?", (TENANT_ZERO_ID,))
            if row is None:
                tx.execute(
                    "INSERT INTO tenants (id, display_name, discord_id, db_file, status, is_tenant_zero, accepted_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'active', 1, ?, ?, ?)",
                    (TENANT_ZERO_ID, display_name[:64] or "Owner", discord, db_text, _iso(now), _iso(now), _iso(now)))
                self._audit(tx, TENANT_ZERO_ID, "bootstrap", "system", "", now)
            elif row["discord_id"] is None and discord is not None:
                tx.execute("UPDATE tenants SET discord_id = ?, updated_at = ? WHERE id = ?", (discord, _iso(now), TENANT_ZERO_ID))
                self._audit(tx, TENANT_ZERO_ID, "discord_id_set", "system", "", now)

        apply_transition(self._core, write=write)
        return self.get(TENANT_ZERO_ID)

    # ------------------------------------------------------------------ lifecycle

    def invite(self, *, display_name: str, discord_id: str, invited_by: str, now: datetime) -> Tenant:
        name = (display_name or "").strip()
        if not 1 <= len(name) <= 64:
            raise InvalidTenantInput("display_name must be 1-64 characters")
        discord = self._discord(discord_id)
        tenant_id = f"tenant_{uuid.uuid4().hex[:12]}"

        def write(tx: Transaction, _s: object) -> None:
            active = tx.fetch_one("SELECT COUNT(*) AS n FROM tenants WHERE status != 'removed'")["n"]
            if active >= self._cap:
                raise TenantCapReached(f"the tenant cap of {self._cap} is reached")
            if tx.fetch_one("SELECT 1 FROM tenants WHERE discord_id = ? AND status != 'removed'", (discord,)):
                raise TenantError("that Discord id already belongs to a tenant")
            tx.execute(
                "INSERT INTO tenants (id, display_name, discord_id, db_file, status, invited_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'invited', ?, ?, ?)",
                (tenant_id, name, discord, f"{tenant_id}.sqlite", _iso(now), _iso(now), _iso(now)))
            self._audit(tx, tenant_id, "invited", invited_by, "", now)

        apply_transition(self._core, write=write)
        return self.get(tenant_id)

    def accept(self, *, discord_id: str, now: datetime) -> Tenant | None:
        """The wearer accepts, in their own Discord conversation. Only an INVITED tenant of that Discord id; None otherwise."""
        discord = self._discord(discord_id)

        def write(tx: Transaction, _s: object) -> str | None:
            row = tx.fetch_one("SELECT id FROM tenants WHERE discord_id = ? AND status = 'invited'", (discord,))
            if row is None:
                return None
            tx.execute("UPDATE tenants SET status = 'active', accepted_at = ?, updated_at = ? WHERE id = ? AND status = 'invited'",
                       (_iso(now), _iso(now), row["id"]))
            self._audit(tx, row["id"], "accepted", f"wearer:{discord}", "", now)
            return row["id"]

        tenant_id = apply_transition(self._core, write=write)
        return None if tenant_id is None else self.get(tenant_id)

    def suspend(self, tenant_id: str, *, actor: str, now: datetime) -> bool:
        return self._transition(tenant_id, ("active",), "suspended", "suspended", actor, now, allow_zero=True)

    def resume(self, tenant_id: str, *, actor: str, now: datetime) -> bool:
        return self._transition(tenant_id, ("suspended",), "active", "resumed", actor, now, allow_zero=True)

    def remove(self, tenant_id: str, *, actor: str, now: datetime) -> bool:
        """The keyholder removes a tenant. Immediate; tenant zero cannot be removed."""
        return self._remove(tenant_id, RemovedBy.KEYHOLDER, actor, now)

    def leave(self, *, discord_id: str, now: datetime) -> Tenant | None:
        """The wearer leaves, unilaterally and immediately (also when only invited or suspended). Tenant zero cannot leave."""
        discord = self._discord(discord_id)
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT id FROM tenants WHERE discord_id = ? AND status != 'removed' AND is_tenant_zero = 0", (discord,))
        if row is None or not self._remove(row["id"], RemovedBy.WEARER, f"wearer:{discord}", now):
            return None
        return self.get(row["id"])

    def pair_lock(self, tenant_id: str, *, lock_id: str, chaster_account_id: str, actor: str, now: datetime) -> bool:
        """Names the ONE managed lock of a tenant and the wearer's Chaster account (E4 verifies it against Chaster)."""
        if not _CHASTER_ID.match(lock_id or "") or not _CHASTER_ID.match(chaster_account_id or ""):
            raise InvalidTenantInput("lock_id and chaster_account_id must be plain Chaster ids")

        def write(tx: Transaction, _s: object) -> bool:
            cur = tx.execute("UPDATE tenants SET lock_id = ?, chaster_account_id = ?, pairing_confirmed_at = NULL, updated_at = ? "
                             "WHERE id = ? AND status != 'removed' AND is_tenant_zero = 0", (lock_id, chaster_account_id, _iso(now), tenant_id))
            if cur.rowcount:
                self._audit(tx, tenant_id, "paired", actor, "", now)
            return cur.rowcount > 0

        return bool(apply_transition(self._core, write=write))

    def confirm_pairing(self, tenant_id: str, *, actor: str, now: datetime) -> bool:
        """The pairing becomes usable (the wearer confirmed it, or their own connected account proved it)."""
        def write(tx: Transaction, _s: object) -> bool:
            cur = tx.execute("UPDATE tenants SET pairing_confirmed_at = ?, updated_at = ? WHERE id = ? AND status != 'removed' "
                             "AND lock_id IS NOT NULL AND pairing_confirmed_at IS NULL", (_iso(now), _iso(now), tenant_id))
            if cur.rowcount:
                self._audit(tx, tenant_id, "pairing_confirmed", actor, "", now)
            return cur.rowcount > 0

        return bool(apply_transition(self._core, write=write))

    def unpair(self, tenant_id: str, *, actor: str, now: datetime) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            cur = tx.execute("UPDATE tenants SET lock_id = NULL, chaster_account_id = NULL, pairing_confirmed_at = NULL, updated_at = ? "
                             "WHERE id = ? AND lock_id IS NOT NULL", (_iso(now), tenant_id))
            if cur.rowcount:
                self._audit(tx, tenant_id, "unpaired", actor, "", now)
            return cur.rowcount > 0

        return bool(apply_transition(self._core, write=write))

    # ------------------------------------------------------------------ reads

    def get(self, tenant_id: str) -> Tenant | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM tenants WHERE id = ?", (tenant_id,))
        return None if row is None else _row_to_tenant(row)

    def find_by_discord_id(self, discord_id: str) -> Tenant | None:
        """The non-removed tenant of that Discord id (a removed one is deliberately invisible)."""
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM tenants WHERE discord_id = ? AND status != 'removed'", (str(discord_id),))
        return None if row is None else _row_to_tenant(row)

    def find_by_chaster_account(self, chaster_account_id: str) -> Tenant | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM tenants WHERE chaster_account_id = ? AND status != 'removed'", (chaster_account_id,))
        return None if row is None else _row_to_tenant(row)

    def list_tenants(self, *, include_removed: bool = False) -> list[Tenant]:
        sql = "SELECT * FROM tenants" + ("" if include_removed else " WHERE status != 'removed'") + " ORDER BY is_tenant_zero DESC, created_at, id"
        with self._core.transaction() as tx:
            return [_row_to_tenant(r) for r in tx.fetch_all(sql)]

    def managed_tenants(self) -> list[Tenant]:
        """Tenants whose management and polling are currently allowed (ACTIVE only)."""
        return [t for t in self.list_tenants() if t.is_managed]

    def is_managed(self, tenant_id: str) -> bool:
        tenant = self.get(tenant_id)
        return tenant is not None and tenant.is_managed

    def pending_invitations(self) -> list[Tenant]:
        """Invited tenants whose invitation DM has not been delivered yet."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM tenants WHERE status = 'invited' AND invite_notified_at IS NULL ORDER BY created_at")
        return [_row_to_tenant(r) for r in rows]

    def mark_invite_notified(self, tenant_id: str, *, now: datetime) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            cur = tx.execute("UPDATE tenants SET invite_notified_at = ?, updated_at = ? WHERE id = ? AND status = 'invited' "
                             "AND invite_notified_at IS NULL", (_iso(now), _iso(now), tenant_id))
            if cur.rowcount:
                self._audit(tx, tenant_id, "invitation_delivered", "system", "", now)
            return cur.rowcount > 0

        return bool(apply_transition(self._core, write=write))

    def events(self, tenant_id: str | None = None, *, limit: int = 100) -> list[dict]:
        with self._core.transaction() as tx:
            if tenant_id is None:
                rows = tx.fetch_all("SELECT * FROM tenant_events ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
            else:
                rows = tx.fetch_all("SELECT * FROM tenant_events WHERE tenant_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                    (tenant_id, limit))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ internals

    def _transition(self, tenant_id, from_statuses, to_status, event, actor, now, *, allow_zero: bool) -> bool:
        marks = ",".join("?" for _ in from_statuses)

        def write(tx: Transaction, _s: object) -> bool:
            cur = tx.execute(
                f"UPDATE tenants SET status = ?, updated_at = ? WHERE id = ? AND status IN ({marks})"
                + ("" if allow_zero else " AND is_tenant_zero = 0"),
                (to_status, _iso(now), tenant_id, *from_statuses))
            if cur.rowcount:
                self._audit(tx, tenant_id, event, actor, "", now)
            return cur.rowcount > 0

        return bool(apply_transition(self._core, write=write))

    def _remove(self, tenant_id: str, by: RemovedBy, actor: str, now: datetime) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            cur = tx.execute(
                "UPDATE tenants SET status = 'removed', removed_at = ?, removed_by = ?, updated_at = ? "
                "WHERE id = ? AND status != 'removed' AND is_tenant_zero = 0", (_iso(now), by.value, _iso(now), tenant_id))
            if cur.rowcount:
                self._audit(tx, tenant_id, "removed" if by is RemovedBy.KEYHOLDER else "left", actor, "", now)
            return cur.rowcount > 0

        return bool(apply_transition(self._core, write=write))

    @staticmethod
    def _audit(tx: Transaction, tenant_id: str | None, event: str, actor: str, detail: str, now: datetime) -> None:
        tx.execute("INSERT INTO tenant_events (id, tenant_id, event, actor, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (str(uuid.uuid4()), tenant_id, event, (actor or "")[:64], detail[:200], _iso(now)))

    @staticmethod
    def _discord(value: str) -> str:
        text = str(value).strip() if value is not None else ""
        if not _DISCORD_ID.match(text):
            raise InvalidTenantInput("a Discord id is 5-25 digits")
        return text


def _opt(value: str | None) -> datetime | None:
    return None if value is None else _parse_iso(value)


def _row_to_tenant(row) -> Tenant:
    return Tenant(
        id=row["id"], display_name=row["display_name"], discord_id=row["discord_id"],
        chaster_account_id=row["chaster_account_id"], lock_id=row["lock_id"], db_file=row["db_file"],
        status=TenantStatus(row["status"]), is_tenant_zero=bool(row["is_tenant_zero"]), invited_at=_opt(row["invited_at"]),
        accepted_at=_opt(row["accepted_at"]), removed_at=_opt(row["removed_at"]),
        removed_by=None if row["removed_by"] is None else RemovedBy(row["removed_by"]),
        created_at=_parse_iso(row["created_at"]), updated_at=_parse_iso(row["updated_at"]),
        pairing_confirmed_at=_opt(row["pairing_confirmed_at"]) if "pairing_confirmed_at" in row.keys() else None,
    )
