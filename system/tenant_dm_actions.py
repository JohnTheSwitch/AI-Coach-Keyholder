"""
system/tenant_dm_actions.py

Multi-wearer parity (owner, 2026-11-14): a DM button pressed by an additional wearer belongs to THAT wearer's runtime, never to the owner's
facade (which would answer "That isn't yours" at best). `TenantAwareActions` keeps the owner's facades and, for an active tenant's wearer,
hands the press to the facade of the same scope in the tenant's own runtime (`TenantRuntime.dm_actions`). A scope a tenant does not have yet
says so plainly; nothing from one tenant ever reaches another.
"""

from __future__ import annotations

import logging

from application.models import OutgoingMessage
from tenancy.router import RouteKind, TenantRouter

logger = logging.getLogger("ai_coach_keyholder.system.tenant_dm_actions")

__all__ = ["TenantAwareActions", "NOT_YET"]

NOT_YET = OutgoingMessage(text="That isn't available for you yet.")


class _Routed:
    def __init__(self, scope: str, actions: "TenantAwareActions") -> None:
        self._scope = scope
        self._actions = actions

    def press(self, *, discord_user_id: str, action: str, ident: str):
        facade = self._actions.facade_for(self._scope, discord_user_id)
        if facade is None:
            return NOT_YET
        return facade.press(discord_user_id=discord_user_id, action=action, ident=ident)


class TenantAwareActions(dict):
    """A dict of the owner's facades (so `bot.dm_actions[scope] = ...` keeps working) whose `get` routes by the presser."""

    def __init__(self, owner: dict, *, router: TenantRouter, supervisor) -> None:
        super().__init__(owner)
        self._router = router
        self._supervisor = supervisor

    def get(self, scope, default=None):
        return _Routed(scope, self)

    def facade_for(self, scope: str, discord_user_id: str):
        route = self._router.route_discord(str(discord_user_id))
        tenant = route.tenant
        if route.kind is RouteKind.UNKNOWN or tenant is None or tenant.is_tenant_zero:
            return dict.get(self, scope)
        if route.kind is not RouteKind.ACTIVE:
            return None                                    # invited or suspended: nothing to press
        runtime = self._supervisor.runtimes.get(tenant.id)
        if runtime is None or runtime.tenant_id != tenant.id:
            return None
        return getattr(runtime, "dm_actions", {}).get(scope)
