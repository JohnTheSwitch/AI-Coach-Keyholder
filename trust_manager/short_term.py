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
- the wearer's own word (owner, 2026-11-13: "if he lied, would anything catch it?"): `commitment_kept` and `commitment_not_kept`. Nothing checks
  a commitment report, and up to three a day could otherwise outweigh missed check-ins and end supervision on self-report alone. Both are left
  out, so being honest about a broken commitment costs nothing here either. They still count for the long-term score;
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

__all__ = ["DEFAULT_WINDOW", "EXCLUDED_TYPES", "SELF_REPORTED_TYPES", "ShortTermTrust", "short_term_from", "determine_short_term_trust_in_transaction"]

DEFAULT_WINDOW = timedelta(days=14)
SELF_REPORTED_TYPES = frozenset({EvidenceType.COMMITMENT_KEPT.value, EvidenceType.COMMITMENT_NOT_KEPT.value})
EXCLUDED_TYPES = frozenset({EvidenceType.CLEAN_STREAK_BONUS.value}) | SELF_REPORTED_TYPES
# Owner, 2026-11-17 (asked 2026-11-16 10:50, held 24 h, confirmed): check-ins missed or thin before he took the lock seriously do not count
# here; `misses_from` (the composition passes supervision.models.SERIOUS_FROM) is the first moment they count. They stay in the record and in
# the long-term score.
_MISS_TYPES = (EvidenceType.CHECKIN_MISSED.value, EvidenceType.CHECKIN_INSUFFICIENT.value)


def _misses_clause(misses_from: datetime | None) -> tuple[str, tuple]:
    if misses_from is None:
        return "", ()
    return " AND NOT (evidence_type IN (?, ?) AND created_at < ?)", (*_MISS_TYPES, _iso(misses_from))


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
                                              window: timedelta = DEFAULT_WINDOW, misses_from: datetime | None = None) -> ShortTermTrust:
    """The narrow read for other modules (Supervision reads it inside its outbox handler's transaction). Read-only."""
    rows = tx.fetch_all(
        "SELECT evidence_type, raw_weight, evidence_confidence, source_entity_type, source_entity_id FROM trust_evidence "
        "WHERE domain_id = ? AND created_at > ? AND created_at <= ?" + _misses_clause(misses_from)[0],
        (domain_id, _iso(now - window), _iso(now), *_misses_clause(misses_from)[1]))
    return short_term_from(_net_corrections(rows), window)


def _net_corrections(rows) -> list[tuple[str, float, float]]:
    """Bug fix 2026-11-16: an owner correction (`manual_review`, same source entity) reverses a piece; counted on its own it added the
    reversal as GOOD evidence while the miss still counted as BAD. A correction now nets with the piece it corrects (both in the window: one
    piece of the summed weight, dropped at zero); a correction whose piece already left the window is dropped with it."""
    corrections: dict[str, list] = {}
    for r in rows:
        if r["source_entity_type"] == "manual_review":
            corrections.setdefault(r["source_entity_id"], []).append(r)
    out: list[tuple[str, float, float]] = []
    for r in rows:
        if r["source_entity_type"] == "manual_review":
            continue
        fixes = corrections.pop(r["source_entity_id"], None) if r["source_entity_id"] is not None else None
        if not fixes:
            out.append((r["evidence_type"], r["raw_weight"], r["evidence_confidence"]))
            continue
        net = float(r["raw_weight"]) * float(r["evidence_confidence"]) + sum(float(f["raw_weight"]) * float(f["evidence_confidence"]) for f in fixes)
        if abs(net) > 1e-12:
            out.append((r["evidence_type"], net, 1.0))
    return out


def determine_recovery_estimate_in_transaction(tx: Transaction, domain_id: str, *, now: datetime, window: timedelta, threshold: float,
                                               future_answers: Iterable[datetime], good_weight: float,
                                               misses_from: datetime | None = None) -> datetime | None:
    """Owner, 2026-11-16 (Proposal A): when would short-term Trust reach `threshold` if every check-in at `future_answers` is answered?
    The first instant (an answer, or a midnight-UTC daily evaluation, the two moments supervision is re-evaluated) at which it would.
    None when it would not within the answers given. Read-only; a rough guide for the wearer ("about"), never a promise."""
    future = sorted(t for t in future_answers if t > now)
    if not future:
        return None
    rows = tx.fetch_all(
        "SELECT evidence_type, raw_weight, evidence_confidence, source_entity_type, source_entity_id, created_at FROM trust_evidence "
        "WHERE domain_id = ? AND created_at > ? AND created_at <= ?" + _misses_clause(misses_from)[0],
        (domain_id, _iso(now - window), _iso(now), *_misses_clause(misses_from)[1]))
    past = [(dict(r), datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))) for r in rows]
    added = [({"evidence_type": EvidenceType.CHECKIN_ANSWERED.value, "raw_weight": good_weight, "evidence_confidence": 1.0,
               "source_entity_type": "projection", "source_entity_id": f"p{i}"}, t) for i, t in enumerate(future)]
    first_day = now.replace(hour=23, minute=0, second=0, microsecond=0)
    evaluations = {*future, *(first_day + timedelta(days=k) for k in range((future[-1] - now).days + 2))}
    for moment in sorted(t for t in evaluations if now < t <= future[-1] + timedelta(days=1)):
        pieces = [r for r, t in past + added if moment - window < t <= moment]
        score = short_term_from(_net_corrections(pieces), window).score
        if score is not None and score >= threshold:
            return moment
    return None
