"""
release_engine/conditioning.py

The wearer's own switch for Progressive Conditioning (v0.39 Phase F2; table `conditioning_preferences`, migration 036).

Accepting the invitation is consent to Progressive Conditioning (docs/architecture/phase_f_proposal.md 5.1), so a user with no row is ON. `conditioning off`
stores OFF: from then on the hidden factor never increases; it stays at its current value (it is not reset). Phase F4 (the factor itself) reads `is_enabled`.
Nothing here reveals or stores the factor.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso

__all__ = ["ConditioningPreferences"]


class ConditioningPreferences:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def is_enabled(self, user_id: str) -> bool:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT enabled FROM conditioning_preferences WHERE user_id = ?", (user_id,))
        return True if row is None else bool(row["enabled"])

    def set_enabled(self, user_id: str, enabled: bool, *, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO conditioning_preferences (user_id, enabled, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
                       (user_id, int(bool(enabled)), _iso(now)))

        apply_transition(self._core, write=write)
