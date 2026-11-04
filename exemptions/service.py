"""
exemptions/service.py

The Temporary Wear Exemption workflow (philosophy 3.6), from the wearer's first message to the Penalty Engine's existing `freeze()`:

    "I need an exemption" -> reason button -> length button -> the keyholder's card -> Approve -> `freeze(temporary_wear_exemption, expires_at)`

Everything after that already existed and is reused: `penalty_window.frozen` -> `rules_engine/penalty_hold.py` -> an unfreeze intent -> the penalty barrier, under the
runtime halt. When the exemption ends, by time or by the wearer's "I'm back", the window's `resumed` event raises the barrier again. No code here touches
Chaster, and no hour is added or removed anywhere: a pause is a Penalty Window freeze, and the redemption hold stays on (Zero-Time-Modification).

**Who decides.** The keyholder, by button (3.2, and 2.5: a person decides, a model never does). `decide()` reads the request and nothing else: it cannot see whether a
Penalty Window exists (invariant I7), so a request is treated the same inside and outside one. Applying the decision is a separate step, `tick()`.

**Not a gate on safety.** This is for planned life. A health or safety need never waits for this (3.9, 3.10), and every message here says so.

An exemption approved while no window runs is simply recorded as in force, and `tick()` pauses a window that starts during it.
"""

from __future__ import annotations

import hmac
import logging
import re
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from application.models import ActionSpec, OutgoingMessage
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso
from notifications.queue import enqueue_in_transaction
from penalty_engine.models import FreezeReason

logger = logging.getLogger("ai_coach_keyholder.exemptions")

__all__ = ["ExemptionService", "ExemptionDecision", "is_exemption_request", "CATEGORIES", "DURATIONS", "SAFETY_LINE"]

CATEGORIES = {"family": "Family event", "social": "Social occasion", "work_school": "Work or school", "health": "Health", "travel": "Travel", "other": "Something else"}
DURATIONS = (6, 24, 72, 168)                # hours; a longer absence is what a vacation is for
SAFETY_LINE = "If this is an urgent health or safety matter, you don't need this: do what keeps you safe, and it will never be held against you."

_REQUEST = re.compile(r"\b(exemption|exempt me|v[yý]jimk[auy]|v[yý]jimku)\b", re.IGNORECASE)


def is_exemption_request(message: str) -> bool:
    """Deterministic, never a model's reading: the word itself opens the buttons, and a button is what commits to anything."""
    return isinstance(message, str) and len(message) <= 300 and _REQUEST.search(message) is not None


class ExemptionDecision(StrEnum):
    APPROVED = "approved"
    DECLINED = "declined"
    NOT_OWNER = "not_owner"
    NOT_PENDING = "not_pending"


class Penalties(Protocol):
    def get_active_or_frozen_penalty_window(self): ...
    def freeze(self, penalty_window_id: str, reason: FreezeReason, *, now: datetime, exemption_id: str | None = None, expires_at: datetime | None = None): ...
    def resume(self, penalty_window_id: str, reason: FreezeReason, *, now: datetime) -> None: ...


def _hours(value: int) -> str:
    return f"{value // 24} day{'s' if value >= 48 else ''}" if value >= 24 else f"{value} hours"


