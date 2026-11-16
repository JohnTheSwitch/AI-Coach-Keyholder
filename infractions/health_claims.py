"""
infractions/health_claims.py

G5 and "judge on long emergencies" (owner, 2026-11-15; philosophy v1.17, 3.9 and 3.10). A health opening, a health exemption or a Chaster
emergency unlock is always granted at once, and nothing here ever delays, refuses or conditions one. AFTERWARDS a claim is reviewed when a
stored fact puts it in doubt:

- it lasted longer than LONG_CLAIM (3 hours, the owner's limit): always reviewed, with or without any other fact;
- a request for a break or an exemption had been declined within DECLINED_BEFORE (12 hours) before it ("an expressed wish for release just
  before").

Frequency alone is never a reason (3.9: counted and asked about by system/health_pattern.py, never evidence). A review is a `health_claim`
question on the one signals path: the wearer explains, the judge decides on that with its claim instructions (a genuine need is the default;
still unclear after one follow-up clears it), one appeal, and a claim confirmed false counts as FALSE_HEALTH_CLAIM, a full Incident in the Honesty
domain. One review per claim at most.

Two sources:
- `claim_review_signal(...)` is handed to ExemptionService, which calls it when a health opening or health exemption closes on Chaster (or a
  health opening is still open after STILL_OPEN), with the declined requests it found itself (it owns that table).
- `EmergencyWatch.poll` reads the wearer's own Chaster locks: an emergency-unlocked lock is remembered in `emergency_unlocks` (this module is its
  only writer); it is reviewed once a lock is seen locked again, or after STILL_OPEN without one, when a reason above applies.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from chaster.managed_locks import ManagedLock
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso
from infractions.signals import InfractionSignals, SignalKind

logger = logging.getLogger("ai_coach_keyholder.infractions.health_claims")

__all__ = ["LONG_CLAIM", "DECLINED_BEFORE", "STILL_OPEN", "review_reasons", "claim_review_signal", "EmergencyWatch"]

LONG_CLAIM = timedelta(hours=3)            # owner, 2026-11-15: "I would do 3 hours for the emergency"
DECLINED_BEFORE = timedelta(hours=12)      # a declined request this soon before a claim is "a wish for release just before"
STILL_OPEN = timedelta(hours=24)           # a claim still open this long is reviewed without waiting for its end
# Owner, 2026-11-16: "every emergency unlock is asked about; unexplained = a serious incident". A Chaster emergency unlock is always asked
# about now, with or without the reasons above (a genuine need is still the judge's default; silence confirms FALSE_HEALTH_CLAIM, severe).
EVERY_EMERGENCY = "every Chaster emergency unlock is asked about"
FIRST_SIGHT_LIMIT = timedelta(hours=48)    # an emergency unlock first seen later than this (e.g. an old lock on the first run) is never reviewed

CLAIM_DETAIL = {
    "opening": "A health opening was reviewed afterwards because stored facts put it in doubt.",
    "exemption": "A health exemption was reviewed afterwards because stored facts put it in doubt.",
    "emergency": "A Chaster emergency unlock was reviewed afterwards (every one is asked about since 2026-11-16).",
}
_WHAT = {"opening": "health opening", "exemption": "health exemption", "emergency": "emergency unlock"}


def _hours(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60:02d} min"


def review_reasons(*, started_at: datetime, ended_at: datetime | None, declined: list[str], now: datetime) -> list[str]:
    """Why a claim is reviewed; empty: it is never questioned. Length counts up to its end, or up to now while it is still open."""
    reasons = []
    length = (ended_at or now) - started_at
    if length > LONG_CLAIM:
        reasons.append(f"it lasted {_hours(length)}{'' if ended_at else ' and is still open'}, longer than the 3-hour limit")
    if declined:
        reasons.append(f"a request was declined shortly before it ({'; '.join(declined[:3])})")
    return reasons


def _wearer_text(what: str, reasons: list[str]) -> str:
    return (f"Your {_WHAT[what]} was granted at once, as it always is. I review it now because {' and '.join(reasons)}. "
            "If it was a real health or safety need, tell me what it was; that's all I need.")


def _facts(what: str, started_at: datetime, ended_at: datetime | None, reasons: list[str], extra: str = "") -> str:
    end = f"ended at {_iso(ended_at)}" if ended_at else "not ended yet"
    return (f"{_WHAT[what].capitalize()} started at {_iso(started_at)}, {end}. Reviewed because {'; '.join(reasons)}. {extra}").strip()


def claim_review_signal(signals: InfractionSignals, *, trust_domain: str):
    """`raise_claim(tx, row, ended_at, declined, now) -> bool` for ExemptionService (in its transaction). `row` is the wear_exemptions row,
    `declined` the requests declined in DECLINED_BEFORE before it ("<category> at <time>")."""

    def raise_claim(tx: Transaction, row: dict, ended_at: datetime | None, declined: list[str], now: datetime) -> bool:
        if not row.get("opened_at"):
            return False
        what = "opening" if row.get("kind") == "hygiene_health" else "exemption"
        started = parse_iso(row["opened_at"])
        reasons = review_reasons(started_at=started, ended_at=ended_at, declined=declined, now=now)
        if not reasons:
            return False
        filed = signals.raise_in_transaction(tx, user_id=row["user_id"], kind=SignalKind.HEALTH_CLAIM, subject_id=row["id"], trust_domain=trust_domain, now=now,
                                             detail=CLAIM_DETAIL[what], wearer_text=_wearer_text(what, reasons),
                                             facts=_facts(what, started, ended_at, reasons, f"Stated reason: {(row.get('reason') or 'none').strip()[:300]}."),
                                             window=(started, ended_at or now))
        if filed:
            decision("infractions", "A health claim is reviewed afterwards (G5): the wearer is asked, the judge decides.", claim=what, reasons="; ".join(reasons))
        return filed

    return raise_claim


class EmergencyWatch:
    def __init__(self, core: CoreDatabase, *, user_id: str, trust_domain: str, signals: InfractionSignals,
                 list_worn_locks: Callable[[datetime], tuple[tuple[ManagedLock, ...], list[str]]],
                 declined_between: Callable[[str, str, str], list[str]]) -> None:
        """`declined_between(user_id, lo, hi)`: the requests declined in [lo, hi) (exemptions.service.get_declined_requests)."""
        self._core = core
        self._user = user_id
        self._domain = trust_domain
        self._signals = signals
        self._worn = list_worn_locks
        self._declined = declined_between

    def poll(self, now: datetime) -> list[str]:
        locks, problems = self._worn(now)
        if problems:
            return problems                          # an unreadable listing says nothing about a relock
        for lock in locks:
            if lock.emergency_unlocked and lock.unlocked_at:
                self._remember(lock, now)
        relocked = any(lock.status == "locked" for lock in locks)
        with self._core.transaction() as tx:
            open_rows = [dict(r) for r in tx.fetch_all("SELECT * FROM emergency_unlocks WHERE user_id = ? AND reviewed_at IS NULL", (self._user,))]
        for row in open_rows:
            try:
                self._review(row, relocked, now)
            except Exception:
                logger.exception("An emergency unlock could not be reviewed; retrying next pass.")
        return []

    def _remember(self, lock: ManagedLock, now: datetime) -> None:
        try:
            unlocked = parse_iso(lock.unlocked_at)
        except (ValueError, TypeError):
            return
        old = now - unlocked > FIRST_SIGHT_LIMIT

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT OR IGNORE INTO emergency_unlocks (lock_id, user_id, unlocked_at, seen_at, reviewed_at, outcome) VALUES (?, ?, ?, ?, ?, ?)",
                       (lock.lock_id, self._user, _iso(unlocked), _iso(now), _iso(now) if old else None, "too_old" if old else None))

        apply_transition(self._core, write=write)

    def _review(self, row: dict, relocked: bool, now: datetime) -> None:
        unlocked = parse_iso(row["unlocked_at"])
        relocked_at = parse_iso(row["relocked_seen_at"]) if row["relocked_seen_at"] else (now if relocked and now > unlocked else None)
        if relocked_at is None and now - unlocked < STILL_OPEN:
            return
        declined = self._declined(self._user, _iso(unlocked - DECLINED_BEFORE), _iso(unlocked))
        reasons = review_reasons(started_at=unlocked, ended_at=relocked_at, declined=declined, now=now) or [EVERY_EMERGENCY]

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE emergency_unlocks SET relocked_seen_at = ?, reviewed_at = ?, outcome = ? WHERE lock_id = ? AND reviewed_at IS NULL",
                              (_iso(relocked_at) if relocked_at else None, _iso(now), "reviewed" if reasons else "not_questioned", row["lock_id"])).rowcount
            if done and reasons:
                extra = "The relock time is when a locked lock was first seen afterwards (checked every few minutes)." if relocked_at else ""
                self._signals.raise_in_transaction(tx, user_id=self._user, kind=SignalKind.HEALTH_CLAIM, subject_id=f"emergency:{row['lock_id']}",
                                                   trust_domain=self._domain, now=now, detail=CLAIM_DETAIL["emergency"],
                                                   wearer_text=_wearer_text("emergency", reasons),
                                                   facts=_facts("emergency", unlocked, relocked_at, reasons, extra), window=(unlocked, relocked_at or now))
            return bool(done)

        if apply_transition(self._core, write=write):
            decision("infractions", "Emergency unlock looked at afterwards (G5).", lock_id=row["lock_id"],
                     outcome="reviewed" if reasons else "not_questioned", reasons="; ".join(reasons) or None)
