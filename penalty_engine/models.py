"""
penalty_engine/models.py

Data structures for Penalty Engine Slice 1 — the core state machine
(start, freeze-as-a-set-of-reasons, resume, natural completion) and its
public read APIs. Canonical:
docs/architecture/penalty_window_technical_design.md Sections 2.1-2.6.

Deferred to a later slice (see penalty_engine/README.md): extend()/
should_extend() (Extension integration), recovery_credit_ledger/
recovery_credit_decisions (Recovery Plan integration, 3.4), terminate()
(administrative termination — explicitly deferred by the architecture
document itself, 2.1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


def new_id() -> str:
    return str(uuid.uuid4())


class PenaltyWindowStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    COMPLETED = "completed"


class ResolutionMethod(StrEnum):
    COUNTDOWN_COMPLETE = "countdown_complete"
    # 'manual_termination' is part of the canonical model (2.1) but has
    # no writer in this slice -- terminate() is explicitly deferred by
    # the architecture document itself, not merely by this slice.


class FreezeReason(StrEnum):
    TEMPORARY_WEAR_EXEMPTION = "temporary_wear_exemption"
    EMERGENCY_OVERRIDE = "emergency_override"
    PARTNERED_INTIMACY_AUTHORIZATION = "partnered_intimacy_authorization"


class FreezeEndReason(StrEnum):
    RESUMED_NORMALLY = "resumed_normally"
    EXPIRED = "expired"


class AuthorizationFreezeState(StrEnum):
    NOT_FOUND = "not_found"
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"


@dataclass(kw_only=True)
class PenaltyWindow:
    """
    Mutable-with-status (implementation_conventions.md Section 7): status,
    accumulated_active_hours, and active_period_started_at are "what is
    true now" -- history lives in the append-only domain_events trail
    (penalty_window.started/.frozen/.resumed/.completed), not in old
    values of this row.
    """
    id: str = field(default_factory=new_id)
    created_at: datetime
    status: PenaltyWindowStatus = PenaltyWindowStatus.ACTIVE
    closed_at: datetime | None = None
    resolution_method: ResolutionMethod | None = None
    base_duration_hours: float
    extensions_hours: float = 0.0
    accumulated_active_hours: float = 0.0
    active_period_started_at: datetime | None = None
    recovery_credits_earned_hours: float = 0.0
    max_target_hours: float = 336.0     # hard cap of THIS window, fixed at start (Standard 336 h, Advanced 720 h)


@dataclass(frozen=True, kw_only=True)
class FreezePeriod:
    id: str = field(default_factory=new_id)
    penalty_window_id: str
    started_at: datetime
    ended_at: datetime | None = None
    reason: FreezeReason
    exemption_id: str | None = None
    authorization_decision_id: str | None = None
    expires_at: datetime | None = None
    end_reason: FreezeEndReason | None = None


class PenaltyWindowNotFound(Exception):
    """
    Raised when a given penalty_window_id does not exist at all --
    distinct from a genuinely existing window with no recorded relevant
    domains (get_penalty_window_relevant_domains(), 2.6), so a caller
    can tell "no such window" apart from "this window's data looks
    anomalous."
    """
    def __init__(self, penalty_window_id: str) -> None:
        super().__init__(f"No PenaltyWindow with id={penalty_window_id!r}")
        self.penalty_window_id = penalty_window_id


@dataclass(frozen=True, kw_only=True)
class RecoveryCreditDecision:
    """
    Append-only (penalty_window_technical_design.md 3.4, applying
    recovery_plan_technical_design.md Section 6). Always written for
    every RecoveryTaskCompletion processed, regardless of outcome --
    the same "never silently indistinguishable from not-yet-processed"
    discipline as ExtensionDecision.
    """
    id: str = field(default_factory=new_id)
    created_at: datetime
    completion_id: str          # UNIQUE -- I26 primary guarantee
    penalty_window_id: str

    proposed_hours: float
    credited_hours: float        # may be 0
    capacity_limited: bool
    explanation: str             # required, non-empty regardless of credited_hours
