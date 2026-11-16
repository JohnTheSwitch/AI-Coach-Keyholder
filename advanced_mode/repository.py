"""
advanced_mode/repository.py

docs/architecture/advanced_mode_technical_design.md (draft, not
approved for implementation as a whole -- see advanced_mode/README.md
for the exact boundary this module implements).

Two structurally separate public classes, the same split
task_catalog established first in this project:

- `AdvancedMode` -- read-only (`get_current_mode`, `get_active_request`).
  No write method exists on this class, full stop. Never applies a
  lazy state transition as a side effect of being read -- see
  `advance_transition_state()`'s own docstring for why that is a
  separate, explicit write instead.
- `AdvancedModeAdministration` -- critical_change-governed write API.
  Takes `PenaltyEngine` as a per-call parameter (matching
  `PenaltyEngine.start_window_if_eligible(tm: TrustManager, ...)`'s own
  established shape -- a dependency passed per call, never stored at
  construction).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from advanced_mode.models import (
    ActiveModeTransitionExistsError,
    MinimumTimeInAdvancedNotMetError,
    ModeTransitionInterruptedByPenaltyWindowError,
    ModeTransitionNotConfirmableError,
    ModeTransitionRequest,
    ModeTransitionSourceModeMismatchError,
    ModeTransitionStatus,
    NoActiveModeTransitionError,
    OperatingMode,
    OperatingModeState,
)
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from penalty_engine.repository import PenaltyEngine

__all__ = ["AdvancedMode", "AdvancedModeAdministration"]

MINIMUM_DAYS_IN_ADVANCED = 30
CONFIRMATION_WAIT_HOURS = 24

_NON_TERMINAL_STATUSES = (
    ModeTransitionStatus.BLOCKED_BY_PENALTY_WINDOW.value,
    ModeTransitionStatus.WAITING.value,
    ModeTransitionStatus.PAUSED_BY_PENALTY_WINDOW.value,
    ModeTransitionStatus.AWAITING_CONFIRMATION.value,
)


def _require_consent_id(consent_id: str) -> None:
    if not consent_id or not consent_id.strip():
        raise ValueError(
            "A non-empty consent reference is required -- mode transitions are a "
            "specialized two-stage critical_change process."
        )


def _row_to_state(row) -> OperatingModeState:
    return OperatingModeState(
        current_mode=OperatingMode(row["current_mode"]), mode_activated_at=_parse_iso(row["mode_activated_at"]),
    )


def _row_to_request(row) -> ModeTransitionRequest:
    return ModeTransitionRequest(
        id=row["id"], source_mode=OperatingMode(row["source_mode"]), target_mode=OperatingMode(row["target_mode"]),
        status=ModeTransitionStatus(row["status"]), requested_at=_parse_iso(row["requested_at"]),
        requested_via_consent_id=row["requested_via_consent_id"],
        wait_started_at=_parse_iso(row["wait_started_at"]) if row["wait_started_at"] is not None else None,
        wait_interrupted_at=_parse_iso(row["wait_interrupted_at"]) if row["wait_interrupted_at"] is not None else None,
        confirmable_at=_parse_iso(row["confirmable_at"]) if row["confirmable_at"] is not None else None,
        confirmed_at=_parse_iso(row["confirmed_at"]) if row["confirmed_at"] is not None else None,
        confirmed_via_consent_id=row["confirmed_via_consent_id"],
        cancelled_at=_parse_iso(row["cancelled_at"]) if row["cancelled_at"] is not None else None,
        invalidated_at=_parse_iso(row["invalidated_at"]) if row["invalidated_at"] is not None else None,
        resolved_at=_parse_iso(row["resolved_at"]) if row["resolved_at"] is not None else None,
    )


def _fetch_active_request(tx: Transaction) -> ModeTransitionRequest | None:
    row = tx.fetch_one(
        f"SELECT * FROM mode_transition_requests WHERE status IN "
        f"({', '.join('?' for _ in _NON_TERMINAL_STATUSES)})",
        _NON_TERMINAL_STATUSES,
    )
    return _row_to_request(row) if row is not None else None


def _compute_next_state(
    request: ModeTransitionRequest, pw_active: bool, now: datetime,
) -> ModeTransitionRequest:
    """
    Pure function, no I/O -- applies AT MOST ONE deterministic
    transition, never cascades multiple steps in a single call (per
    explicit review guidance: PAUSED -> WAITING must not, in the same
    call, immediately continue into AWAITING_CONFIRMATION, since the
    freshly-restarted 24h wait has obviously not elapsed yet -- and
    since confirmable_at is set to now + 24h in that same step, a
    second check within the same call would trivially never fire
    anyway, making single-step-per-call both correct and simpler to
    reason about than an internal loop).
    """
    if request.status == ModeTransitionStatus.BLOCKED_BY_PENALTY_WINDOW:
        if not pw_active:
            return replace(
                request, status=ModeTransitionStatus.WAITING, wait_started_at=now,
                confirmable_at=now + timedelta(hours=CONFIRMATION_WAIT_HOURS),
            )
        return request

    if request.status == ModeTransitionStatus.WAITING:
        if pw_active:
            return replace(
                request, status=ModeTransitionStatus.PAUSED_BY_PENALTY_WINDOW,
                wait_interrupted_at=now, confirmable_at=None,
            )
        if request.confirmable_at is not None and now >= request.confirmable_at:
            return replace(request, status=ModeTransitionStatus.AWAITING_CONFIRMATION)
        return request

    if request.status == ModeTransitionStatus.PAUSED_BY_PENALTY_WINDOW:
        if not pw_active:
            return replace(
                request, status=ModeTransitionStatus.WAITING, wait_started_at=now,
                confirmable_at=now + timedelta(hours=CONFIRMATION_WAIT_HOURS),
            )
        return request

    if request.status == ModeTransitionStatus.AWAITING_CONFIRMATION:
        if pw_active:
            return replace(
                request, status=ModeTransitionStatus.PAUSED_BY_PENALTY_WINDOW,
                wait_interrupted_at=now, confirmable_at=None,
            )
        return request

    return request  # CANCELLED/COMPLETED -- terminal, never touched


def _write_request_fields(tx: Transaction, request: ModeTransitionRequest) -> None:
    """Shared by advance_transition_state()'s own write and
    confirm_transition()'s PAUSED/INVALIDATED branches -- writes every
    mutable field of a request row (all except id/source_mode/
    target_mode/requested_at/requested_via_consent_id, which never
    change after creation)."""
    tx.execute(
        """
        UPDATE mode_transition_requests SET
            status = ?, wait_started_at = ?, wait_interrupted_at = ?, confirmable_at = ?,
            confirmed_at = ?, confirmed_via_consent_id = ?, cancelled_at = ?, invalidated_at = ?, resolved_at = ?
        WHERE id = ?
        """,
        (
            request.status.value,
            _iso(request.wait_started_at) if request.wait_started_at is not None else None,
            _iso(request.wait_interrupted_at) if request.wait_interrupted_at is not None else None,
            _iso(request.confirmable_at) if request.confirmable_at is not None else None,
            _iso(request.confirmed_at) if request.confirmed_at is not None else None,
            request.confirmed_via_consent_id,
            _iso(request.cancelled_at) if request.cancelled_at is not None else None,
            _iso(request.invalidated_at) if request.invalidated_at is not None else None,
            _iso(request.resolved_at) if request.resolved_at is not None else None,
            request.id,
        ),
    )


class AdvancedMode:
    """
    Read-only. No write method exists on this class at all -- never
    applies `advance_transition_state()`'s own lazy transitions as a
    side effect of a read (that would give a "read-only" method hidden
    state-changing behavior -- see advanced_mode/README.md for the
    full reasoning). `get_active_request()` returns exactly the
    persisted row, whatever status it is actually in right now --
    which may be stale relative to elapsed time or a Penalty Window
    that changed since the last write; call
    `AdvancedModeAdministration.advance_transition_state()` explicitly
    first if a settled view is needed.
    """

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def get_current_mode(self) -> OperatingModeState:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM operating_mode_state WHERE id = 1")
        return _row_to_state(row)

    def get_active_request(self) -> ModeTransitionRequest | None:
        with self._core.transaction() as tx:
            return _fetch_active_request(tx)


class AdvancedModeAdministration:
    """critical_change-governed write API for the two-stage mode
    transition process. `penalty_engine` is passed per call, never
    stored at construction (matching
    `PenaltyEngine.start_window_if_eligible(tm, ...)`'s own shape)."""

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def start_new_wearer_at_beginner(self, *, now: datetime) -> bool:
        """Owner, 2026-11-16: an additional wearer's database starts at the beginner level. Only a database that never had a mode transition
        and is still at its seeded Standard is moved; True when it was moved now. Never used for the owner's database."""
        def write(tx: Transaction, _s: object) -> bool:
            if tx.fetch_one("SELECT 1 FROM mode_transition_requests LIMIT 1") is not None:
                return False
            done = tx.execute("UPDATE operating_mode_state SET current_mode = ?, mode_activated_at = ? WHERE id = 1 AND current_mode = ?",
                              (OperatingMode.BEGINNER.value, _iso(now), OperatingMode.STANDARD.value)).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="advanced_mode.started_at_beginner", source_module="advanced_mode", occurred_at=now,
                                            payload={"mode": OperatingMode.BEGINNER.value}))
            return bool(done)

        return bool(apply_transition(self._core, write=write))

    # -------------------------------------------------------------------
    # The explicit reconciliation command
    # -------------------------------------------------------------------

    def advance_transition_state(
        self, penalty_engine: PenaltyEngine, *, now: datetime,
    ) -> ModeTransitionRequest | None:
        """
        The ONLY place deterministic, time/PW-driven transitions are
        applied (BLOCKED->WAITING, PAUSED->WAITING, WAITING->PAUSED,
        WAITING->AWAITING_CONFIRMATION, AWAITING_CONFIRMATION->PAUSED).
        Idempotent: calling this repeatedly with an unchanged
        situation performs no UPDATE and returns an equivalent request
        unchanged -- verified directly by a dedicated test, not merely
        assumed. Never confirms, never cancels, never changes
        OperatingMode, never consumes or creates any consent. Returns
        `None` if no non-terminal request exists at all.
        """
        def write(tx: Transaction, _state: object) -> ModeTransitionRequest | None:
            request = _fetch_active_request(tx)
            if request is None:
                return None
            pw = penalty_engine.get_active_or_frozen_penalty_window_in_transaction(tx)
            updated = _compute_next_state(request, pw is not None, now)
            if updated == request:
                return request  # no-op: idempotent, no UPDATE issued
            _write_request_fields(tx, updated)
            return updated

        return apply_transition(self._core, write=write)

    # -------------------------------------------------------------------
    # Request
    # -------------------------------------------------------------------

    def request_transition(
        self, penalty_engine: PenaltyEngine, *, target_mode: OperatingMode,
        requested_via_consent_id: str, now: datetime,
    ) -> ModeTransitionRequest:
        """MODE-1 (at most one non-terminal request, enforced here AND
        by idx_one_active_mode_transition_request as a second,
        independent guarantee), the 30-day Advanced->Standard minimum,
        and target_mode != current mode are all checked in this same
        transaction before any row is written."""
        _require_consent_id(requested_via_consent_id)

        def write(tx: Transaction, _state: object) -> ModeTransitionRequest:
            if _fetch_active_request(tx) is not None:
                raise ActiveModeTransitionExistsError(
                    "A non-terminal mode transition request already exists -- cancel it first."
                )

            state_row = tx.fetch_one("SELECT * FROM operating_mode_state WHERE id = 1")
            current_state = _row_to_state(state_row)

            if target_mode == current_state.current_mode:
                raise ValueError(
                    f"target_mode ({target_mode.value!r}) must differ from the current mode "
                    f"({current_state.current_mode.value!r})."
                )

            if current_state.current_mode == OperatingMode.ADVANCED:      # 30 days in Advanced before going down from it
                minimum_met_at = current_state.mode_activated_at + timedelta(days=MINIMUM_DAYS_IN_ADVANCED)
                if now < minimum_met_at:
                    raise MinimumTimeInAdvancedNotMetError(
                        f"Advanced -> Standard requires at least {MINIMUM_DAYS_IN_ADVANCED} days in "
                        f"Advanced; eligible from {minimum_met_at.isoformat()}."
                    )

            pw = penalty_engine.get_active_or_frozen_penalty_window_in_transaction(tx)
            if pw is not None:
                status = ModeTransitionStatus.BLOCKED_BY_PENALTY_WINDOW
                wait_started_at = None
                confirmable_at = None
            else:
                status = ModeTransitionStatus.WAITING
                wait_started_at = now
                confirmable_at = now + timedelta(hours=CONFIRMATION_WAIT_HOURS)

            request = ModeTransitionRequest(
                source_mode=current_state.current_mode, target_mode=target_mode, status=status,
                requested_at=now, requested_via_consent_id=requested_via_consent_id,
                wait_started_at=wait_started_at, confirmable_at=confirmable_at,
            )
            tx.execute(
                """
                INSERT INTO mode_transition_requests
                    (id, source_mode, target_mode, status, requested_at, requested_via_consent_id,
                     wait_started_at, confirmable_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.id, request.source_mode.value, request.target_mode.value, request.status.value,
                    _iso(request.requested_at), request.requested_via_consent_id,
                    _iso(request.wait_started_at) if request.wait_started_at is not None else None,
                    _iso(request.confirmable_at) if request.confirmable_at is not None else None,
                ),
            )
            return request

        return apply_transition(self._core, write=write)

    # -------------------------------------------------------------------
    # Cancel
    # -------------------------------------------------------------------

    def cancel_request(self, request_id: str, *, now: datetime) -> ModeTransitionRequest:
        """Cancels whichever non-terminal status the request is
        currently in -- no settling needed first, cancellation is
        valid from any non-terminal state."""
        def write(tx: Transaction, _state: object) -> ModeTransitionRequest:
            row = tx.fetch_one("SELECT * FROM mode_transition_requests WHERE id = ?", (request_id,))
            if row is None:
                raise NoActiveModeTransitionError(f"No mode transition request {request_id!r}.")
            request = _row_to_request(row)
            if request.is_terminal():
                raise NoActiveModeTransitionError(
                    f"Request {request_id!r} is already {request.status.value!r} -- nothing to cancel."
                )
            updated = replace(request, status=ModeTransitionStatus.CANCELLED, cancelled_at=now, resolved_at=now)
            _write_request_fields(tx, updated)
            return updated

        return apply_transition(self._core, write=write)

    # -------------------------------------------------------------------
    # Confirm
    # -------------------------------------------------------------------

    def confirm_transition(
        self, request_id: str, penalty_engine: PenaltyEngine, *, confirmed_via_consent_id: str, now: datetime,
    ) -> ModeTransitionRequest:
        """
        Only valid when the request's CURRENTLY STORED status is
        already AWAITING_CONFIRMATION -- does not itself settle
        BLOCKED/WAITING/PAUSED first (call advance_transition_state()
        separately if that's needed; keeps this method's own contract
        simple: "confirm an already-awaiting request").

        Two checks, in this exact order, both atomically inside the
        SAME write transaction as the final change:

        1. **source_mode integrity** (checked FIRST): re-reads
           `operating_mode_state` and compares it against the request's
           own `source_mode`. A mismatch means the request's original
           premise -- confirming a transition FROM a specific starting
           mode -- is no longer valid (something else changed
           `OperatingMode` in the meantime). The request is committed
           to INVALIDATED (not CANCELLED -- this was not an explicit
           user cancellation, and not COMPLETED/PAUSED -- the request's
           premise, not merely its timing, is wrong). Checked before
           the Penalty Window check below since there is no point
           checking PW for a request whose own premise already no
           longer holds.
        2. **MODE-5** (checked only if source_mode matches): re-checks
           Penalty Window, exactly as before.

        Both checks follow the same commit-then-raise discipline: the
        `write()` closure returns normally in every branch (never
        raises from inside the transaction -- raising inside would
        roll back the very state change the resulting exception claims
        happened, since `Database.transaction()` rolls back on any
        exception raised inside its own `with` block). The
        corresponding domain exception
        (`ModeTransitionSourceModeMismatchError`/
        `ModeTransitionInterruptedByPenaltyWindowError`) is raised only
        after `apply_transition()` has already returned -- i.e. only
        after the transaction has already committed.
        """
        _require_consent_id(confirmed_via_consent_id)

        def write(tx: Transaction, _state: object) -> tuple[ModeTransitionRequest, str, OperatingMode | None]:
            row = tx.fetch_one("SELECT * FROM mode_transition_requests WHERE id = ?", (request_id,))
            if row is None:
                raise NoActiveModeTransitionError(f"No mode transition request {request_id!r}.")
            request = _row_to_request(row)
            if request.status != ModeTransitionStatus.AWAITING_CONFIRMATION:
                raise ModeTransitionNotConfirmableError(
                    f"Request {request_id!r} is {request.status.value!r}, not AWAITING_CONFIRMATION."
                )

            state_row = tx.fetch_one("SELECT * FROM operating_mode_state WHERE id = 1")
            current_state = _row_to_state(state_row)
            if current_state.current_mode != request.source_mode:
                invalidated = replace(
                    request, status=ModeTransitionStatus.INVALIDATED, invalidated_at=now, resolved_at=now,
                    confirmed_at=None, confirmed_via_consent_id=None, cancelled_at=None,
                )
                _write_request_fields(tx, invalidated)
                return invalidated, "source_mode_mismatch", current_state.current_mode

            pw = penalty_engine.get_active_or_frozen_penalty_window_in_transaction(tx)
            if pw is not None:
                paused = replace(
                    request, status=ModeTransitionStatus.PAUSED_BY_PENALTY_WINDOW,
                    wait_interrupted_at=now, confirmable_at=None,
                )
                _write_request_fields(tx, paused)
                return paused, "penalty_window_interrupted", None

            completed = replace(
                request, status=ModeTransitionStatus.COMPLETED, confirmed_at=now,
                confirmed_via_consent_id=confirmed_via_consent_id, resolved_at=now,
            )
            _write_request_fields(tx, completed)
            tx.execute(
                "UPDATE operating_mode_state SET current_mode = ?, mode_activated_at = ? WHERE id = 1",
                (request.target_mode.value, _iso(now)),
            )
            return completed, "success", None

        request, outcome, actual_current_mode = apply_transition(self._core, write=write)
        # The transaction has already committed successfully at this point
        # (apply_transition() only returns once Database.transaction()'s
        # own `with` block has exited normally) -- raising here, strictly
        # outside that block, cannot roll back what was already written.
        if outcome == "source_mode_mismatch":
            raise ModeTransitionSourceModeMismatchError(
                request, expected_source_mode=request.source_mode, actual_current_mode=actual_current_mode,
            )
        if outcome == "penalty_window_interrupted":
            raise ModeTransitionInterruptedByPenaltyWindowError(request)
        return request
