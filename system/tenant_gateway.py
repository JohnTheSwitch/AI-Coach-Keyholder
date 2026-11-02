"""
system/tenant_gateway.py

TenantGateway (v0.39 Phase E3): decides what ONE incoming Discord DM means when multi-wearer is on, and hands it to exactly one tenant.

Routing by the author's Discord id (tenancy.router):
  unknown / removed  -> a fixed refusal; the text is not processed and not stored anywhere.
  invited            -> only `accept` and `decline` mean something; anything else repeats the invitation. Nothing is stored.
  suspended          -> only the wearer's own safety commands (`halt`, `resume`, `leave`) work; everything else gets a fixed notice.
  active             -> the wearer's safety commands first, then the tenant's OWN ApplicationService (tenant zero: the existing one).

Wearer commands (exact, case-insensitive, handled before any conversation, never by a model):
  halt          stops every lock action for THIS tenant at once (its own execution halt, reason `wearer_halt`).
  resume        clears only a halt the wearer set; it cannot clear the keyholder's halt or a new tenant's initial halt.
  leave         asks for confirmation; `leave confirm` ends the membership unilaterally and immediately: the tenant is removed, its
                execution is halted and its loop stops.
  accept/decline (invited only) record or refuse consent. Acceptance writes a consent record into the tenant's own database.

Owner command (only from tenant zero's Discord id, i.e. the owner's verified identity): `invite <discord id> <name>`.

Logging: a message is stored only in the database of the tenant it belongs to; strangers' and invitees' texts are never stored.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from application.models import EmbedSpec, IncomingMessage, OutgoingMessage
from chaster_execution.control import ExecutionControl
from database.database import Database
from database.models import ConsentAction, ConsentRecord, ConsentTargetType
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from system.tenant_loops import TenantLoopSupervisor
from tenancy.admin import AdminOutcome, TenantAdmin
from tenancy.models import WEARER_HALT_REASON, Tenant
from tenancy.registry import TenantRegistry, resolve_db_path
from tenancy.router import RouteKind, TenantRouter

logger = logging.getLogger("ai_coach_keyholder.system.tenant_gateway")

__all__ = ["TenantGateway", "GatewayReply", "InvitationNotifier", "WEARER_HALT_REASON", "TEXTS", "invitation_text"]

CONSENT_TARGET = "multi_wearer_keyholder"

TEXTS = {
    "stranger": "This bot only talks with people its keyholder has invited.",
    "accepted": ("Welcome. You have joined. Send any message to start setting up. At any time: `halt` stops every lock action at once, "
                 "`leave` ends your membership."),
    "accept_failed": "That did not work, nothing was changed. Please try again.",
    "declined": "You declined the invitation. Nothing about you is kept as an active membership.",
    "suspended": "Your keyholder has paused your membership for now. `halt` and `leave` still work at any time.",
    "halted": "Done: every lock action for you is stopped. Send `resume` to undo your own halt.",
    "halt_failed": "The halt could not be written. Please try again at once; if it keeps failing, use Chaster directly.",
    "resumed": "Your own halt is cleared.",
    "resume_refused": "There is no halt of yours to clear. A halt set by your keyholder can only be cleared by them.",
    "leave_confirm": ("Leaving ends your membership at once: your lock is no longer managed by this bot. "
                      "Send `leave confirm` to leave."),
    "left": "You have left. Your lock is no longer managed by this bot. Nothing more will happen on your behalf.",
    "owner_cannot_leave": "The owner's own account cannot leave.",
    "unavailable": "I cannot reach your space right now. Your safety commands `halt` and `leave` still work.",
    "invite_usage": "Usage: `invite <discord id> <name>`.",
    "no_lock": "No lock is paired with you yet. Your keyholder pairs it; then you confirm it here.",
    "lock_unreachable": "I cannot check Chaster right now. Nothing was changed; try again later.",
    "lock_not_managed": "The paired lock is not currently a locked lock held by this bot, so nothing can act on it.",
    "lock_confirmed": "Confirmed. This lock is now yours to be managed here; `halt` still stops everything at once.",
    "lock_rejected": "The pairing was removed. Nothing will act on that lock.",
    "nothing_to_confirm": "There is nothing to confirm.",
}

_INVITE_TEXTS = {
    AdminOutcome.DONE: "Invitation created; the person will get a DM and must send `accept` themselves.",
    AdminOutcome.CAP_REACHED: "Refused: the tenant cap is reached.",
    AdminOutcome.REFUSED: "Refused: that Discord id already belongs to a tenant.",
    AdminOutcome.INVALID: "Refused: a Discord id is 5-25 digits and a name 1-64 characters.",
    AdminOutcome.NOT_OWNER: "Refused: only the owner can invite.",
    AdminOutcome.OWNER_NOT_CONFIGURED: "Refused: OWNER_USER_ID is not configured.",
}


def invitation_text(name: str) -> str:
    return (f"Hello {name}. The keyholder of this bot has invited you to let it act as a keyholder for your Chaster lock.\n"
            "If you accept: the bot keeps your own separate records (goals, check-ins, trust) and may lock or release your managed lock "
            "within its safety rules. You can stop every lock action at any moment with `halt`, and end everything with `leave`.\n"
            "This system uses Progressive Conditioning: over time, earning a release becomes gradually harder. The exact amount stays hidden. "
            "The bot also keeps a Coaching Memory of your goals, patterns and quotes, which you can view with `memory` and erase with `forget`, "
            "and it may write private suggestions about your regime for your keyholder. You can switch Progressive Conditioning off at any time "
            "with `conditioning off`; it then stays at its current level.\n"
            "Reply `accept` to join or `decline` to refuse. Nothing happens until you accept.")


@dataclass(frozen=True)
class GatewayReply:
    text: str
    embed: EmbedSpec | None = None
    log_db: Database | None = None          # where this exchange may be stored (None: not stored at all)


class TenantGateway:
    def __init__(self, *, registry: TenantRegistry, supervisor: TenantLoopSupervisor, admin: TenantAdmin | None,
                 owner_user_id: str | None, tenant_zero_handler: Callable[[IncomingMessage], OutgoingMessage],
                 tenant_zero_db: Database, tenant_dir, clock: Clock, tenant_zero_control: ExecutionControl | None = None,
                 pairing=None) -> None:
        self._registry = registry
        self._router = TenantRouter(registry)
        self._supervisor = supervisor
        self._admin = admin
        self._owner = owner_user_id
        self._zero_handler = tenant_zero_handler
        self._zero_db = tenant_zero_db
        self._zero_control = tenant_zero_control or ExecutionControl(tenant_zero_db.db_path, core=tenant_zero_db._core)
        self._tenant_dir = tenant_dir
        self._pairing = pairing          # system.lock_pairing.LockPairingService (Phase E4)
        self._clock = clock

    async def handle(self, incoming: IncomingMessage) -> GatewayReply:
        route = self._router.route_discord(incoming.external_user_id)
        command = (incoming.text or "").strip().lower()
        if route.kind is RouteKind.UNKNOWN:
            return GatewayReply(TEXTS["stranger"])
        tenant = route.tenant
        if route.kind is RouteKind.INVITED:
            return await self._invited(tenant, command, incoming)

        log_db = await self._log_db(tenant)
        safety = await self._safety_command(tenant, command)
        if safety is not None:
            return GatewayReply(safety, log_db=log_db)
        if route.kind is RouteKind.SUSPENDED:
            return GatewayReply(TEXTS["suspended"], log_db=log_db)
        if not tenant.is_tenant_zero and command in ("lock", "confirm lock", "reject lock"):
            return GatewayReply(await asyncio.to_thread(self._lock_command, tenant, command), log_db=log_db)

        if tenant.is_tenant_zero:
            owner = self._owner_command(incoming.text or "")
            if owner is not None:
                return GatewayReply(owner, log_db=log_db)
            outgoing = await asyncio.to_thread(self._zero_handler, incoming)
            return GatewayReply(outgoing.text, outgoing.embed, log_db)

        runtime = await self._supervisor.get_or_build(tenant.id)
        if runtime is None:
            return GatewayReply(TEXTS["unavailable"], log_db=log_db)
        if runtime.tenant_id != tenant.id:                                   # hardening: never hand a message to another tenant's runtime
            logger.error("Runtime mismatch for a routed message; refused.")
            return GatewayReply(TEXTS["unavailable"])
        outgoing = await asyncio.to_thread(runtime.handle_message, incoming)
        return GatewayReply(outgoing.text, outgoing.embed, log_db)

    # ------------------------------------------------------------------ invited

    async def _invited(self, tenant: Tenant, command: str, incoming: IncomingMessage) -> GatewayReply:
        now = self._clock.now()
        if command == "accept":
            accepted = self._registry.accept(discord_id=incoming.external_user_id, now=now)
            if accepted is None:
                return GatewayReply(TEXTS["accept_failed"])
            runtime = await self._supervisor.get_or_build(accepted.id)
            if runtime is not None:
                self._record_consent(Database(runtime.db_path, core=runtime.core), now, tenant.display_name)
            return GatewayReply(TEXTS["accepted"])
        if command == "decline":
            self._registry.leave(discord_id=incoming.external_user_id, now=now)
            return GatewayReply(TEXTS["declined"])
        return GatewayReply(invitation_text(tenant.display_name))

    def _record_consent(self, db: Database, now: datetime, name: str) -> None:
        try:
            db.save_consent_record(ConsentRecord(
                created_at=now, target_type=ConsentTargetType.INTEGRATION, target_id=CONSENT_TARGET,
                action=ConsentAction.APPROVED, explanation_shown=invitation_text(name), user_comment="accept"))
        except Exception:
            logger.exception("Recording the acceptance in the tenant database failed; the registry still holds the acceptance time.")

    # ------------------------------------------------------------------ wearer safety commands

    async def _safety_command(self, tenant: Tenant, command: str) -> str | None:
        if command == "halt":
            return self._halt(tenant)
        if command == "resume":
            return self._resume(tenant)
        if command == "leave":
            return TEXTS["owner_cannot_leave"] if tenant.is_tenant_zero else TEXTS["leave_confirm"]
        if command == "leave confirm":
            if tenant.is_tenant_zero:
                return TEXTS["owner_cannot_leave"]
            return await self._leave(tenant)
        return None

    def _tenant_core(self, tenant: Tenant):
        """(path, core) of an EXISTING tenant database, or None when it was never created (then nothing can act for the tenant)."""
        runtime = self._supervisor.runtimes.get(tenant.id)
        if runtime is not None:
            return runtime.db_path, runtime.core
        if tenant.archived_at is not None:
            return None
        path = resolve_db_path(tenant, self._tenant_dir)
        return (path, CoreDatabase(path)) if path.exists() else None

    def _control(self, tenant: Tenant) -> ExecutionControl | None:
        if tenant.is_tenant_zero:
            return self._zero_control
        found = self._tenant_core(tenant)
        return None if found is None else ExecutionControl(found[0], core=found[1])

    def _halt(self, tenant: Tenant) -> str:
        control = self._control(tenant)
        if control is None:
            return TEXTS["halted"]          # the tenant database does not exist yet: nothing can act, and a new one starts halted
        try:
            control.set_halted(True, reason=WEARER_HALT_REASON, now=self._clock.now())
        except Exception:
            logger.exception("A wearer halt could not be written.")
            return TEXTS["halt_failed"]
        return TEXTS["halted"]

    def _resume(self, tenant: Tenant) -> str:
        control = self._control(tenant)
        if control is None:
            return TEXTS["resume_refused"]
        try:
            if not control.is_halted() or control.reason() != WEARER_HALT_REASON:
                return TEXTS["resume_refused"]
            control.set_halted(False, reason="wearer_resume", now=self._clock.now())
        except Exception:
            logger.exception("A wearer resume failed.")
            return TEXTS["resume_refused"]
        return TEXTS["resumed"]

    async def _leave(self, tenant: Tenant) -> str:
        self._halt(tenant)                                               # belt and braces: nothing may act while leaving
        left = self._registry.leave(discord_id=tenant.discord_id, now=self._clock.now())
        await self._supervisor.stop_tenant(tenant.id)
        return TEXTS["left"] if left is not None else TEXTS["accept_failed"]

    # ------------------------------------------------------------------ lock pairing (wearer side)

    def _lock_command(self, tenant: Tenant, command: str) -> str:
        if self._pairing is None or not tenant.lock_id:
            return TEXTS["no_lock"]
        if command == "reject lock":
            self._pairing.reject_by_wearer(tenant)
            return TEXTS["lock_rejected"]
        if command == "confirm lock" and tenant.pairing_confirmed_at is not None:
            return TEXTS["nothing_to_confirm"]
        described = self._pairing.describe(tenant)                 # always checked live: never confirm a lock that is not held right now
        if described is None:
            return TEXTS["no_lock"]
        if described.outcome.value == "unavailable":
            return TEXTS["lock_unreachable"]
        if described.outcome.value == "not_managed":
            return TEXTS["lock_not_managed"]
        if command == "confirm lock":
            return TEXTS["lock_confirmed"] if self._pairing.confirm_by_wearer(tenant) else TEXTS["nothing_to_confirm"]
        state = "is confirmed" if tenant.pairing_confirmed_at is not None else "waits for you: send `confirm lock` if it is yours, otherwise `reject lock`"
        return f"Paired lock: '{described.title or 'untitled'}' of Chaster user {described.wearer_username or 'unknown'}. The pairing {state}."

    # ------------------------------------------------------------------ owner

    def _owner_command(self, text: str) -> str | None:
        parts = text.strip().split()
        if not parts or parts[0].lower() != "invite":
            return None
        if self._admin is None or not self._owner:
            return _INVITE_TEXTS[AdminOutcome.OWNER_NOT_CONFIGURED]
        if len(parts) < 3:
            return TEXTS["invite_usage"]
        # The sender is tenant zero's verified Discord identity, i.e. the owner: act as OWNER_USER_ID.
        result = self._admin.invite(actor=self._owner, display_name=" ".join(parts[2:]), discord_id=parts[1])
        return _INVITE_TEXTS.get(result.outcome, "Refused.")

    async def _log_db(self, tenant: Tenant) -> Database | None:
        if tenant.is_tenant_zero:
            return self._zero_db
        found = self._tenant_core(tenant)
        return None if found is None else Database(found[0], core=found[1])


class InvitationNotifier:
    """Delivers pending invitation DMs. A failed delivery is retried on the next pass; a delivered one is marked once."""

    def __init__(self, *, registry: TenantRegistry, send: Callable[[str, str], Awaitable[bool]], clock: Clock) -> None:
        self._registry = registry
        self._send = send
        self._clock = clock

    async def run_once(self) -> int:
        delivered = 0
        for tenant in self._registry.pending_invitations():
            try:
                ok = await self._send(tenant.discord_id, invitation_text(tenant.display_name))
            except Exception:
                logger.exception("Delivering an invitation failed.")
                ok = False
            if ok and self._registry.mark_invite_notified(tenant.id, now=self._clock.now()):
                delivered += 1
        return delivered

    async def run(self, interval_seconds: float = 30.0) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The invitation pass failed; will retry.")
            await asyncio.sleep(interval_seconds)
