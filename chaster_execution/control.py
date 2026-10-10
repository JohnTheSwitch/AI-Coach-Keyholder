"""
chaster_execution/control.py

The runtime halt switch (local DB kill switch). Fail-closed: a missing row,
an unreadable row or any error is treated as HALTED. It is changed only by a
local script (scripts/execution_control.py), never from Discord or a model.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso

__all__ = ["ExecutionControl"]


class ExecutionControl:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def is_halted(self) -> bool:
        try:
            with self._core.transaction() as tx:
                row = tx.fetch_one("SELECT halted FROM execution_control WHERE id = 1")
            return True if row is None else bool(row["halted"])
        except Exception:
            return True

    def reason(self) -> str | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT reason FROM execution_control WHERE id = 1")
        return None if row is None else row["reason"]

    def set_halted(self, halted: bool, *, reason: str, now: datetime) -> None:
        if not reason or not reason.strip():
            raise ValueError("reason must be non-empty")
        with self._core.transaction() as tx:
            tx.execute(
                "INSERT INTO execution_control (id, halted, reason, updated_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET halted = excluded.halted, reason = excluded.reason, "
                "updated_at = excluded.updated_at",
                (1 if halted else 0, reason, _iso(now)),
            )
