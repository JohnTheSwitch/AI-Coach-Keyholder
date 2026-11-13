"""
trust_manager/checkin_scoring.py

Pure scoring for check-in evidence (v0.39). A missed check-in is a clear drop; an
insufficient answer that stayed insufficient after the follow-up window is a minor one.
Values are the owner's matrix; the per-row and per-recalculation caps still bound their effect.
"""

from __future__ import annotations

from trust_manager.models import EvidenceType

__all__ = [
    "CHECKIN_MISSED_WEIGHT", "CHECKIN_MISSED_WEIGHTS", "CHECKIN_INSUFFICIENT_WEIGHT", "CHECKIN_EVIDENCE_CONFIDENCE", "checkin_evidence_spec",
    "COMMITMENT_NOT_KEPT_WEIGHT", "CHECKIN_ANSWERED_WEIGHT", "HYGIENE_ON_TIME_WEIGHT",
]

# Owner-specified matrix (v0.39 section 11.9): the drop grows with the number of CONSECUTIVE missed check-ins.
CHECKIN_MISSED_WEIGHTS = {1: -0.02, 2: -0.04, 3: -0.06}      # the 3rd and every later consecutive miss use -0.06
CHECKIN_MISSED_WEIGHT = CHECKIN_MISSED_WEIGHTS[1]

# Owner, 2026-11-13: a check-in answered on time and well enough earns as much as a first miss costs. Before, only a whole clean day earned anything
# (+0.008 at first), so from 0.00 leaving Supervision Mode took about 24 perfect days in a row.
CHECKIN_ANSWERED_WEIGHT = 0.02

# Owner, 2026-11-13: hygiene Trust had no way up at all. A planned opening relocked on time earns the same.
HYGIENE_ON_TIME_WEIGHT = 0.02

# An answer that stayed insufficient after the follow-up window.
CHECKIN_INSUFFICIENT_WEIGHT = -0.01

# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
# System-observed facts (a deadline passed) carry full confidence.
CHECKIN_EVIDENCE_CONFIDENCE = 1.0

# Phase J3 (owner, 2026-11-04): a commitment reported as not kept is a minor Trust reduction and nothing else. Half a first missed check-in, and flat: no streak.
COMMITMENT_NOT_KEPT_WEIGHT = -0.01


def checkin_evidence_spec(kind: str, streak: int = 1) -> tuple[EvidenceType, float, float]:
    """(evidence type, raw weight, confidence) for 'missed' (with its position `streak` in a run of consecutive
    misses) or 'insufficient'."""
    if kind == "missed":
        if isinstance(streak, bool) or not isinstance(streak, int) or streak < 1:
            raise ValueError("streak must be a positive int")
        return EvidenceType.CHECKIN_MISSED, CHECKIN_MISSED_WEIGHTS[min(streak, 3)], CHECKIN_EVIDENCE_CONFIDENCE
    if kind == "insufficient":
        return EvidenceType.CHECKIN_INSUFFICIENT, CHECKIN_INSUFFICIENT_WEIGHT, CHECKIN_EVIDENCE_CONFIDENCE
    if kind == "answered":
        return EvidenceType.CHECKIN_ANSWERED, CHECKIN_ANSWERED_WEIGHT, CHECKIN_EVIDENCE_CONFIDENCE
    raise ValueError(f"unknown check-in evidence kind: {kind!r}")
