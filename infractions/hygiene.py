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
from infrastructure.decision_log import decision
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
# Owner, 2026-11-24: "the system could check chaster logs and try to find hygiene openings that weren't allowed". The lock history is read
# this often; an opening in it that this watch never saw (the PC was off or asleep) and the bot did not open itself is asked about like any
# other ("Ask, then judge"). Only openings this recent are looked at, so the first read never digs up old ones.
HISTORY_EVERY = timedelta(minutes=30)
HISTORY_LOOKBACK = timedelta(days=3)
SAME_OPENING = timedelta(minutes=5)         # Chaster's event time and its openedAt, or the bot's own opening time, differ by seconds
OPENED_EVENT, LOCKED_EVENT = "temporary_opening_opened", "temporary_opening_locked"
MISSED_OPENING_QUESTION = ("While I was away, your lock history shows a hygiene opening ({when}). Was this a planned break, or a health or comfort "
                           "need? Either is fine, and a health need never counts against anything.")

OPENING_QUESTION = ("I saw your lock open for a hygiene opening. Was this a planned break, or a health or comfort need? Either is fine, and a health need "
                    "never counts against anything.")
DURATION_TEXT = "Your planned hygiene break ran longer than allowed. Why?"
FREQUENCY_TEXT = "You've had more planned hygiene breaks this week than allowed. Why this one?"
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
                 read_opening: Callable[[str, datetime], object], get_trust_score: Callable[[], float | None], interval_seconds: int,
                 read_history: Callable[[str, datetime], list] | None = None, local_clock: Callable[[datetime], str] | None = None) -> None:
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._user = user_id
        self._domain = trust_domain
        self._signals = signals
        self._list = list_worn_locks
        self._read = read_opening
        self._score = get_trust_score
        self._max_gap = timedelta(seconds=interval_seconds * MAX_FOLLOW_GAP_INTERVALS)
        self._history = read_history
        self._clock_text = local_clock or (lambda t: t.strftime("%a %d %b %H:%M UTC"))
        self._history_at: datetime | None = None

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
            if self._history is not None and (self._history_at is None or now - self._history_at >= HISTORY_EVERY):
                try:
                    self._backfill(lock.lock_id, self._history(lock.lock_id, now), opened_at, now)
                except Exception as exc:
                    problems.append(f"{lock.lock_id} history: {type(exc).__name__}")
        if self._history is not None and (self._history_at is None or now - self._history_at >= HISTORY_EVERY):
            self._history_at = now
        self.evaluate(now)
        return problems

    def _backfill(self, lock_id: str, history: list, open_now: str | None, now: datetime) -> int:
        """Owner, 2026-11-24: openings in the lock history that this watch never saw and the bot did not open itself. Each is recorded with the
        times the history gives (a relock found = a known length) and asked about like a live one; then the usual judging applies."""
        events = []
        for e in history or []:
            try:
                events.append((str(e.get("type") or ""), parse_iso(e["createdAt"])))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        events.sort(key=lambda x: x[1])
        found = 0
        for i, (kind, at) in enumerate(events):
            if kind != OPENED_EVENT or at < now - HISTORY_LOOKBACK:
                continue
            if open_now is not None and abs(parse_iso(open_now) - at) <= SAME_OPENING:
                continue                                        # still open: the live watch has it
            relock = next((t for k, t in events[i + 1:] if k.startswith(LOCKED_EVENT)), None)
            found += apply_transition(self._core, write=lambda tx, _s, at=at, relock=relock: self._record_missed(tx, lock_id, at, relock, now))
        return found

    def _record_missed(self, tx: Transaction, lock_id: str, opened: datetime, relocked: datetime | None, now: datetime) -> int:
        lo, hi = _iso(opened - SAME_OPENING), _iso(opened + SAME_OPENING)
        if tx.fetch_one("SELECT 1 FROM hygiene_openings WHERE lock_id = ? AND opened_at >= ? AND opened_at <= ?", (lock_id, lo, hi)) is not None:
            return 0                                            # seen live (or recorded before)
        if tx.fetch_one("SELECT 1 FROM wear_exemptions WHERE opening_lock_id = ? AND opened_at >= ? AND opened_at <= ?", (lock_id, lo, hi)) is not None:
            return 0                                            # the bot opened this itself, for a reason it already knows
        opening_id = str(uuid4())
        closed = relocked or now
        tx.execute("INSERT OR IGNORE INTO hygiene_openings (id, user_id, lock_id, opened_at, last_seen_open_at, closed_at, duration_known) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (opening_id, self._user, lock_id, _iso(opened), _iso(closed), _iso(closed), 1 if relocked else 0))
        enqueue_in_transaction(tx, user_id=self._user, kind="hygiene_opening", dedupe_key=f"hygiene:opening:{opening_id}",
                               text=MISSED_OPENING_QUESTION.format(when=self._clock_text(opened)), now=now,
                               actions=(ActionSpec(action_id=f"hygiene:planned:{opening_id}", label="Planned break"),
                                        ActionSpec(action_id=f"hygiene:health:{opening_id}", label="Health or comfort need", style="success")))
        decision("hygiene", "An opening the bot never saw was found in the lock history; he is asked about it.", lock_id=lock_id,
                 opened_at=_iso(opened), relocked_at=_iso(relocked) if relocked else None)
        return 1

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
        count = tx.fetch_one("SELECT COUNT(*) AS n FROM hygiene_openings WHERE user_id = ? AND classification = 'planned' AND opened_at > ? AND opened_at <= ?",
                             (self._user, _iso(opened - ROLLING_WEEK), row["opened_at"]))["n"]
        # For the judge only, never the wearer: the limits come from Trust.
        facts = (f"The wearer answered 'planned break'. Opened {row['opened_at']}, closed {row['closed_at']}: {int((closed - opened).total_seconds() // 60)} minutes"
                 f"{'' if row['duration_known'] else ' (the close time is uncertain)'}. Allowed: {policy.session_minutes} minutes per break "
                 f"(plus {int(OVERRUN_GRACE.total_seconds() // 60)} minutes of grace) and {policy.breaks_per_week} planned breaks per rolling week; this was break "
                 f"number {count} in the last 7 days.")
        if row["duration_known"] and closed - opened > timedelta(minutes=policy.session_minutes) + OVERRUN_GRACE:
            raised += self._signals.raise_in_transaction(tx, user_id=self._user, kind=SignalKind.HYGIENE_DURATION, subject_id=row["id"], trust_domain=self._domain,
                                                         detail=DURATION_DETAIL, wearer_text=DURATION_TEXT, now=now, facts=facts, window=(opened, closed))
        if count > policy.breaks_per_week:
            raised += self._signals.raise_in_transaction(tx, user_id=self._user, kind=SignalKind.HYGIENE_FREQUENCY, subject_id=row["id"], trust_domain=self._domain,
                                                         detail=FREQUENCY_DETAIL, wearer_text=FREQUENCY_TEXT, now=now, facts=facts, window=(opened, closed))
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
            if tx.fetch_one("SELECT 1 FROM wear_exemptions WHERE opening_lock_id = ? AND status = 'approved' AND opened_at IS NOT NULL", (lock_id,)) is not None:
                return                              # the bot opened this itself, for a reason it already knows: asking again would be wrong
            opening_id = str(uuid4())
            cursor = tx.execute("INSERT OR IGNORE INTO hygiene_openings (id, user_id, lock_id, opened_at, last_seen_open_at) VALUES (?, ?, ?, ?, ?)",
                                (opening_id, self._user, lock_id, opened_at, _iso(now)))
            if cursor.rowcount == 1:
                enqueue_in_transaction(tx, user_id=self._user, kind="hygiene_opening", dedupe_key=f"hygiene:opening:{opening_id}", text=OPENING_QUESTION, now=now,
                                       actions=(ActionSpec(action_id=f"hygiene:planned:{opening_id}", label="Planned break"),
                                                ActionSpec(action_id=f"hygiene:health:{opening_id}", label="Health or comfort need", style="success")))

        apply_transition(self._core, write=write)

