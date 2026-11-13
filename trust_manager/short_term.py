"""
trust_manager/short_term.py

Short-term Trust (owner, 2026-11-13): how the wearer is doing RIGHT NOW, next to the long-term score (`trust_domain_state.score`).

One number had two jobs. The long-term score sets what the system allows (hygiene allowance, Penalty Window length, the token pool's Trust
part, the check-in level); it moves slowly, which is right for privileges. Supervision Mode asks a different question, "is it going badly at
the moment?", and with the slow score one bad week took weeks to climb out of. Short-term Trust answers that question only.

Definition: the share of good evidence among all evidence of the domain within a rolling window,

    short_term = good / (good + bad)

where good / bad are the summed effective weights (raw_weight * evidence_confidence, capped like a recalculation) of the positive / negative
pieces. It is read from the same `trust_evidence` rows the long-term score consumes, so nothing new is recorded. Left out:

- `clean_streak_bonus`: it is derived from the same check-ins that already count one by one (they would count twice);
- zero weights.

No evidence in the window: the score is None (unknown). Pure arithmetic plus one read; no clock, no writes, no events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from infrastructure.database import Transaction
from infrastructure.time_format import iso as _iso
from trust_manager.models import EvidenceType
from trust_manager.recalculation import MAX_ABS_EFFECTIVE_WEIGHT

__all__ = ["DEFAULT_WINDOW", "EXCLUDED_TYPES", "ShortTermTrust", "short_term_from", "determine_short_term_trust_in_transaction"]

DEFAULT_WINDOW = timedelta(days=14)
EXCLUDED_TYPES = frozenset({EvidenceType.CLEAN_STREAK_BONUS.value})


@dataclass(frozen=True)
class ShortTermTrust:
    score: float | None      # None: no evidence in the window
    pieces: int              # pieces of evidence that counted
    good: float              # summed positive effective weight
    bad: float               # summed |negative effective weight|
    window_days: float


def _effective(raw_weight: float, confidence: float) -> float:
    return max(-MAX_ABS_EFFECTIVE_WEIGHT, min(MAX_ABS_EFFECTIVE_WEIGHT, raw_weight * confidence))


def short_term_from(rows: Iterable[tuple[str, float, float]], window: timedelta = DEFAULT_WINDOW) -> ShortTermTrust:
    """`rows`: (evidence_type, raw_weight, evidence_confidence) of the window."""
    good = bad = 0.0
    pieces = 0
    for evidence_type, raw_weight, confidence in rows:
        if evidence_type in EXCLUDED_TYPES:
            continue
        weight = _effective(float(raw_weight), float(confidence))
        if weight == 0:
            continue
        pieces += 1
        if weight > 0:
            good += weight
        else:
            bad -= weight
    score = None if pieces == 0 else good / (good + bad)
    return ShortTermTrust(score=score, pieces=pieces, good=good, bad=bad, window_days=window.total_seconds() / 86400)


def determine_short_term_trust_in_transaction(tx: Transaction, domain_id: str, *, now: datetime,
                                              window: timedelta = DEFAULT_WINDOW) -> ShortTermTrust:
    """The narrow read for other modules (Supervision reads it inside its outbox handler's transaction). Read-only."""
    rows = tx.fetch_all(
        "SELECT evidence_type, raw_weight, evidence_confidence FROM trust_evidence WHERE domain_id = ? AND created_at > ? AND created_at <= ?",
        (domain_id, _iso(now - window), _iso(now)))
    return short_term_from(((r["evidence_type"], r["raw_weight"], r["evidence_confidence"]) for r in rows), window)
