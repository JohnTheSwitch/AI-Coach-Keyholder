"""
trust_manager/checkin_scoring.py

Pure scoring for check-in evidence (v0.39). A missed check-in is a clear drop; an
insufficient answer that stayed insufficient after the follow-up window is a minor one.
Values are bootstrap-default placeholders (the owner specified "score drop" / "minor"
but no numbers); the per-row and per-recalculation caps still bound their effect.
"""

from __future__ import annotations

from trust_manager.models import EvidenceType

__all__ = [
    "CHECKIN_MISSED_WEIGHT", "CHECKIN_INSUFFICIENT_WEIGHT", "CHECKIN_EVIDENCE_CONFIDENCE", "checkin_evidence_spec",
]

# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
# Placeholder: a missed check-in lowers Trust noticeably.
CHECKIN_MISSED_WEIGHT = -0.06

# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
# Placeholder: an answer that stayed insufficient after the follow-up is a minor drop.
CHECKIN_INSUFFICIENT_WEIGHT = -0.02

# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
# System-observed facts (a deadline passed) carry full confidence.
CHECKIN_EVIDENCE_CONFIDENCE = 1.0


def checkin_evidence_spec(kind: str) -> tuple[EvidenceType, float, float]:
    """(evidence type, raw weight, confidence) for 'missed' or 'insufficient'."""
    if kind == "missed":
        return EvidenceType.CHECKIN_MISSED, CHECKIN_MISSED_WEIGHT, CHECKIN_EVIDENCE_CONFIDENCE
    if kind == "insufficient":
        return EvidenceType.CHECKIN_INSUFFICIENT, CHECKIN_INSUFFICIENT_WEIGHT, CHECKIN_EVIDENCE_CONFIDENCE
    raise ValueError(f"unknown check-in evidence kind: {kind!r}")
