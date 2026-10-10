"""
release_engine/service.py

Domain core of the Single Barrier release (v0.39, Phase 1: no wiring, no Chaster call).

Flow: tokens are earned (ledger) -> `ensure_plan()` stores a SECRET target time
(never an instant unlock) -> at/after the target `commit_authorization()`
re-validates everything inside one transaction and, only then, SPENDS exactly one
token and records an `authorized` release -> provider progress is recorded as
provider_requested -> provider_confirmed | provider_failed | physical_state_unknown.

Secrecy: `target_at` and `seed` live only in release_plans. No event payload, outcome,
log line or exception text produced here contains a timestamp of the plan.
Authority: every method is a domain-service call; nothing here is reachable from a
model, Discord command or conversation path (AST-guarded).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from release_engine.ledger import LedgerEntryType, balance, insert_entry, reverse_entry
from release_engine.scheduler import plan_target, validate_offsets
from schedule_windows.windows import WindowConfig, WindowKind, classify

__all__ = ["ReleaseEngine", "ReleaseGates", "CommitStatus", "CommitOutcome", "ProviderState", "TOKEN_COST"]

TOKEN_COST = 1     # a Single Barrier release costs exactly one token


class CommitStatus(StrEnum):
    AUTHORIZED = "authorized"
    NO_PLAN = "no_plan"
    NOT_DUE = "not_due"
    RETARGETED = "retargeted"              # due, but the window is closed: a new target was drawn
    VETOED = "vetoed"                      # a hard veto cancelled the plan
    GATE_NOT_MET = "gate_not_met"          # transient (e.g. stale lock data): plan kept, retry later
    INSUFFICIENT_TOKENS = "insufficient_tokens"
    WINDOW_CLOSED = "window_closed"        # backstop only
    ALREADY_OPEN = "already_open"          # a release is already in flight for this user


class ProviderState(StrEnum):
    AUTHORIZED = "authorized"
    PROVIDER_REQUESTED = "provider_requested"
    PROVIDER_CONFIRMED = "provider_confirmed"
    PROVIDER_FAILED = "provider_failed"
    PHYSICAL_STATE_UNKNOWN = "physical_state_unknown"


_OPEN_STATES = (ProviderState.AUTHORIZED.value, ProviderState.PROVIDER_REQUESTED.value,
                ProviderState.PHYSICAL_STATE_UNKNOWN.value)


@dataclass(frozen=True, kw_only=True)
class ReleaseGates:
    """Facts the caller assembles from other modules' public read APIs (this package
    imports none of them)."""
    lock_barrier_confirmed: bool       # a FRESH observation: locked, keyholder lock, isFrozen is True
    penalty_window_active: bool = False
    supervision_active: bool = False
    last_check_in_missed: bool = False

    def hard_veto(self) -> str | None:
        if self.penalty_window_active:
            return "penalty_window_active"
        if self.supervision_active:
            return "supervision_active"
        if self.last_check_in_missed:
            return "check_in_missed"
        return None


@dataclass(frozen=True, kw_only=True)
class CommitOutcome:
    """Deliberately carries no timestamp and no plan data."""
    status: CommitStatus
    authorization_id: str | None = None
    reason: str = ""


class ReleaseEngine:
    def __init__(
        self, db_path: str | Path, *, core: CoreDatabase | None = None, window_config: WindowConfig,
        min_offset_minutes: int, max_offset_minutes: int, max_hold_hours: int,
        seed_factory: Callable[[], bytes] = lambda: os.urandom(32),
    ) -> None:
        if isinstance(max_hold_hours, bool) or not isinstance(max_hold_hours, int) or max_hold_hours < 1:
            raise ValueError("max_hold_hours must be a positive int")
        validate_offsets(window_config, min_offset_minutes, max_offset_minutes)
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._cfg = window_config
        self._lo, self._hi = min_offset_minutes, max_offset_minutes
        self._max_hold = timedelta(hours=max_hold_hours)
        self._seed_factory = seed_factory

    # ------------------------------------------------------------------ ledger

    def earn_tokens(self, *, user_id: str, amount: int, reason_code: str, source_event_id: str | None, now: datetime) -> str | None:
        """EARN `amount`. Idempotent per source_event_id (a redelivered event never earns twice)."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise ValueError("amount must be a positive int")

        def write(tx: Transaction, _s: object) -> str | None:
            if source_event_id is not None and tx.fetch_one(
                "SELECT 1 FROM token_ledger WHERE source_event_id = ? AND entry_type = 'earn'", (source_event_id,)
            ):
                return None
            return insert_entry(tx, user_id=user_id, delta=amount, entry_type=LedgerEntryType.EARN,
                                reason_code=reason_code, now=now, source_event_id=source_event_id)

        return apply_transition(self._core, write=write)

    def get_balance(self, user_id: str) -> int:
        with self._core.transaction() as tx:
            return balance(tx, user_id)

    # ------------------------------------------------------------------ plans

    def ensure_plan(self, *, user_id: str, now: datetime) -> bool:
        """Creates the adaptive plan when the requirement (balance >= 1) is met and no plan
        or release is in flight. Returns True if a plan was created."""
        def write(tx: Transaction, _s: object) -> bool:
            if balance(tx, user_id) < TOKEN_COST:
                return False
            if tx.fetch_one("SELECT 1 FROM release_plans WHERE user_id = ? AND status = 'planned'", (user_id,)):
                return False
            if self._open_authorization(tx, user_id):
                return False
            seed = self._seed_factory()
            target = plan_target(now, seed, self._cfg, self._lo, self._hi)
            if target is None:
                return False
            plan_id = str(uuid.uuid4())
            tx.execute(
                "INSERT INTO release_plans (id, user_id, status, requirement_met_at, target_at, seed, created_at, updated_at) "
                "VALUES (?, ?, 'planned', ?, ?, ?, ?, ?)",
                (plan_id, user_id, _iso(now), _iso(target), seed, _iso(now), _iso(now)),
            )
            self._emit(tx, "release_plan.created", {"plan_id": plan_id}, now)
            return True

        return apply_transition(self._core, write=write)

    def cancel_plan(self, *, user_id: str, reason: str, now: datetime) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            return self._cancel_in_tx(tx, user_id, reason, now)

        return apply_transition(self._core, write=write)

    def has_planned_release(self, user_id: str) -> bool:
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM release_plans WHERE user_id = ? AND status = 'planned'", (user_id,)) is not None

    # ------------------------------------------------------------------ authorisation

    def commit_authorization(self, *, user_id: str, gates: ReleaseGates, now: datetime) -> CommitOutcome:
        """The commit point (D8). One transaction: re-validate, then SPEND exactly one token."""
        def write(tx: Transaction, _s: object) -> CommitOutcome:
            if self._open_authorization(tx, user_id):
                return CommitOutcome(status=CommitStatus.ALREADY_OPEN)
            plan = tx.fetch_one("SELECT * FROM release_plans WHERE user_id = ? AND status = 'planned'", (user_id,))
            if plan is None:
                return CommitOutcome(status=CommitStatus.NO_PLAN)
            if now < _parse_iso(plan["target_at"]):
                return CommitOutcome(status=CommitStatus.NOT_DUE)

            veto = gates.hard_veto()
            if veto is not None:
                self._cancel_in_tx(tx, user_id, veto, now)
                return CommitOutcome(status=CommitStatus.VETOED, reason=veto)
            if not gates.lock_barrier_confirmed:
                return CommitOutcome(status=CommitStatus.GATE_NOT_MET, reason="lock_state_not_confirmed")
            if classify(self._cfg, now) is not WindowKind.DISCRETIONARY:
                new_target = plan_target(now, bytes(plan["seed"]), self._cfg, self._lo, self._hi)
                if new_target is None:
                    return CommitOutcome(status=CommitStatus.GATE_NOT_MET, reason="no_valid_window")
                tx.execute("UPDATE release_plans SET target_at = ?, updated_at = ? WHERE id = ?",
                           (_iso(new_target), _iso(now), plan["id"]))
                self._emit(tx, "release_plan.retargeted", {"plan_id": plan["id"]}, now)
                return CommitOutcome(status=CommitStatus.RETARGETED, reason="window_closed")

            # DA1-CONCURRENT: the binding balance check, under this transaction's write lock.
            if balance(tx, user_id) < TOKEN_COST:
                self._cancel_in_tx(tx, user_id, "insufficient_tokens", now)
                return CommitOutcome(status=CommitStatus.INSUFFICIENT_TOKENS, reason="insufficient_tokens")

            auth_id = str(uuid.uuid4())
            spend_id = insert_entry(
                tx, user_id=user_id, delta=-TOKEN_COST, entry_type=LedgerEntryType.SPEND,
                reason_code="single_barrier_release", now=now, authorization_decision_id=auth_id,
            )
            self._insert_authorization(tx, auth_id, plan["id"], user_id, "token", spend_id, "token release authorized", now)
            tx.execute("UPDATE release_plans SET status = 'authorized', updated_at = ? WHERE id = ?", (_iso(now), plan["id"]))
            self._emit(tx, "release.authorized", {"authorization_id": auth_id, "kind": "token"}, now)
            return CommitOutcome(status=CommitStatus.AUTHORIZED, authorization_id=auth_id)

        return apply_transition(self._core, write=write)

    def commit_backstop(self, *, user_id: str, barrier_since: datetime, gates: ReleaseGates, now: datetime) -> CommitOutcome:
        """MAX_BARRIER_HOLD_HOURS safety release: spends NO token and ignores token/veto gates,
        but still requires a confirmed barrier and a valid discretionary window."""
        def write(tx: Transaction, _s: object) -> CommitOutcome:
            if self._open_authorization(tx, user_id):
                return CommitOutcome(status=CommitStatus.ALREADY_OPEN)
            if now - barrier_since < self._max_hold:
                return CommitOutcome(status=CommitStatus.NOT_DUE)
            if not gates.lock_barrier_confirmed:
                return CommitOutcome(status=CommitStatus.GATE_NOT_MET, reason="lock_state_not_confirmed")
            if classify(self._cfg, now) is not WindowKind.DISCRETIONARY:
                return CommitOutcome(status=CommitStatus.WINDOW_CLOSED, reason="window_closed")
            plan = tx.fetch_one("SELECT id FROM release_plans WHERE user_id = ? AND status = 'planned'", (user_id,))
            if plan is not None:
                plan_id = plan["id"]
                tx.execute("UPDATE release_plans SET status = 'authorized', updated_at = ? WHERE id = ?", (_iso(now), plan_id))
            else:
                plan_id = str(uuid.uuid4())
                tx.execute(
                    "INSERT INTO release_plans (id, user_id, status, requirement_met_at, target_at, seed, created_at, updated_at) "
                    "VALUES (?, ?, 'authorized', ?, ?, ?, ?, ?)",
                    (plan_id, user_id, _iso(now), _iso(now), self._seed_factory(), _iso(now), _iso(now)),
                )
            auth_id = str(uuid.uuid4())
            self._insert_authorization(tx, auth_id, plan_id, user_id, "backstop", None, "maximum hold backstop", now)
            self._emit(tx, "release.authorized", {"authorization_id": auth_id, "kind": "backstop"}, now)
            return CommitOutcome(status=CommitStatus.AUTHORIZED, authorization_id=auth_id)

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ provider progress

    def mark_provider_requested(self, *, authorization_id: str, now: datetime) -> bool:
        return self._advance(authorization_id, (ProviderState.AUTHORIZED,), ProviderState.PROVIDER_REQUESTED,
                             "provider request sent", now)

    def mark_provider_confirmed(self, *, authorization_id: str, now: datetime) -> bool:
        return self._advance(authorization_id, (ProviderState.PROVIDER_REQUESTED, ProviderState.PHYSICAL_STATE_UNKNOWN),
                             ProviderState.PROVIDER_CONFIRMED, "provider confirmed", now)

    def mark_physical_state_unknown(self, *, authorization_id: str, detail: str, now: datetime) -> bool:
        """Ambiguous outcome: the token spend is KEPT (conservative) and a human must look."""
        return self._advance(authorization_id, (ProviderState.PROVIDER_REQUESTED,),
                             ProviderState.PHYSICAL_STATE_UNKNOWN, detail, now)

    def mark_provider_failed(self, *, authorization_id: str, detail: str, now: datetime) -> bool:
        """Provably not applied: the spend is compensated (REVERSAL) in the same transaction."""
        return self._advance(authorization_id, (ProviderState.AUTHORIZED, ProviderState.PROVIDER_REQUESTED),
                             ProviderState.PROVIDER_FAILED, detail, now, compensate=True)

    def get_authorization_state(self, authorization_id: str) -> ProviderState | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT state FROM release_authorizations WHERE id = ?", (authorization_id,))
        return None if row is None else ProviderState(row["state"])

    # ------------------------------------------------------------------ internals

    def _advance(self, auth_id: str, from_states, to_state: ProviderState, detail: str, now: datetime,
                 *, compensate: bool = False) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT * FROM release_authorizations WHERE id = ?", (auth_id,))
            if row is None or row["state"] not in {s.value for s in from_states}:
                return False
            tx.execute("UPDATE release_authorizations SET state = ?, detail = ?, updated_at = ? WHERE id = ?",
                       (to_state.value, detail, _iso(now), auth_id))
            if compensate and row["spend_entry_id"]:
                reverse_entry(tx, original_entry_id=row["spend_entry_id"], reason_code="provider_failed", now=now)
            self._emit(tx, f"release.{to_state.value}", {"authorization_id": auth_id}, now)
            return True

        return apply_transition(self._core, write=write)

    @staticmethod
    def _open_authorization(tx: Transaction, user_id: str) -> bool:
        marks = ",".join("?" for _ in _OPEN_STATES)
        return tx.fetch_one(
            f"SELECT 1 FROM release_authorizations WHERE user_id = ? AND state IN ({marks})", (user_id, *_OPEN_STATES)
        ) is not None

    @staticmethod
    def _insert_authorization(tx, auth_id, plan_id, user_id, kind, spend_id, detail, now) -> None:
        tx.execute(
            "INSERT INTO release_authorizations (id, plan_id, user_id, kind, state, spend_entry_id, detail, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'authorized', ?, ?, ?, ?)",
            (auth_id, plan_id, user_id, kind, spend_id, detail, _iso(now), _iso(now)),
        )

    def _cancel_in_tx(self, tx: Transaction, user_id: str, reason: str, now: datetime) -> bool:
        plan = tx.fetch_one("SELECT id FROM release_plans WHERE user_id = ? AND status = 'planned'", (user_id,))
        if plan is None:
            return False
        tx.execute("UPDATE release_plans SET status = 'cancelled', cancel_reason = ?, updated_at = ? WHERE id = ?",
                   (reason, _iso(now), plan["id"]))
        self._emit(tx, "release_plan.cancelled", {"plan_id": plan["id"], "reason": reason}, now)
        return True

    @staticmethod
    def _emit(tx: Transaction, event_type: str, payload: dict, now: datetime) -> None:
        write_event(tx, DomainEvent(event_type=event_type, source_module="release_engine", payload=payload, occurred_at=now))
