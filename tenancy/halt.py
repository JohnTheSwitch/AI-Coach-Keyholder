"""
tenancy/halt.py

The global kill switch and its combination with a tenant's own halt (v0.39 Phase E1).

`PlatformHalt` is one row in the platform database that stops execution for EVERY tenant. `CombinedHalt` is what a tenant's executor and release tick read
(`is_halted()` is true when EITHER the platform halt or the tenant's own halt is set). Both fail closed: an unreadable switch counts as halted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso

__all__ = ["PlatformHalt", "CombinedHalt", "HaltSource"]


class HaltSource(Protocol):
    def is_halted(self) -> bool: ...


class PlatformHalt:
    def __init__(self, core: CoreDatabase) -> None:
        self._core = core

    def is_halted(self) -> bool:
        try:
            with self._core.transaction() as tx:
                row = tx.fetch_one("SELECT halted FROM platform_halt WHERE id = 1")
            return True if row is None else bool(row["halted"])
        except Exception:
            return True

    def reason(self) -> str | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT reason FROM platform_halt WHERE id = 1")
        return None if row is None else row["reason"]

    def set_halted(self, halted: bool, *, reason: str, actor: str, now: datetime) -> None:
        if not reason or not reason.strip():
            raise ValueError("reason must be non-empty")
        import uuid
        with self._core.transaction() as tx:
            tx.execute(
                "INSERT INTO platform_halt (id, halted, reason, updated_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET halted = excluded.halted, reason = excluded.reason, updated_at = excluded.updated_at",
                (int(bool(halted)), reason.strip()[:200], _iso(now)))
            tx.execute("INSERT INTO tenant_events (id, tenant_id, event, actor, detail, created_at) VALUES (?, NULL, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), "platform_halt" if halted else "platform_resume", (actor or "")[:64], reason.strip()[:200], _iso(now)))


class CombinedHalt:
    """True when the platform halt OR the tenant's own halt is set."""

    def __init__(self, platform: HaltSource, tenant: HaltSource) -> None:
        self._platform = platform
        self._tenant = tenant

    def is_halted(self) -> bool:
        return bool(self._platform.is_halted()) or bool(self._tenant.is_halted())
