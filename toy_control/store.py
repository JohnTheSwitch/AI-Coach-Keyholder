"""
toy_control/store.py

His own level for the toy (owner, 2026-11-17: "make it more intelligently done"): each session is recorded with the level it was played at,
and his "Too weak / Right / Too much" after it sets the level the next session starts from. "Right" still climbs a little, so it moves up
over the weeks like the plug ladder; no answer keeps the level. The only writer of `toy_sessions` (migration 092).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso

__all__ = ["ToyLevels", "START_LEVEL", "MIN_LEVEL", "MAX_LEVEL", "STEPS", "FEEDBACK"]

START_LEVEL = 0.5
MIN_LEVEL, MAX_LEVEL = 0.2, 1.0
STEPS = {"too_weak": 0.1, "right": 0.02, "too_much": -0.1}
FEEDBACK = {"toyweak": "too_weak", "toyright": "right", "toymuch": "too_much"}


class ToyLevels:
    def __init__(self, core: CoreDatabase, *, user_id: str) -> None:
        self._core = core
        self._user = user_id

    def level(self) -> float:
        """The level the next session is played at."""
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT base_level, next_level FROM toy_sessions WHERE user_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                               (self._user,))
        if row is None:
            return START_LEVEL
        return float(row["next_level"] if row["next_level"] is not None else row["base_level"])

    def record_start(self, *, task_ref: str, mode: str, profile: str, level: float, now: datetime) -> str:
        session_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO toy_sessions (id, user_id, task_ref, mode, profile, base_level, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (session_id, self._user, task_ref, mode, profile, level, _iso(now)))
            write_event(tx, DomainEvent(event_type="toy_control.session_started", source_module="toy_control", occurred_at=now,
                                        payload={"toy_session_id": session_id, "task_ref": task_ref, "mode": mode, "profile": profile}))

        apply_transition(self._core, write=write)
        return session_id

    def feedback(self, *, task_ref: str, answer: str, now: datetime) -> float | None:
        """The newest session of `task_ref` without feedback gets `answer`; returns the next level, or None when there is none to answer."""
        step = STEPS[answer]

        def write(tx: Transaction, _s: object) -> float | None:
            row = tx.fetch_one("SELECT id, base_level FROM toy_sessions WHERE user_id = ? AND task_ref = ? AND feedback IS NULL "
                               "ORDER BY started_at DESC, rowid DESC LIMIT 1", (self._user, task_ref))
            if row is None:
                return None
            nxt = round(max(MIN_LEVEL, min(MAX_LEVEL, float(row["base_level"]) + step)), 2)
            tx.execute("UPDATE toy_sessions SET feedback = ?, feedback_at = ?, next_level = ? WHERE id = ? AND feedback IS NULL",
                       (answer, _iso(now), nxt, row["id"]))
            write_event(tx, DomainEvent(event_type="toy_control.feedback_recorded", source_module="toy_control", occurred_at=now,
                                        payload={"toy_session_id": row["id"], "feedback": answer}))
            return nxt

        return apply_transition(self._core, write=write)
