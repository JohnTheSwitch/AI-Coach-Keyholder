"""
tenancy/router.py

TenantRouter (v0.39 Phase E1): the pure lookup that says what a message from a Discord id means. It decides nothing about content and calls nothing;
the Discord wiring that uses it arrives in E3. A removed tenant is indistinguishable from a stranger (no information about past membership leaks).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tenancy.models import Tenant, TenantStatus
from tenancy.registry import TenantRegistry

__all__ = ["TenantRouter", "RouteKind", "TenantRoute"]


class RouteKind(StrEnum):
    ACTIVE = "active"            # hand the message to this tenant
    INVITED = "invited"          # only the acceptance (or a decline/leave) is meaningful
    SUSPENDED = "suspended"      # paused by the keyholder: a fixed notice, nothing is processed
    UNKNOWN = "unknown"          # not a tenant (or removed): a fixed refusal


@dataclass(frozen=True)
class TenantRoute:
    kind: RouteKind
    tenant: Tenant | None = None


class TenantRouter:
    def __init__(self, registry: TenantRegistry) -> None:
        self._registry = registry

    def route_discord(self, discord_id: str) -> TenantRoute:
        tenant = self._registry.find_by_discord_id(str(discord_id))
        return self._route(tenant)

    def route_chaster_account(self, chaster_account_id: str) -> TenantRoute:
        return self._route(self._registry.find_by_chaster_account(chaster_account_id))

    @staticmethod
    def _route(tenant: Tenant | None) -> TenantRoute:
        if tenant is None or tenant.status is TenantStatus.REMOVED:
            return TenantRoute(RouteKind.UNKNOWN)
        kind = {TenantStatus.ACTIVE: RouteKind.ACTIVE, TenantStatus.INVITED: RouteKind.INVITED,
                TenantStatus.SUSPENDED: RouteKind.SUSPENDED}[tenant.status]
        return TenantRoute(kind, tenant)
