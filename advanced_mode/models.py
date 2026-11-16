"""
advanced_mode/models.py

docs/architecture/advanced_mode_technical_design.md (draft, not
approved for implementation as a whole). This module implements ONLY
OperatingMode itself, its global-singleton persistence, and the
two-stage critical_change transition process -- see
advanced_mode/README.md for the exact boundary between what is
implemented here and what remains draft/undecided.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


def new_id() -> str:
    return str(uuid.uuid4())


class OperatingMode(StrEnum):
    BEGINNER = "beginner"          # owner, 2026-11-16: "beginner, standard and advanced"; beginner is close to the owner's own start
    STANDARD = "standard"
    ADVANCED = "advanced"


# Owner, 2026-11-16: the three levels in order of difficulty. Moving down is "lowering the difficulty" (the Iron Contract: only unlocked).
MODE_RANK = {OperatingMode.BEGINNER: 0, OperatingMode.STANDARD: 1, OperatingMode.ADVANCED: 2}


def is_lowering(source: "OperatingMode", target: "OperatingMode") -> bool:
    return MODE_RANK[target] < MODE_RANK[source]


class ModeTransitionStatus(StrEnum):
    """
    Seven distinct states, deliberately not a single `is_pending`
    boolean (per explicit review guidance -- the same discipline
    task_catalog already applies elsewhere). BLOCKED_BY_PENALTY_WINDOW
    and PAUSED_BY_PENALTY_WINDOW both mean "waiting for a PW to end,"
    but are kept audibly distinct: BLOCKED means the request was made
    while a PW was already active and no wait has ever started yet
    (wait_started_at IS NULL); PAUSED means a wait was running or had
    already elapsed and a NEW PW interrupted it. Both transition to
    WAITING once the PW ends, but their own history is different.

    INVALIDATED (added under direct review, distinct from CANCELLED):
    the request's own `source_mode` no longer matches the actual
    current `OperatingMode` at the moment confirmation was attempted --
    the request's original premise (confirming a transition FROM a
    specific starting mode) is no longer valid, and the request can
    never safely be reused, but this was not an explicit user
    cancellation.
    """
    BLOCKED_BY_PENALTY_WINDOW = "blocked_by_penalty_window"
    WAITING = "waiting"
    PAUSED_BY_PENALTY_WINDOW = "paused_by_penalty_window"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    INVALIDATED = "invalidated"


TERMINAL_STATUSES = frozenset({
    ModeTransitionStatus.CANCELLED, ModeTransitionStatus.COMPLETED, ModeTransitionStatus.INVALIDATED,
})


@dataclass(kw_only=True)
class OperatingModeState:
    """The global singleton (operating_mode_state, id=1) -- mutable,
    exactly one row ever exists. Not tied to any UserAccount -- see
    this module's own README for why."""
    current_mode: OperatingMode
    mode_activated_at: datetime


@dataclass(kw_only=True)
class ModeTransitionRequest:
    """
    Mutable-with-status (mode_transition_requests) -- CANCELLED/
    COMPLETED are terminal *values* of `status`, not rows moved
    elsewhere (see migration 017's own comment).

    Field invariants (enforced by repository write paths, not by the
    database schema -- see advanced_mode/README.md's own invariant
    table for exactly which layer guarantees what):
    - status == CANCELLED  =>  cancelled_at is not None and resolved_at is not None and confirmed_at is None
    - status == COMPLETED  =>  confirmed_at is not None and confirmed_via_consent_id is not None and resolved_at is not None
    - status == INVALIDATED  =>  invalidated_at is not None and resolved_at is not None and confirmed_at is None and confirmed_via_consent_id is None and cancelled_at is None
    - status == AWAITING_CONFIRMATION  =>  confirmable_at is not None and confirmed_at is None
    - status == PAUSED_BY_PENALTY_WINDOW  =>  confirmable_at is None (invalidated until waiting restarts)
    - status == BLOCKED_BY_PENALTY_WINDOW  =>  wait_started_at is None (no wait has ever run yet)
    """
    id: str = field(default_factory=new_id)
    source_mode: OperatingMode
    target_mode: OperatingMode
    status: ModeTransitionStatus
    requested_at: datetime
    requested_via_consent_id: str
    wait_started_at: datetime | None = None
    wait_interrupted_at: datetime | None = None
    confirmable_at: datetime | None = None
    confirmed_at: datetime | None = None
    confirmed_via_consent_id: str | None = None
    cancelled_at: datetime | None = None
    invalidated_at: datetime | None = None
    resolved_at: datetime | None = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class ActiveModeTransitionExistsError(ValueError):
    """MODE-1: a non-terminal request already exists -- the caller
    must cancel it first. Raised at the application level, matching
    the database's own idx_one_active_mode_transition_request partial
    unique index as a second, independent guarantee (never the sole
    enforcement -- see advanced_mode/README.md)."""


class NoActiveModeTransitionError(LookupError):
    """cancel_request()/confirm_transition() called with no matching
    non-terminal request."""


class MinimumTimeInAdvancedNotMetError(ValueError):
    """An Advanced -> Standard request was attempted before the
    30-day minimum (mode_activated_at + 30 days) elapsed."""


class ModeTransitionNotConfirmableError(ValueError):
    """confirm_transition() called while the request's (settled)
    status is not AWAITING_CONFIRMATION."""


class ModeTransitionSourceModeMismatchError(RuntimeError):
    """
    Raised by confirm_transition() when the actual current
    `OperatingMode`, re-read atomically inside the same write
    transaction, no longer matches the request's own `source_mode` --
    the request's original premise (confirming a transition FROM a
    specific starting mode) is no longer valid. By the time this is
    raised, the request has ALREADY been committed to INVALIDATED --
    raised strictly outside the write transaction, the same discipline
    ModeTransitionInterruptedByPenaltyWindowError already established
    (raising inside the transaction would roll back the very state
    change this exception claims happened).
    """
    def __init__(
        self, request: ModeTransitionRequest, *, expected_source_mode: OperatingMode, actual_current_mode: OperatingMode,
    ) -> None:
        super().__init__(
            f"Mode transition {request.id!r} could not be confirmed -- OperatingMode was "
            f"{actual_current_mode.value!r}, not the expected source_mode "
            f"{expected_source_mode.value!r}. The request has been committed to INVALIDATED; "
            f"OperatingMode did not change."
        )
        self.request = request
        self.expected_source_mode = expected_source_mode
        self.actual_current_mode = actual_current_mode


class ModeTransitionInterruptedByPenaltyWindowError(RuntimeError):
    """
    Raised by confirm_transition() when a Penalty Window was found to
    be active/frozen at the atomic, in-transaction check immediately
    before what would have been the final write. By the time this is
    raised, the request has ALREADY been committed to
    PAUSED_BY_PENALTY_WINDOW -- raised strictly outside the write
    transaction (see this module's own README for why raising it
    *inside* the transaction would have rolled back the very state
    change it claims happened). `request` reflects the actual,
    already-persisted state, not a stale snapshot.
    """
    def __init__(self, request: ModeTransitionRequest) -> None:
        super().__init__(
            f"Mode transition {request.id!r} could not be confirmed -- a Penalty "
            f"Window was active or frozen at the final check. The request has been "
            f"committed to PAUSED_BY_PENALTY_WINDOW, not confirmed; OperatingMode "
            f"did not change."
        )
        self.request = request
