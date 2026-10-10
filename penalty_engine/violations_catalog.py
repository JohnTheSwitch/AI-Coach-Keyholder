"""
penalty_engine/violations_catalog.py

The rule-violation catalog by domain (v0.39, section 11.8). Declarative data only: no I/O, not wired yet. It records the
owner-specified severity, the Trust drop of a first/warning offense, and side effects. Whether a code can be detected
automatically depends on modules that do not exist yet (hygiene, status reports); those entries are marked `detectable=False`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from penalty_engine.duration import ViolationSeverity as S

__all__ = ["Violation", "CATALOG", "DEFAULT_TRUST_DROP", "get", "by_domain"]

# Owner default for a violation that has no specific drop of its own.
DEFAULT_TRUST_DROP = {S.MINOR: Decimal("0.02"), S.MODERATE: Decimal("0.05"), S.SEVERE: Decimal("0.10")}


@dataclass(frozen=True)
class Violation:
    code: str
    domain: str                       # hygiene | supervision | integrity
    severity: S
    trust_drop: Decimal               # applied to the Trust score on a first / warning occurrence
    description: str
    side_effects: tuple[str, ...] = ()
    detectable: bool = True           # False while the detecting module does not exist


def _v(code, domain, severity, drop, description, side=(), detectable=True) -> Violation:
    """`drop=None` means the owner gave no specific number: the severity default applies."""
    return Violation(code, domain, severity, DEFAULT_TRUST_DROP[severity] if drop is None else Decimal(drop),
                     description, tuple(side), detectable)


CATALOG: tuple[Violation, ...] = (
    _v("HYGIENE_OVERRUN_MINOR", "hygiene", S.MINOR, "0.03", "Overrun of 2-5 minutes after the grace period", detectable=False),
    _v("HYGIENE_OVERRUN_REPEATED", "hygiene", S.MINOR, "0.05", "Second minor overrun within 7 days",
       ("hygiene_lockout_48h",), detectable=False),
    _v("HYGIENE_OVERRUN_MODERATE", "hygiene", S.MODERATE, None, "Overrun of 5-15 minutes after the grace period", detectable=False),
    _v("HYGIENE_RELOCK_FAILED", "hygiene", S.SEVERE, None, "Failed or unconfirmed re-lock more than 15 minutes after hygiene",
       ("hygiene_lockout_48h",), detectable=False),
    _v("HYGIENE_MISUSE", "hygiene", S.SEVERE, None, "Hygiene window used for an unauthorized purpose", detectable=False),
    _v("CHECKIN_MISSED_FIRST", "supervision", S.MINOR, "0.02", "First missed check-in (warning)"),
    _v("CHECKIN_INSUFFICIENT", "supervision", S.MINOR, "0.01", "Insufficient check-in response after the grace window"),
    _v("CHECKIN_REPEATED_NONCOMPLIANCE", "supervision", S.MINOR, "0.06",
       "Three consecutive missed check-ins, or three within 48 hours"),
    _v("SUPERVISION_ABANDONED", "supervision", S.SEVERE, None, "No communication for more than 24 hours",
       ("high_security_status",), detectable=False),
    _v("STATUS_REPORT_LATE", "integrity", S.MINOR, "0.02", "Late status report in the app", detectable=False),
    _v("CHASTER_SETTINGS_TAMPERED", "integrity", S.MODERATE, None, "Unauthorized change of Chaster settings or extensions",
       detectable=False),
    _v("LOCK_RENEWAL_DELAYED", "integrity", S.MODERATE, None, "Delayed lock renewal after session completion", detectable=False),
    _v("EPISTEMIC_MISREPRESENTATION", "integrity", S.SEVERE, None,
       "Claimed locked while the provider reports unlocked", detectable=False),
)

_BY_CODE = {v.code: v for v in CATALOG}


def get(code: str) -> Violation:
    return _BY_CODE[code]


def by_domain(domain: str) -> tuple[Violation, ...]:
    return tuple(v for v in CATALOG if v.domain == domain)
