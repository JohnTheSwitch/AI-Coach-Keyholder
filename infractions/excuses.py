"""
infractions/excuses.py

Legitimate behaviour is never an Incident (philosophy 3.8, section 6): an Emergency Override, an approved exemption and a vacation each excuse everything that
overlaps them. Asked again right before an Incident is registered, so an excuse that appears after the detection still counts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso

__all__ = ["Excuses"]


class Excuses:
    def __init__(self, core: CoreDatabase, *, on_vacation: Callable[[str, datetime, datetime], bool] | None = None) -> None:
        self._core = core
        self._on_vacation = on_vacation

    def reason(self, user_id: str, start: datetime, end: datetime, *, ignore_exemption: str | None = None) -> str | None:
        """Why `[start, end]` needs no judgment, or None. A failed read excuses: a doubt never counts against the wearer (2.12)."""
        try:
            with self._core.transaction() as tx:
                emergency = tx.fetch_one("SELECT 1 FROM freeze_periods WHERE reason = 'emergency_override' AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)",
                                         (_iso(end), _iso(start)))
                exemption = tx.fetch_one("SELECT 1 FROM wear_exemptions WHERE user_id = ? AND decided_at IS NOT NULL AND decided_at <= ? AND status IN ('approved', 'ended') "
                                         "AND expires_at IS NOT NULL AND COALESCE(ended_at, expires_at) >= ? AND id != ?", (user_id, _iso(end), _iso(start), ignore_exemption or ""))
            if emergency is not None:
                return "emergency_override"
            if exemption is not None:
                return "exemption"
            if self._on_vacation is not None and self._on_vacation(user_id, start, end):
                return "vacation"
            return None
        except Exception:
            return "unreadable"
