"""
chaster_execution/models.py

Types for the Chaster execution plane (Phase 3). Canonical:
docs/architecture/chaster_execution_phase3_proposal.md.

The transport interface has exactly ONE method: set_frozen, for barrier management. There is, and will
never be, a method for a time change (owner decision, Zero-Time-Modification) or for the combination,
recovery or unlock endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "ExecStatus", "TransportOutcome", "TransportResult", "LockActionTransport",
    "BARRIER_ACTIONS", "BARRIER_RULE_IDS", "DECISION_MAX_AGE_MINUTES",
]


class ExecStatus(StrEnum):
    DRY_RUN = "dry_run"
    PENDING = "pending"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    BLOCKED = "blocked"          # a pre-flight check refused; nothing was sent
    EXPIRED = "expired"          # older than the staleness window; nothing was sent
    NEEDS_REVIEW = "needs_review"  # outcome unknown; NEVER retried automatically


# v0.39: freeze/unfreeze may execute ONLY for barrier management, i.e. for decisions created by the
# release engine under one of these rule ids. A freeze/unfreeze from any other rule (for example a
# custom Rules Engine ruleset) never executes.
def remaining_seconds(obs, now) -> int:
    """Owner, 2026-11-28 (the timer to 0): the time left on a lock. While it is frozen Chaster's clock stands still, so it is counted from
    the moment it was frozen, not from now (provider-reported `frozenAt`/`endDate`). 0 when unknown or already over."""
    if obs is None or obs.end_date is None:
        return 0
    start = obs.frozen_at if obs.is_frozen and obs.frozen_at is not None else now
    return max(0, int((obs.end_date - start).total_seconds()))


BARRIER_ACTIONS = frozenset({"freeze", "unfreeze", "zero_time"})               # zero_time: owner, 2026-11-28 (the pick-up timer to 0)
BARRIER_RULE_IDS = frozenset({"barrier.release", "barrier.freeze", "barrier.zero_time"})

DECISION_MAX_AGE_MINUTES = 15


class TransportOutcome(StrEnum):
    OK = "ok"                          # Chaster accepted the change (HTTP 204)
    NOT_SENT = "not_sent"              # provably failed before the request left (e.g. no connection)
    UNAUTHORIZED = "unauthorized"      # 401
    FORBIDDEN = "forbidden"            # 403: missing lock.time.add / lock.time.remove permission
    NOT_ELIGIBLE = "not_eligible"      # 400: e.g. the lock is not locked
    NOT_FOUND = "not_found"            # 404
    UNKNOWN = "unknown"                # timeout / 5xx / anything after sending: outcome ambiguous


@dataclass(frozen=True, kw_only=True)
class TransportResult:
    outcome: TransportOutcome
    http_status: int | None = None


class LockActionTransport(Protocol):
    """Authenticated as the KEYHOLDER connection (Route A). Implementations
    must never log or return tokens, headers or bodies."""

    def set_frozen(self, *, lock_id: str, frozen: bool) -> TransportResult: ...

    def remove_remaining_time(self, *, lock_id: str, seconds: int) -> TransportResult: ...
