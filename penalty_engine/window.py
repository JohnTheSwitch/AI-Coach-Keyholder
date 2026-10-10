"""
penalty_engine/window.py

Pure, deterministic functions describing a PenaltyWindow's countdown.
Canonical: docs/architecture/penalty_window_technical_design.md
Sections 1 (I5, I6), 2.2. No database access — mirrors
trust_manager/severity.py and trust_manager/recalculation.py's own
separation of pure computation from persistence.

`MAX_TARGET_ACTIVE_HOURS = 336` is given explicitly by the architecture
document (I5: "min(base_duration_hours + extensions_hours, 336)").
`DEFAULT_BASE_DURATION_HOURS`, by contrast, is NOT given a specific
value anywhere in the document — starting a window requires some base
duration, and nothing in Sections 1-4 commits to a number. This is this
slice's own default, flagged here exactly like
trust_manager/recalculation.py flags MAX_ABS_EFFECTIVE_WEIGHT/CONFIDENCE_K,
for the same reason: so a future reviewer knows this one, unlike
MAX_TARGET_ACTIVE_HOURS, was not transcribed from the architecture
document.
"""

from __future__ import annotations

from datetime import datetime

from penalty_engine.models import PenaltyWindow, PenaltyWindowStatus

__all__ = [
    "target_active_hours",
    "active_hours_elapsed",
    "is_complete",
    "remaining_active_hours",
]

# I5 — given explicitly by the architecture document. NOT a bootstrap
# default.
MAX_TARGET_ACTIVE_HOURS = 336.0       # the Standard Mode cap; a window carries its own cap (Advanced Mode: 720 h)

# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
# Temporary executable value pending an explicit ownership decision.
# NOT given a specific value anywhere in the architecture document --
# this slice's own default. See this module's docstring. Directly
# determines how long a base penalty lasts -- a strong personal-policy
# candidate (plausibly user-owned), not merely a technical parameter.
DEFAULT_BASE_DURATION_HOURS = 24.0


def target_active_hours(window: PenaltyWindow) -> float:
    """I5: target_active_hours = min(base_duration_hours + extensions_hours, 336) —
    a calculation, never a stored value; the cap is enforced at read time."""
    return min(window.base_duration_hours + window.extensions_hours, window.max_target_hours)


def active_hours_elapsed(window: PenaltyWindow, now: datetime) -> float:
    """
    I6: the sum of all FreezePeriod intervals is never included here —
    this is why FROZEN windows have active_period_started_at=None and
    simply return accumulated_active_hours unchanged (time does not
    advance while frozen), while ACTIVE windows add the elapsed time
    since active_period_started_at, which is exactly what makes 4.5's
    downtime rule ("downtime IS counted toward an ACTIVE window's
    countdown") true for free — this function knows nothing about
    whether the process was running in between; it only ever compares
    two absolute timestamps.
    """
    if window.status != PenaltyWindowStatus.ACTIVE or window.active_period_started_at is None:
        return window.accumulated_active_hours
    elapsed_since_active = (now - window.active_period_started_at).total_seconds() / 3600.0
    return window.accumulated_active_hours + elapsed_since_active


def is_complete(window: PenaltyWindow, now: datetime) -> bool:
    """ACTIVE -> COMPLETED guard (2.2): is_complete(now) == True."""
    return active_hours_elapsed(window, now) >= target_active_hours(window)


def remaining_active_hours(window: PenaltyWindow, now: datetime) -> float:
    """Never negative -- a window that has already reached its target
    reports zero remaining, not a negative number."""
    return max(0.0, target_active_hours(window) - active_hours_elapsed(window, now))
