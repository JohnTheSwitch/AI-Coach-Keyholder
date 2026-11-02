"""
tenancy/admin.py

TenantAdmin (v0.39 Phase E1): the OWNER operations on the tenant registry, used by the local CLI (`python -m cli tenants ...`). Every mutating call checks
the owner identity (exact constant-time match with OWNER_USER_ID; unset refuses everybody) and records a refusal as an audit event. Like the manual
freeze, the real boundary is local access to the machine and the databases; the identity check is a deliberate-typing guard plus an audit trail.
A wearer never uses this class: a wearer accepts or leaves through their own Discord conversation (E3), which calls the registry directly.
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from infrastructure.clock import Clock
from infrastructure.time_format import iso as _iso
from tenancy.halt import PlatformHalt
from tenancy.models import Tenant
from tenancy.registry import InvalidTenantInput, TenantCapReached, TenantError, TenantRegistry

__all__ = ["TenantAdmin", "AdminOutcome", "AdminResult"]


class AdminOutcome(StrEnum):
    DONE = "done"
    OWNER_NOT_CONFIGURED = "owner_not_configured"
    NOT_OWNER = "not_owner"
    NOT_CONFIRMED = "not_confirmed"
    NOT_FOUND = "not_found"                # no such tenant, or not in a state this action applies to
    CAP_REACHED = "cap_reached"
    REFUSED = "refused"                    # duplicate Discord id, tenant zero protection, ...
    INVALID = "invalid"
    WEARER_HALT = "wearer_halt"            # the wearer halted their own execution: only the wearer can clear it


@dataclass(frozen=True)
class AdminResult:
    outcome: AdminOutcome
    tenant: Tenant | None = None

    @property
    def done(self) -> bool:
        return self.outcome is AdminOutcome.DONE


class TenantAdmin:
    def __init__(self, *, registry: TenantRegistry, halt: PlatformHalt, owner_user_id: str | None, clock: Clock) -> None:
        self._registry = registry
        self._halt = halt
        self._owner = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None
        self._clock = clock

    def invite(self, *, actor: str, display_name: str, discord_id: str) -> AdminResult:
        refusal = self._guard(actor, "invite")
        if refusal:
            return refusal
        try:
            return AdminResult(AdminOutcome.DONE, self._registry.invite(
                display_name=display_name, discord_id=discord_id, invited_by="owner", now=self._clock.now()))
        except TenantCapReached:
            return AdminResult(AdminOutcome.CAP_REACHED)
        except InvalidTenantInput:
            return AdminResult(AdminOutcome.INVALID)
        except TenantError:
            return AdminResult(AdminOutcome.REFUSED)

    def suspend(self, *, actor: str, tenant_id: str) -> AdminResult:
        return self._change(actor, "suspend", tenant_id, lambda now: self._registry.suspend(tenant_id, actor="owner", now=now))

    def resume(self, *, actor: str, tenant_id: str) -> AdminResult:
        return self._change(actor, "resume", tenant_id, lambda now: self._registry.resume(tenant_id, actor="owner", now=now))

    def remove(self, *, actor: str, tenant_id: str, confirmed: bool) -> AdminResult:
        refusal = self._guard(actor, "remove")
        if refusal:
            return refusal
        if confirmed is not True:
            return AdminResult(AdminOutcome.NOT_CONFIRMED)
        return self._change(actor, "remove", tenant_id, lambda now: self._registry.remove(tenant_id, actor="owner", now=now), checked=True)

    def halt_all(self, *, actor: str, reason: str) -> AdminResult:
        refusal = self._guard(actor, "halt")
        if refusal:
            return refusal
        try:
            self._halt.set_halted(True, reason=reason, actor="owner", now=self._clock.now())
        except ValueError:
            return AdminResult(AdminOutcome.INVALID)
        return AdminResult(AdminOutcome.DONE)

    def resume_all(self, *, actor: str, reason: str) -> AdminResult:
        refusal = self._guard(actor, "unhalt")
        if refusal:
            return refusal
        try:
            self._halt.set_halted(False, reason=reason, actor="owner", now=self._clock.now())
        except ValueError:
            return AdminResult(AdminOutcome.INVALID)
        return AdminResult(AdminOutcome.DONE)

    def authorize(self, actor: str, action: str) -> AdminResult | None:
        """None when `actor` is the owner; otherwise the audited refusal. For owner operations that live outside this class
        (for example a tenant's own execution halt)."""
        return self._guard(actor, action)

    # ------------------------------------------------------------------ internals

    def _change(self, actor, action, tenant_id, operation, *, checked: bool = False) -> AdminResult:
        if not checked:
            refusal = self._guard(actor, action)
            if refusal:
                return refusal
        ok = operation(self._clock.now())
        return AdminResult(AdminOutcome.DONE if ok else AdminOutcome.NOT_FOUND, self._registry.get(tenant_id) if ok else None)

    def _guard(self, actor: str, action: str) -> AdminResult | None:
        if self._owner is None:
            self._refusal(actor, action, "owner_not_configured")
            return AdminResult(AdminOutcome.OWNER_NOT_CONFIGURED)
        if not isinstance(actor, str) or not hmac.compare_digest(actor.encode("utf-8"), self._owner.encode("utf-8")):
            self._refusal(actor, action, "not_owner")
            return AdminResult(AdminOutcome.NOT_OWNER)
        return None

    def _refusal(self, actor: str, action: str, why: str) -> None:
        with self._registry._core.transaction() as tx:                     # same platform database
            tx.execute("INSERT INTO tenant_events (id, tenant_id, event, actor, detail, created_at) VALUES (?, NULL, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), f"refused_{action}", (actor or "")[:64], why, _iso(self._clock.now())))
