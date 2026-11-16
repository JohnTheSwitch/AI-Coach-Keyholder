"""
opt_outs/service.py

Gap fix G3 (owner, 2026-11-15): "the check-ins turning off after 24h shouldn't be automatic and should ask after 24h if I really wanna do it."

While the wearer is locked (fails closed: not KNOWN to be unlocked counts as locked), switching off one of the training switches is not done at
once. The request waits COOLING; then the keyholder asks "Do you still want it off?" with two buttons, and only "Yes, switch it off" applies it.
"Keep it on", or no answer within ANSWER_WINDOW, leaves it on. Switching it back on while a request waits withdraws the request.

Covered switches (SWITCHES): photo check-ins, Progressive Conditioning, the weekly review. Owner, 2026-11-16: removing a piece from the
gear list too (key `gear:<piece>`, registered with `register_gear`): "gear remove shouldn't work instantly". Never covered, always immediate: the runtime halt,
leaving, health, the emergency option, and research sharing (a privacy right: `research off` also asks for erasure).

Table `opt_out_requests` (migration 074), written only here. Events: `opt_outs.requested`, `opt_outs.asked`, `opt_outs.decided`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from application.models import ActionSpec
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.opt_outs")

__all__ = ["OptOuts", "Switch", "SWITCHES", "COOLING", "ANSWER_WINDOW"]

COOLING = timedelta(hours=24)
ANSWER_WINDOW = timedelta(hours=24)
SWITCHES = {"photo_check_ins": "photo check-ins", "conditioning": "Progressive Conditioning", "weekly_review": "the weekly review"}


GEAR_PREFIX = "gear:"


def _gear(key: str) -> bool:
    return key.startswith(GEAR_PREFIX)


@dataclass(frozen=True)
class Switch:
    key: str
    apply_off: Callable[[str, datetime], str]          # (user_id, now) -> the reply after switching it off


class OptOuts:
    def __init__(self, core: CoreDatabase, *, locked: Callable[[str], bool], local_clock: Callable[[datetime], str]) -> None:
        """`locked(user_id)`: True unless the wearer is KNOWN to be unlocked (fails closed)."""
        self._core = core
        self._locked = locked
        self._clock = local_clock
        self.switches: dict[str, Switch] = {}
        self._gear_apply: Callable[[str, str, datetime], str] | None = None      # (user_id, piece, now) -> the reply after removing it
        self._gear_label: Callable[[str], str] = lambda piece: piece

    def register(self, key: str, apply_off: Callable[[str, datetime], str]) -> None:
        assert key in SWITCHES
        self.switches[key] = Switch(key, apply_off)

    def register_gear(self, apply_remove: Callable[[str, str, datetime], str], label: Callable[[str], str]) -> None:
        """Owner, 2026-11-16: `gear remove <piece>` waits like a switch-off while locked."""
        self._gear_apply, self._gear_label = apply_remove, label

    def _covered(self, key: str) -> bool:
        return (self._gear_apply is not None) if _gear(key) else key in self.switches

    def _name(self, key: str) -> str:
        return f"the {self._gear_label(key[len(GEAR_PREFIX):])} from your gear list" if _gear(key) else SWITCHES[key]

    def _apply(self, user_id: str, key: str, now: datetime) -> str:
        if _gear(key):
            return self._gear_apply(user_id, key[len(GEAR_PREFIX):], now) if self._gear_apply is not None else "Removed."
        switch = self.switches.get(key)
        return switch.apply_off(user_id, now) if switch is not None else "Switched off."

    # ------------------------------------------------------------------ the command

    def request_off(self, user_id: str, key: str, now: datetime) -> str | None:
        """None when it applies at once (unlocked or not covered); otherwise the reply saying it waits."""
        if not self._covered(key) or not self._locked(user_id):
            return None
        verb, still = ("remove", "removed") if _gear(key) else ("switch off", "off")
        pending = self._pending(user_id, key)
        if pending is not None:
            return (f"You already asked to {verb} {self._name(key)}. I'll ask you on {self._clock(_parse_iso(pending['ask_at']))} whether you "
                    f"still want it {still}.")
        ask_at = now + COOLING
        row_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO opt_out_requests (id, user_id, switch, status, requested_at, ask_at) VALUES (?, ?, ?, 'waiting', ?, ?)",
                       (row_id, user_id, key, _iso(now), _iso(ask_at)))
            write_event(tx, DomainEvent(event_type="opt_outs.requested", source_module="opt_outs", occurred_at=now,
                                        payload={"opt_out_id": row_id, "switch": key}))

        apply_transition(self._core, write=write)
        decision("opt_outs", "A switch-off while locked waits 24 hours, then is asked about.", switch=key)
        if _gear(key):
            return (f"Noted. While you're locked, removing {self._name(key)} isn't done at once: on {self._clock(ask_at)} I'll ask whether "
                    f"you still want it removed, and only a yes removes it. Until then tasks may still use it. If it broke or you lost it, "
                    f"say so: `gear remove <piece> broken` or `gear remove <piece> lost` removes it at once.")
        return (f"Noted. While you're locked, switching off {SWITCHES[key]} isn't done at once: on {self._clock(ask_at)} I'll ask whether you "
                f"still want it off, and only a yes switches it off. Until then it stays on. Health, safety, `leave` and the emergency option "
                f"are never delayed.")

    def withdraw(self, user_id: str, key: str, now: datetime) -> bool:
        """Switching it back on withdraws a waiting request."""
        return self._close_waiting(user_id, key, "withdrawn", now)

    # ------------------------------------------------------------------ the question

    def tick(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            due = [dict(r) for r in tx.fetch_all("SELECT * FROM opt_out_requests WHERE status = 'waiting' AND ask_at <= ?", (_iso(now),))]
            stale = [dict(r) for r in tx.fetch_all("SELECT * FROM opt_out_requests WHERE status = 'asked' AND asked_at <= ?",
                                                   (_iso(now - ANSWER_WINDOW),))]
        for row in due:
            self._ask(row, now)
        for row in stale:
            self._decide(row, "lapsed", now)

    def _ask(self, row: dict, now: datetime) -> None:
        if _gear(row["switch"]):
            text = (f"24 hours ago you asked to remove {self._name(row['switch'])}. Do you still want it removed? If I don't hear from you, "
                    f"it stays on the list.")
            labels = ("Yes, remove it", "Keep it")
        else:
            text = (f"24 hours ago you asked to switch off {SWITCHES[row['switch']]}. Do you still want it off? If I don't hear from you, "
                    f"it stays on.")
            labels = ("Yes, switch it off", "Keep it on")
        actions = (ActionSpec(action_id=f"optout:yes:{row['id']}", label=labels[0]),
                   ActionSpec(action_id=f"optout:no:{row['id']}", label=labels[1], style="primary"))

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE opt_out_requests SET status = 'asked', asked_at = ? WHERE id = ? AND status = 'waiting'",
                              (_iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="opt_outs.asked", source_module="opt_outs", occurred_at=now,
                                            payload={"opt_out_id": row["id"], "switch": row["switch"]}))
                enqueue_in_transaction(tx, user_id=row["user_id"], kind="opt_out", dedupe_key=f"optout:ask:{row['id']}", now=now, text=text, actions=actions)
            return bool(done)

        apply_transition(self._core, write=write)

    def press(self, *, user_id: str, row_id: str, action: str, now: datetime) -> str:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM opt_out_requests WHERE id = ?", (row_id,))
        if row is None or row["user_id"] != user_id:
            return "That isn't yours to decide."
        if action not in ("yes", "no"):
            return "That isn't one of the options."
        row = dict(row)
        if row["status"] != "asked":
            return "That was already decided."
        if action == "no":
            self._decide(row, "kept_on", now)
            if _gear(row["switch"]):
                return f"Good. The {self._gear_label(row['switch'][len(GEAR_PREFIX):])} stays on your gear list."
            return f"Good. {SWITCHES[row['switch']].capitalize()} stays on."
        if not self._decide(row, "applied", now):
            return "That was already decided."
        return self._apply(user_id, row["switch"], now)

    # ------------------------------------------------------------------ internals

    def _pending(self, user_id: str, key: str) -> dict | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM opt_out_requests WHERE user_id = ? AND switch = ? AND status IN ('waiting', 'asked')", (user_id, key))
        return None if row is None else dict(row)

    def _close_waiting(self, user_id: str, key: str, status: str, now: datetime) -> bool:
        row = self._pending(user_id, key)
        return row is not None and self._decide(row, status, now)

    def _decide(self, row: dict, status: str, now: datetime) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE opt_out_requests SET status = ?, decided_at = ? WHERE id = ? AND status IN ('waiting', 'asked')",
                              (status, _iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="opt_outs.decided", source_module="opt_outs", occurred_at=now,
                                            payload={"opt_out_id": row["id"], "switch": row["switch"], "status": status}))
            return bool(done)

        done = apply_transition(self._core, write=write)
        if done:
            decision("opt_outs", f"A switch-off request ended: {status}.", switch=row["switch"])
        return bool(done)

    async def loop(self, clock, *, interval_seconds: float = 300.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.tick, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The opt-out pass failed; it continues.")
            await asyncio.sleep(interval_seconds)
