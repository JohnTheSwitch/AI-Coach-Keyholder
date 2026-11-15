"""
settings_watch/service.py

Gap fix G4 (owner, 2026-11-15): "settings changes are visible". The wearer controls the bot's settings (quiet hours, workday, time zone,
the runtime halt), so changing them must not be a silent way out. This module remembers the last values it saw; when one differs (a restart
with changed settings, or the halt switched on or off), it records the change, asks the wearer about it in the DM (free answer, never a
consequence by itself) and lets the weekly review list the week's changes. The first sighting of a key is only remembered.

Tables (migration 075), written only here: `settings_seen` (key, value, seen_at), `settings_changes` (id, user_id, key, old, new, changed_at).
Event: `settings_watch.changed`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.settings_watch")

__all__ = ["SettingsWatch", "LABELS", "config_values"]

LABELS = {"time_zone": "your time zone", "quiet_hours": "your quiet hours", "workday": "your workday hours", "holidays": "your holiday country",
          "weekend_release": "the weekend release start", "halt": "the runtime halt"}


def config_values(config) -> dict[str, str]:
    """The watched settings that come from the bot's configuration (they change only with a restart)."""
    return {
        "time_zone": str(getattr(config, "app_timezone", "") or ""),
        "quiet_hours": f"{getattr(config, 'quiet_hours_start', '')}-{getattr(config, 'quiet_hours_end', '')}",
        "workday": f"{getattr(config, 'workday_start', '')}-{getattr(config, 'workday_end', '')}",
        "holidays": str(getattr(config, "holiday_country", "") or ""),
        "weekend_release": str(getattr(config, "weekend_release_start", "") or ""),
    }


class SettingsWatch:
    def __init__(self, core: CoreDatabase, *, user_id: str, values: Callable[[], dict[str, str]]) -> None:
        self._core = core
        self.user_id = user_id
        self._values = values

    def tick(self, now: datetime) -> list[str]:
        """The keys that changed in this pass."""
        current = self._values()
        with self._core.transaction() as tx:
            seen = {r["key"]: r["value"] for r in tx.fetch_all("SELECT key, value FROM settings_seen")}
        changed = [k for k, v in current.items() if k in seen and seen[k] != v]
        fresh = [k for k in current if k not in seen]
        if not changed and not fresh:
            return []

        def write(tx: Transaction, _s: object) -> None:
            for key in fresh + changed:
                tx.execute("INSERT INTO settings_seen (key, value, seen_at) VALUES (?, ?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value, seen_at = excluded.seen_at", (key, current[key], _iso(now)))
            for key in changed:
                change_id = str(uuid.uuid4())
                tx.execute("INSERT INTO settings_changes (id, user_id, key, old, new, changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (change_id, self.user_id, key, seen[key], current[key], _iso(now)))
                write_event(tx, DomainEvent(event_type="settings_watch.changed", source_module="settings_watch", occurred_at=now,
                                            payload={"change_id": change_id, "key": key}))
                enqueue_in_transaction(tx, user_id=self.user_id, kind="settings_change", dedupe_key=f"settings:{change_id}", now=now,
                                       text=self._question(key, seen[key], current[key]))

        apply_transition(self._core, write=write)
        for key in changed:
            decision("settings_watch", "A watched setting changed; the wearer is asked about it.", key=key)
        return changed

    @staticmethod
    def _question(key: str, old: str, new: str) -> str:
        if key == "halt":
            if new.startswith("on"):
                return ("The runtime halt was switched on. While it's on I can't act on your lock. That's your safety switch and always "
                        "allowed, but tell me why in a message.")
            return "The runtime halt was cleared. I can act on your lock again."
        return (f"Since I last looked, {LABELS.get(key, key)} changed from {old} to {new}. My settings are yours to change, but tell me why "
                "in a message.")

    def get_changes_between(self, lo: str, hi: str) -> list[dict]:
        """Read-only: the changes recorded in [lo, hi), oldest first (the weekly review)."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT key, old, new, changed_at FROM settings_changes WHERE user_id = ? AND changed_at >= ? AND changed_at < ? "
                                "ORDER BY changed_at", (self.user_id, lo, hi))
        return [dict(r) for r in rows]

    async def loop(self, clock, *, interval_seconds: float = 300.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.tick, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The settings watch pass failed; it continues.")
            await asyncio.sleep(interval_seconds)
