"""
tenancy/services.py

PlatformServices (v0.39 Phase E1): opens the platform database, registers the existing system as `tenant_0` (without touching its database) and exposes the
registry, the router, the global halt and the SHARED keyholder token manager. The keyholder (bot) Chaster connection stays where it is today, in tenant zero's
database: Chaster refresh tokens rotate, so there must be exactly one owner of that connection, and every tenant uses it through this one manager.

Nothing starts by itself: with ENABLE_MULTI_WEARER off none of this is built and the running system is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from tenancy.halt import PlatformHalt
from tenancy.models import DEFAULT_TENANT_CAP
from tenancy.platform_db import open_platform_db
from tenancy.registry import TenantRegistry
from tenancy.router import TenantRouter

logger = logging.getLogger("ai_coach_keyholder.tenancy")

__all__ = ["PlatformServices", "open_platform_services"]


@dataclass
class PlatformServices:
    core: CoreDatabase
    registry: TenantRegistry
    router: TenantRouter
    halt: PlatformHalt
    tenant_dir: Path
    keyholder_token_manager: object | None = None       # the ONE ChasterTokenManager of the bot account (set by the composition root)
    notes: list[str] = field(default_factory=list)


def _owner_discord_id(main_core: CoreDatabase, owner_user_id: str | None) -> str | None:
    if not owner_user_id:
        return None
    try:
        with main_core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT external_id FROM user_channel_identities WHERE user_account_id = ? AND channel = 'discord'", (owner_user_id,))
        return None if row is None else row["external_id"]
    except Exception:
        return None


def open_platform_services(*, platform_db_path: str | Path, main_db_path: str | Path, main_core: CoreDatabase, owner_user_id: str | None,
                           clock: Clock, cap: int = DEFAULT_TENANT_CAP) -> PlatformServices:
    core = open_platform_db(platform_db_path)
    registry = TenantRegistry(core, cap=cap)
    discord_id = _owner_discord_id(main_core, owner_user_id)
    try:
        registry.bootstrap_tenant_zero(db_file=main_db_path, discord_id=discord_id, now=clock.now())
    except Exception:
        # e.g. the owner's Discord id is already used by another tenant: tenant zero still exists, just without it.
        logger.exception("Could not fill in tenant zero's Discord id.")
        registry.bootstrap_tenant_zero(db_file=main_db_path, now=clock.now())
    services = PlatformServices(core=core, registry=registry, router=TenantRouter(registry), halt=PlatformHalt(core),
                                tenant_dir=Path(main_db_path).parent / "tenants")
    services.notes.append(f"Multi-wearer platform ready (tenant cap {cap}); tenant_0 is the existing system.")
    return services
