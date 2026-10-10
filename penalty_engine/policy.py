"""
penalty_engine/policy.py

How a NEW Penalty Window gets its initial duration and hard cap (v0.39). The Penalty Engine asks a `WindowPolicy`
inside the consuming transaction; the dynamic policy turns the incident severity, the domain's Trust score and the
operating mode into hours with `penalty_engine.duration`. Reads are passed in as callables so this package imports
neither the Trust Manager nor Advanced Mode (the composition root supplies same-transaction readers).

A plan with `base_hours == 0` means "warning only": a first minor offense is consumed without a window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from infrastructure.database import Transaction
from penalty_engine.duration import HARD_CAP_HOURS, Mode, ViolationSeverity, initial_window_hours
from trust_manager.models import SeverityTier

__all__ = ["WindowPlan", "WindowPolicy", "DynamicWindowPolicy", "severity_of"]


@dataclass(frozen=True)
class WindowPlan:
    base_hours: float
    max_target_hours: float


class WindowPolicy(Protocol):
    def plan(self, tx: Transaction, *, trust_domain: str, rule_group_id: str, severity: SeverityTier,
             prior_offenses: int) -> WindowPlan: ...


def severity_of(tier: SeverityTier) -> ViolationSeverity:
    """minor -> minor, moderate -> moderate, major / critical -> severe."""
    return {SeverityTier.MINOR: ViolationSeverity.MINOR, SeverityTier.MODERATE: ViolationSeverity.MODERATE,
            SeverityTier.MAJOR: ViolationSeverity.SEVERE, SeverityTier.CRITICAL: ViolationSeverity.SEVERE}[tier]


class DynamicWindowPolicy:
    def __init__(self, *, read_mode: Callable[[Transaction], Mode],
                 read_trust: Callable[[Transaction, str], float | None]) -> None:
        self._read_mode = read_mode
        self._read_trust = read_trust

    def plan(self, tx: Transaction, *, trust_domain: str, rule_group_id: str, severity: SeverityTier,
             prior_offenses: int) -> WindowPlan:
        mode = self._read_mode(tx)
        trust = self._read_trust(tx, trust_domain)
        trust = 0.5 if trust is None else min(1.0, max(0.0, float(trust)))      # unknown domain: the middle of the range
        hours = initial_window_hours(severity_of(severity), mode, trust, prior_offenses=prior_offenses)
        return WindowPlan(base_hours=float(hours), max_target_hours=float(HARD_CAP_HOURS[mode]))
