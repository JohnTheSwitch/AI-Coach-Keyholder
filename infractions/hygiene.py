"""
infractions/hygiene.py

The `HYGIENE_MISUSE` detector. It follows Chaster's own `temporary-opening` state (`GET /extensions/temporary-opening/{lockId}`, read-only) and keeps its own
record of each opening. Chaster cannot tell a shower from a medical need, and nothing may try to (philosophy 3.9), so the wearer says which it was:

- **Health or comfort need** is Mandatory Access. It is believed as stated, counts for nothing, and can be said at any time, even after "planned".
- **Planned break** is a Discretionary Hygiene Break, and only these are measured: length against the allowed session, number against the weekly allowance.
- **No answer** counts for nothing. Silence is not evidence of misuse (2.12), and it may be someone dealing with exactly what Mandatory Access exists for.

So an honest wearer can always keep a real health need out of this, and a wearer who abuses that word is outside what software can see. That is the
philosophy's trade-off, not a gap: a false accusation over a health need is the worse failure. Frequency of health needs is never looked at, not even as a
count (3.9).

A length is only judged when the opening was actually followed: if the bot was away, the close is unknown and the opening is never a duration signal.

The numbers come from `hygiene_privilege_technical_design.md` 2.2. The wearer's card never shows them, because they are derived from Trust.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4

from application.models import ActionSpec
from chaster.managed_locks import ManagedLock
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso
from infractions.signals import InfractionSignals, SignalKind
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.infractions.hygiene")

__all__ = ["HygienePolicy", "HygieneWatch", "OpeningNotAvailable", "policy_for", "parse_opened_at", "OVERRUN_GRACE", "ROLLING_WEEK", "OPENING_QUESTION", "DURATION_TEXT", "FREQUENCY_TEXT"]

# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):
# Temporary executable value pending an explicit ownership decision.
# How far past the allowed session a planned break may run before it is worth a question: nobody should be judged on the minute.
OVERRUN_GRACE = timedelta(minutes=10)
ROLLING_WEEK = timedelta(days=7)
MAX_FOLLOW_GAP_INTERVALS = 3                # openings not seen for longer than this many polls have an unknown close

OPENING_QUESTION = ("I saw your lock open for a hygiene opening. Was this a planned break, or a health or comfort need? Either is fine, and a health need "
                    "never counts against anything.")
DURATION_TEXT = ("Your planned hygiene break ran longer than allowed. If it was really a health or safety need, tell me below and that's the end of it. "
                 "Otherwise it goes to your keyholder to look at.")
FREQUENCY_TEXT = ("You've had more planned hygiene breaks this week than allowed. If this one was really a health or safety need, tell me below and it doesn't count. "
                  "Otherwise it goes to your keyholder to look at.")
DURATION_DETAIL = "A planned hygiene break ran longer than allowed."
FREQUENCY_DETAIL = "There were more planned hygiene breaks this week than allowed."


class OpeningNotAvailable(Exception):
    """The lock has no temporary-opening extension: nothing to follow, and not a problem."""


@dataclass(frozen=True)
class HygienePolicy:
    breaks_per_week: int
    session_minutes: int


_LEVELS = ((0.45, HygienePolicy(3, 20)), (0.70, HygienePolicy(5, 30)), (0.85, HygienePolicy(6, 30)))
_TOP = HygienePolicy(7, 30)


def policy_for(score: float) -> HygienePolicy:
    for threshold, policy in _LEVELS:
        if score < threshold:
            return policy
    return _TOP


def parse_opened_at(body: object) -> str | None:
    """`TemporaryOpeningStatusDto.openedAt` as this system stores it, or None when the lock is not open. Raises ValueError on a shape this code does not know."""
    if not isinstance(body, dict) or "openedAt" not in body:
        raise ValueError("unexpected temporary-opening response")
    value = body["openedAt"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("unexpected openedAt")
    return _iso(parse_iso(value))


class HygieneWatch:
    def __init__(self, db_path: str | Path | None = None, *, core: CoreDatabase | None = None, user_id: str, trust_domain: str, signals: InfractionSignals,
                 list_worn_locks: Callable[[datetime], tuple[tuple[ManagedLock, ...], list[str]]],
                 read_opening: Callable[[str, datetime], object], get_trust_score: Callable[[], float | None], interval_seconds: int) -> None:
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._user = user_id
        self._domain = trust_domain
        self._signals = signals
        self._list = list_worn_locks
        self._read = read_opening
        self._score = get_trust_score
        self._max_gap = timedelta(seconds=interval_seconds * MAX_FOLLOW_GAP_INTERVALS)

    def poll(self, now: datetime) -> list[str]:
        """Returns the problems of this pass. A lock that could not be read is simply not updated: an unread state never closes an opening."""
        locks, problems = self._list(now)
        problems = list(problems)
        for lock in locks:
            if lock.status != "locked" or lock.emergency_unlocked:
                continue
            try:
                opened_at = parse_opened_at(self._read(lock.lock_id, now))
            except OpeningNotAvailable:
                continue
            except Exception as exc:
                problems.append(f"{lock.lock_id}: {type(exc).__name__}")
                continue
            self._observe(lock.lock_id, opened_at, now)
        self.evaluate(now)
        return problems

    def classify(self, *, user_id: str, opening_id: str, health: bool, now: datetime) -> bool:
        """The wearer's answer. "Planned" can become "health" at any time and never the other way round."""
        new = "health" if health else "planned"
        allowed = ("unclassified", "planned") if health else ("unclassified",)
        marks = ",".join("?" for _ in allowed)

        def write(tx: Transaction, _s: object) -> bool:
            cursor = tx.execute(f"UPDATE hygiene_openings SET classification = ?, classified_at = ? WHERE id = ? AND user_id = ? AND classification IN ({marks})",
                                (new, _iso(now), opening_id, user_id, *allowed))
            return cursor.rowcount == 1

        return apply_transition(self._core, write=write)

    def evaluate(self, now: datetime) -> int:
        """Each finished planned break is judged exactly once, against the policy in force when it was judged."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM hygiene_openings WHERE user_id = ? AND classification = 'planned' AND closed_at IS NOT NULL AND evaluated_at IS NULL "
                                "ORDER BY opened_at", (self._user,))
        if not rows:
            return 0
        score = self._score()
        if score is None:
            return 0                                    # no judgment without its basis; the rows stay for the next pass
        policy = policy_for(score)
        raised = 0
        for row in rows:
            raised += apply_transition(self._core, write=lambda tx, _s, r=row: self._judge(tx, r, policy, now))
        return raised

    def signal_window(self, subject_id: str) -> tuple[datetime, datetime]:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT opened_at, closed_at FROM hygiene_openings WHERE id = ?", (subject_id,))
        return parse_iso(row["opened_at"]), parse_iso(row["closed_at"])

    # ------------------------------------------------------------------ internals

    def _judge(self, tx: Transaction, row, policy: HygienePolicy, now: datetime) -> int:
        tx.execute("UPDATE hygiene_openings SET evaluated_at = ? WHERE id = ?", (_iso(now), row["id"]))
        opened, closed = parse_iso(row["opened_at"]), parse_iso(row["closed_at"])
        raised = 0
        if row["duration_known"] and closed - opened > timedelta(minutes=policy.session_minutes) + OVERRUN_GRACE:
            raised += self._signals.raise_in_transaction(tx, user_id=self._user, kind=SignalKind.HYGIENE_DURATION, subject_id=row["id"], trust_domain=self._domain,
                                                         detail=DURATION_DETAIL, wearer_text=DURATION_TEXT, now=now)
        count = tx.fetch_one("SELECT COUNT(*) AS n FROM hygiene_openings WHERE user_id = ? AND classification = 'planned' AND opened_at > ? AND opened_at <= ?",
                             (self._user, _iso(opened - ROLLING_WEEK), row["opened_at"]))["n"]
        if count > policy.breaks_per_week:
            raised += self._signals.raise_in_transaction(tx, user_id=self._user, kind=SignalKind.HYGIENE_FREQUENCY, subject_id=row["id"], trust_domain=self._domain,
                                                         detail=FREQUENCY_DETAIL, wearer_text=FREQUENCY_TEXT, now=now)
        return raised

    def _observe(self, lock_id: str, opened_at: str | None, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            current = tx.fetch_one("SELECT * FROM hygiene_openings WHERE lock_id = ? AND closed_at IS NULL", (lock_id,))
            if current is not None and current["opened_at"] == opened_at:
                tx.execute("UPDATE hygiene_openings SET last_seen_open_at = ? WHERE id = ?", (_iso(now), current["id"]))
                return
            if current is not None:
                followed = now - parse_iso(current["last_seen_open_at"]) <= self._max_gap
                tx.execute("UPDATE hygiene_openings SET closed_at = ?, duration_known = ? WHERE id = ?", (_iso(now), 1 if followed else 0, current["id"]))
            if opened_at is None:
                return
            opening_id = str(uuid4())
            cursor = tx.execute("INSERT OR IGNORE INTO hygiene_openings (id, user_id, lock_id, opened_at, last_seen_open_at) VALUES (?, ?, ?, ?, ?)",
                                (opening_id, self._user, lock_id, opened_at, _iso(now)))
            if cursor.rowcount == 1:
                enqueue_in_transaction(tx, user_id=self._user, kind="hygiene_opening", dedupe_key=f"hygiene:opening:{opening_id}", text=OPENING_QUESTION, now=now,
                                       actions=(ActionSpec(action_id=f"hygiene:planned:{opening_id}", label="Planned break"),
                                                ActionSpec(action_id=f"hygiene:health:{opening_id}", label="Health or comfort need", style="success")))

        apply_transition(self._core, write=write)