class ExemptionService:
    def __init__(self, db_path: str | Path | None = None, *, core: CoreDatabase | None = None, owner_user_id: str | None, penalties: Penalties) -> None:
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._owner = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None
        self._penalties = penalties

    # ------------------------------------------------------------------ the wearer

    def start(self, user_id: str, now: datetime) -> OutgoingMessage:
        def write(tx: Transaction, _s: object) -> tuple[str, str | None]:
            row = tx.fetch_one("SELECT id, status FROM wear_exemptions WHERE user_id = ? AND status IN ('requested', 'approved')", (user_id,))
            if row is not None:
                return row["status"], row["id"]
            tx.execute("DELETE FROM wear_exemptions WHERE user_id = ? AND status = 'draft'", (user_id,))     # an abandoned draft holds nothing
            new_id = str(uuid4())
            tx.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at) VALUES (?, ?, 'draft', ?)", (new_id, user_id, _iso(now)))
            return "draft", new_id

        status, ident = apply_transition(self._core, write=write)
        if status == "requested":
            return OutgoingMessage(text=f"Your exemption request is with your keyholder. {SAFETY_LINE}")
        if status == "approved":
            return self._running_card(ident)
        return OutgoingMessage(text=f"Of course. What is it for? {SAFETY_LINE}",
                               actions=tuple(ActionSpec(action_id=f"exemption:cat_{code}:{ident}", label=label) for code, label in CATEGORIES.items()))

    def choose_category(self, user_id: str, exemption_id: str, code: str, now: datetime) -> OutgoingMessage:
        if code not in CATEGORIES:
            return OutgoingMessage(text="That isn't one of the options.")

        def write(tx: Transaction, _s: object) -> bool:
            return tx.execute("UPDATE wear_exemptions SET category = ? WHERE id = ? AND user_id = ? AND status = 'draft'", (code, exemption_id, user_id)).rowcount == 1

        if not apply_transition(self._core, write=write):
            return OutgoingMessage(text="That request is no longer open. Just ask again if you still need one.")
        return OutgoingMessage(text="How long do you need?",
                               actions=tuple(ActionSpec(action_id=f"exemption:h{hours}:{exemption_id}", label=_hours(hours)) for hours in DURATIONS))

    def choose_hours(self, user_id: str, exemption_id: str, hours: int, now: datetime) -> OutgoingMessage:
        if hours not in DURATIONS:
            return OutgoingMessage(text="That isn't one of the options.")

        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT category FROM wear_exemptions WHERE id = ? AND user_id = ? AND status = 'draft' AND category IS NOT NULL", (exemption_id, user_id))
            if row is None:
                return False
            tx.execute("UPDATE wear_exemptions SET hours = ?, status = 'requested', requested_at = ? WHERE id = ?", (hours, _iso(now), exemption_id))
            if self._owner:
                enqueue_in_transaction(
                    tx, user_id=self._owner, kind="exemption_request", dedupe_key=f"exemption:request:{exemption_id}", now=now, immediate=True,
                    text=f"Exemption request: {CATEGORIES[row['category']]}, {_hours(hours)}. Decide on what it's for, and nothing else.",
                    actions=(ActionSpec(action_id=f"exemption:approve:{exemption_id}", label="Approve", style="success"),
                             ActionSpec(action_id=f"exemption:decline:{exemption_id}", label="Decline", style="secondary")))
            return True

        if not apply_transition(self._core, write=write):
            return OutgoingMessage(text="That request is no longer open. Just ask again if you still need one.")
        return OutgoingMessage(text=f"Sent to your keyholder. I'll tell you as soon as they answer. {SAFETY_LINE}")

    def end_early(self, user_id: str, exemption_id: str, now: datetime) -> OutgoingMessage:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM wear_exemptions WHERE id = ? AND user_id = ? AND status = 'approved'", (exemption_id, user_id))
        if row is None:
            return OutgoingMessage(text="That exemption is already over.")
        self._end(row, now, "wearer_returned")
        return OutgoingMessage(text="Welcome back. Your exemption has ended.")

    # ------------------------------------------------------------------ the keyholder

    def decide(self, *, actor: str, exemption_id: str, approve: bool, now: datetime) -> ExemptionDecision:
        """Reads only the request: no Penalty Window, no Trust, no history (I7). The Penalty Window is touched afterwards by `tick()`, and only on an approval."""
        if self._owner is None or not isinstance(actor, str) or not hmac.compare_digest(actor.encode("utf-8"), self._owner.encode("utf-8")):
            return ExemptionDecision.NOT_OWNER
        decision = ExemptionDecision.APPROVED if approve else ExemptionDecision.DECLINED

        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT * FROM wear_exemptions WHERE id = ? AND status = 'requested'", (exemption_id,))
            if row is None:
                return False
            expires = now + timedelta(hours=row["hours"])
            tx.execute("UPDATE wear_exemptions SET status = ?, decided_at = ?, decided_by = ?, expires_at = ? WHERE id = ?",
                       (decision.value, _iso(now), actor, _iso(expires) if approve else None, exemption_id))
            if approve:
                enqueue_in_transaction(
                    tx, user_id=row["user_id"], kind="exemption_decision", dedupe_key=f"exemption:decision:{exemption_id}", now=now, immediate=True,
                    text=f"Your exemption is approved for {_hours(row['hours'])}. A Penalty Window, if there is one, is paused for that time and continues where it "
                         "left off. Any penalty freeze on your lock is lifted as soon as the system is allowed to act. Tell me when you're back, or it simply ends on time.",
                    actions=(ActionSpec(action_id=f"exemption:end:{exemption_id}", label="I'm back", style="primary"),))
            else:
                enqueue_in_transaction(tx, user_id=row["user_id"], kind="exemption_decision", dedupe_key=f"exemption:decision:{exemption_id}", now=now, immediate=True,
                                       text=f"Your exemption wasn't approved this time. {SAFETY_LINE}")
            return True

        if not apply_transition(self._core, write=write):
            return ExemptionDecision.NOT_PENDING
        if approve:
            self.tick(now)
        return decision

    # ------------------------------------------------------------------ the periodic pass

    def tick(self, now: datetime) -> None:
        """Brings the Penalty Window in line with the exemptions in force. Idempotent and never raises: a failure is retried on the next pass."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM wear_exemptions WHERE status = 'approved'")
        for row in rows:
            try:
                if parse_iso(row["expires_at"]) <= now:
                    self._end(row, now, "expired")
                else:
                    self._apply(row, now)
            except Exception:
                logger.exception("An exemption could not be applied; retrying on the next pass.")

    def in_force(self, user_id: str) -> str | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT id FROM wear_exemptions WHERE user_id = ? AND status = 'approved'", (user_id,))
        return None if row is None else row["id"]

    # ------------------------------------------------------------------ internals

    def _apply(self, row, now: datetime) -> None:
        window = self._penalties.get_active_or_frozen_penalty_window()
        if window is None:
            return
        with self._core.transaction() as tx:
            done = tx.fetch_one("SELECT 1 FROM freeze_periods WHERE penalty_window_id = ? AND exemption_id = ?", (window.id, row["id"]))
        if done is None:
            self._penalties.freeze(window.id, FreezeReason.TEMPORARY_WEAR_EXEMPTION, now=now, exemption_id=row["id"], expires_at=parse_iso(row["expires_at"]))

    def _end(self, row, now: datetime, reason: str) -> None:
        with self._core.transaction() as tx:
            open_freeze = tx.fetch_one("SELECT penalty_window_id FROM freeze_periods WHERE exemption_id = ? AND ended_at IS NULL", (row["id"],))
        if open_freeze is not None:
            self._penalties.resume(open_freeze["penalty_window_id"], FreezeReason.TEMPORARY_WEAR_EXEMPTION, now=now)

        def write(tx: Transaction, _s: object) -> None:
            cursor = tx.execute("UPDATE wear_exemptions SET status = 'ended', ended_at = ?, end_reason = ? WHERE id = ? AND status = 'approved'", (_iso(now), reason, row["id"]))
            if cursor.rowcount == 1 and reason == "expired":
                enqueue_in_transaction(tx, user_id=row["user_id"], kind="exemption_ended", dedupe_key=f"exemption:ended:{row['id']}", now=now, immediate=True,
                                       text="Your exemption has ended. Welcome back.")

        apply_transition(self._core, write=write)

    def _running_card(self, exemption_id: str) -> OutgoingMessage:
        return OutgoingMessage(text="You already have an exemption running. Tell me when you're back, or it simply ends on time.",
                               actions=(ActionSpec(action_id=f"exemption:end:{exemption_id}", label="I'm back", style="primary"),))
