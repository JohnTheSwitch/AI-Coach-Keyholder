"""
tenancy/models.py

Plain values of the tenancy layer (v0.39 Phase E1). A tenant is one managed person with their own isolated SQLite database; `tenant_0` is the
existing system (the owner), registered unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = ["TenantStatus", "RemovedBy", "Tenant", "TENANT_ZERO_ID", "DEFAULT_TENANT_CAP"]

TENANT_ZERO_ID = "tenant_0"
DEFAULT_TENANT_CAP = 5            # counts every non-removed tenant, tenant zero included


class TenantStatus(StrEnum):
    INVITED = "invited"           # the keyholder invited the person; nothing is managed yet
    ACTIVE = "active"             # the wearer accepted; management and polling are allowed
    SUSPENDED = "suspended"       # paused by the keyholder: management and polling are off
    REMOVED = "removed"           # terminal: removed by the keyholder or the wearer left


class RemovedBy(StrEnum):
    KEYHOLDER = "keyholder"
    WEARER = "wearer"


@dataclass(frozen=True, kw_only=True)
class Tenant:
    id: str
    display_name: str
    discord_id: str | None
    chaster_account_id: str | None
    lock_id: str | None
    db_file: str
    status: TenantStatus
    is_tenant_zero: bool
    invited_at: datetime | None
    accepted_at: datetime | None
    removed_at: datetime | None
    removed_by: RemovedBy | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_managed(self) -> bool:
        """Management, polling and execution are allowed only for an ACTIVE tenant."""
        return self.status is TenantStatus.ACTIVE
