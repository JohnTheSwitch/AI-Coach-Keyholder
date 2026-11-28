"""
system/daily_evaluation.py

The daily evaluation tick (v0.39, section 11.9). In the first periodic cycle after local midnight (APP_TIMEZONE) the
previous local day is evaluated exactly once:

  1. facts of the day: goals assigned / met, check-ins due / ok, confirmed violations;
  2. Token Progress Pool: add P_daily; every full 1.0 earns one token (`token_pool.earned`) in the append-only ledger;
  3. Clean Day streak: a clean day extends the streak and records `CLEAN_STREAK_BONUS` Trust evidence; a dirty day
     resets the streak (a violation, failed goal or missed check-in therefore always ends the run).

Everything for one date happens in ONE transaction and is keyed by (user, date): a re-run, a restart or two instances
change nothing twice. Missed days (the process was down) are caught up, at most 7 and oldest first; the very first
run evaluates only yesterday.

Owner, 2026-11-13 (before, the pool filled on bad days and one incident spoiled days it should not):
- A day without an assigned goal uses the share of check-ins answered as its goal ratio (1.0 only when nothing was due either), and a day
  with a confirmed incident in any domain adds nothing to the pool.
- Owner, 2026-11-15: a training session verified in Chaster adds TRAINING_BONUS["full"] (0.15) or ["short"] (0.08) to the pool on an
  earning day, and on a day with goals it also counts as one more goal met.
- `violations` (clean day, probation) counts only incidents of this evaluation's own Trust domain, and never the repeated-missed-check-ins
  incident: it is created the morning after, and its misses already spoiled their own day.

Goal definition of a day: a goal is "assigned" for date d when its progress period ended on
d (the latest evidence of the goal counts) or it was marked COMPLETED on d (met). A day without any such goal has a goal
ratio of 1.0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from release_engine.service import ReleaseEngine
from release_engine.token_pool import TOKEN_SOURCE, apply_day
from schedule_windows.windows import WindowConfig
from trust_manager.clean_streak import advance, is_clean_day
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.system.daily_evaluation")

__all__ = ["DailyEvaluationTask", "DayFacts", "MAX_CATCH_UP_DAYS", "get_daily_evaluations"]

MAX_CATCH_UP_DAYS = 7
TRAINING_BONUS = {"full": Decimal("0.15"), "short": Decimal("0.08")}     # owner, 2026-11-15
WEEK_BONUS = Decimal("0.10")            # owner, 2026-11-15: every planned session of the week verified (paid on Sunday)
RAISE_BONUS = Decimal("0.05")           # owner, 2026-11-15: each weight increase earned through `log`
RAISE_BONUS_MAX = Decimal("0.10")       # a day's cap for raises
_CHECK_IN_CONCLUDED = ("answered", "answered_uncertain", "insufficient", "missed")
# Its misses already spoil their own day; the incident itself is created the next morning (owner, 2026-11-13: no double count).
_NOT_A_DAY_VIOLATION = frozenset({"CHECKIN_REPEATED_NONCOMPLIANCE"})


@dataclass(frozen=True)
class DayFacts:
    goals_assigned: int
    goals_met: int
    check_ins_due: int
    check_ins_ok: int
    violations: int
    any_violations: int = 0     # confirmed incidents of the day in ANY domain (still without the repeated-misses one): no pool progress
    training_full: int = 0      # training sessions verified in Chaster that day (owner, 2026-11-15)
    training_short: int = 0
    week_complete: bool = False   # Sunday: every planned session of the week verified (health-cancelled or slotless ones excused)
    raises: int = 0               # weight increases earned through `log` that day
    surprise: Decimal = Decimal(0)   # research a (owner, 2026-11-15): surprise rewards of tasks verified that day
    cost: Decimal = Decimal(0)    # B7 (owner, 2026-11-16): accepted temptations that day, taken off the pool (never below 0)
    board: Decimal = Decimal(0)   # P1 (owner, 2026-11-15): rewards of task-board tasks verified that day
    edge: Decimal = Decimal(0)    # owner, 2026-11-28: edging during a release session that day (+0.05, the bot ran the toy)

    @property
    def training_bonus(self) -> Decimal:
        return (TRAINING_BONUS["full"] * self.training_full + TRAINING_BONUS["short"] * self.training_short
                + (WEEK_BONUS if self.week_complete else Decimal(0)) + min(RAISE_BONUS * self.raises, RAISE_BONUS_MAX) + self.board + self.surprise + self.edge)


def get_daily_evaluations(core: CoreDatabase, user_id: str, start: date, end: date) -> list[dict]:
    """Roadmap slice 5 (weekly review): the stored evaluations of `start`..`end` (local dates, inclusive), oldest first. Read-only."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT local_date, check_ins_due, check_ins_ok, tokens_earned, progress, paused FROM daily_evaluations "
                            "WHERE user_id = ? AND local_date BETWEEN ? AND ? ORDER BY local_date", (user_id, start.isoformat(), end.isoformat()))
    return [dict(r) for r in rows]


class DailyEvaluationTask:
    def __init__(
        self, db_path: str | Path, *, core: CoreDatabase | None = None, window_config: WindowConfig, user_id: str,
        trust_domain: str, engine: ReleaseEngine, trust: TrustManager, conditioning=None, paused_between=None,
    ) -> None:
        """`conditioning` (release_engine.conditioning.ProgressiveConditioning, Phase F4, only while ENABLE_SLAVE_TRAINING is on) sets the pool's
        payout threshold to base(mode) * factor; without it the threshold is 1.0. `paused_between(start, end)` (Phase G6, iron_contract.vacation) is true
        for a local day that lies entirely inside a vacation: such a day is recorded as paused and changes neither the pool, nor the streak, nor tokens."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._cfg = window_config
        self._user = user_id
        self._domain = trust_domain
        self._engine = engine
        self._trust = trust
        self._conditioning = conditioning
        self._paused_between = paused_between

    def run(self, now: datetime) -> list[str]:
        """Evaluates every finished local day that has not been evaluated yet; returns the dates done."""
        done: list[str] = []
        for day in self._pending_days(now):
            if self._evaluate(day, now):
                done.append(day.isoformat())
        return done

    # ------------------------------------------------------------------ internals

    def _pending_days(self, now: datetime) -> list[date]:
        today = self._cfg.local(now).date()
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT MAX(local_date) AS d FROM daily_evaluations WHERE user_id = ?", (self._user,))
        first = today - timedelta(days=1) if row["d"] is None else date.fromisoformat(row["d"]) + timedelta(days=1)
        first = max(first, today - timedelta(days=MAX_CATCH_UP_DAYS))
        return [first + timedelta(days=i) for i in range((today - first).days)]

    def _bounds(self, day: date) -> tuple[datetime, datetime]:
        return self._cfg.to_utc(day, time(0, 0)), self._cfg.to_utc(day + timedelta(days=1), time(0, 0))

    def _facts(self, day: date) -> DayFacts:
        start, end = self._bounds(day)
        lo, hi = _iso(start), _iso(end)
        with self._core.transaction() as tx:
            evidence = tx.fetch_all(
                "SELECT goal_group_id, outcome FROM goal_evidence WHERE period_end >= ? AND period_end < ? "
                "ORDER BY created_at, rowid", (lo, hi))
            completed = tx.fetch_all(
                "SELECT goal_group_id FROM goals WHERE status = 'completed' AND status_changed_at >= ? AND status_changed_at < ?",
                (lo, hi))
            marks = ",".join("?" for _ in _CHECK_IN_CONCLUDED)
            check_ins = tx.fetch_all(
                f"SELECT status FROM check_ins WHERE user_id = ? AND local_date = ? AND status IN ({marks})",
                (self._user, day.isoformat(), *_CHECK_IN_CONCLUDED))
        outcome: dict[str, bool] = {}
        for r in evidence:                                  # the latest evidence of a goal decides
            outcome[r["goal_group_id"]] = r["outcome"] == "met"
        for r in completed:
            outcome[r["goal_group_id"]] = True
        from obedience.service import get_verified_training
        training = get_verified_training(self._core, self._user, day.isoformat())
        short = sum(1 for k in training if k.startswith("training:short:"))
        if outcome:                                         # a day with goals: each verified session is one more kept item
            for k in training:
                outcome[f"training:{k}"] = True
        day_incidents = [c for c in self._trust.get_confirmed_incidents_since(start)
                         if c.created_at < end and c.rule_group_id not in _NOT_A_DAY_VIOLATION]
        return DayFacts(
            goals_assigned=len(outcome), goals_met=sum(outcome.values()), check_ins_due=len(check_ins),
            check_ins_ok=sum(1 for c in check_ins if c["status"] in ("answered", "answered_uncertain")),
            violations=sum(1 for c in day_incidents if c.trust_domain == self._domain), any_violations=len(day_incidents),
            training_full=len(training) - short, training_short=short,
            week_complete=self._week_complete(day), raises=self._raises(lo, hi) if training or self._trained_before(day) else 0,
            # raises only with a verified session that day, or the day before (bug fix 2026-11-16: sets are often logged the next day)
            board=self._board(lo, hi), surprise=self._surprise(lo, hi), cost=self._cost(lo, hi), edge=self._edge(lo, hi),
        )

    def _edge(self, lo: str, hi: str) -> Decimal:
        try:
            from temptations.service import get_release_edge_rewards
            return get_release_edge_rewards(self._core, self._user, lo, hi)
        except Exception:
            logger.exception("Release edging rewards could not be read; the day counts without them.")
            return Decimal(0)

    def _cost(self, lo: str, hi: str) -> Decimal:
        try:
            from temptations.service import get_temptation_costs
            return get_temptation_costs(self._core, self._user, lo, hi)
        except Exception:
            logger.exception("Temptation costs could not be read; the day counts without them.")
            return Decimal(0)

    def _surprise(self, lo: str, hi: str) -> Decimal:
        try:
            from obedience.service import get_surprise_rewards
            return get_surprise_rewards(self._core, self._user, lo, hi)
        except Exception:
            logger.exception("Surprise rewards could not be read; the day counts without them.")
            return Decimal(0)

    def _board(self, lo: str, hi: str) -> Decimal:
        try:
            from task_board.service import get_board_rewards
            return get_board_rewards(self._core, self._user, lo, hi)
        except Exception:
            logger.exception("Board rewards could not be read; the day counts without them.")
            return Decimal(0)

    def _week_complete(self, day: date) -> bool:
        """On a Sunday: every session the plan gave Monday..Sunday was verified; one cancelled (health or an exemption) or skipped for lack of
        a free slot is excused (never counts against him); at least two must remain."""
        if day.weekday() != 6:
            return False
        from obedience.service import get_training_outcomes
        from training.service import get_planned_sessions
        monday = (day - timedelta(days=6)).isoformat()
        planned = get_planned_sessions(self._core, self._user, monday, day.isoformat())
        outcomes = get_training_outcomes(self._core, self._user, monday, day.isoformat())
        counted = 0
        for local_date in planned:
            o = outcomes.get(local_date)
            if o is not None and (o["status"] == "cancelled" or (o["status"] == "skipped" and o["outcome"] == "no_free_slot")):
                continue
            if o is None or (o["status"], o["outcome"]) != ("passed", "verified"):
                return False
            counted += 1
        return counted >= 2

    def _trained_before(self, day: date) -> bool:
        from obedience.service import get_verified_training
        return bool(get_verified_training(self._core, self._user, (day - timedelta(days=1)).isoformat()))

    def _raises(self, lo: str, hi: str) -> int:
        from training.service import get_raises
        return get_raises(self._core, self._user, lo, hi)

    def _evaluate(self, day: date, now: datetime) -> bool:
        state = self._trust.get_domain_state(self._domain)
        if state is None:
            logger.warning("Daily evaluation skipped: the Trust domain is not available.")
            return False
        score = Decimal(str(min(1.0, max(0.0, state.score))))
        facts = self._facts(day)
        if self._paused_between is not None and self._paused_between(*self._bounds(day)):
            return self._record_paused(day, facts, score, now)
        clean = is_clean_day(
            violations=facts.violations, goals_assigned=facts.goals_assigned, goals_met=facts.goals_met,
            check_ins_due=facts.check_ins_due, check_ins_ok=facts.check_ins_ok,
        )

        def write(tx: Transaction, _s: object) -> bool:
            if tx.fetch_one("SELECT 1 FROM daily_evaluations WHERE user_id = ? AND local_date = ?", (self._user, day.isoformat())):
                return False
            pool_row = tx.fetch_one("SELECT pool FROM token_pool_state WHERE user_id = ?", (self._user,))
            streak_row = tx.fetch_one("SELECT streak FROM clean_streak_state WHERE user_id = ?", (self._user,))
            pool_before = Decimal(pool_row["pool"]) if pool_row else Decimal(0)
            streak_before = int(streak_row["streak"]) if streak_row else 0

            threshold = self._conditioning.threshold_in(tx, self._user) if self._conditioning is not None else 1
            done, total = ((facts.goals_met, facts.goals_assigned) if facts.goals_assigned
                           else (facts.check_ins_ok, facts.check_ins_due))
            result = apply_day(pool_before, done, total, score, threshold, earning=facts.any_violations == 0, bonus=facts.training_bonus,
                               cost=facts.cost)
            if result.tokens_earned:
                self._engine._earn_in_transaction(
                    tx, user_id=self._user, amount=result.tokens_earned, reason_code=TOKEN_SOURCE,
                    source_event_id=f"token_pool:{self._user}:{day.isoformat()}", now=now)
            step = advance(streak_before, score, clean=clean)
            if step.delta > 0:
                self._trust._record_clean_streak_bonus_in_transaction(
                    tx, user_id=self._user, local_date=day.isoformat(), trust_domain=self._domain,
                    bonus=float(step.delta), streak=step.streak, now=now)

            tx.execute("INSERT INTO token_pool_state (user_id, pool, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET pool = excluded.pool, updated_at = excluded.updated_at",
                       (self._user, str(result.pool_after), _iso(now)))
            tx.execute("INSERT INTO clean_streak_state (user_id, streak, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET streak = excluded.streak, updated_at = excluded.updated_at",
                       (self._user, step.streak, _iso(now)))
            tx.execute(
                "INSERT INTO daily_evaluations (user_id, local_date, goals_assigned, goals_met, check_ins_due, check_ins_ok, "
                "violations, trust_used, progress, pool_after, tokens_earned, clean, streak_after, bonus, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._user, day.isoformat(), facts.goals_assigned, facts.goals_met, facts.check_ins_due, facts.check_ins_ok,
                 facts.violations, str(score), str(result.progress), str(result.pool_after), result.tokens_earned,
                 int(clean), step.streak, str(step.delta), _iso(now)))
            write_event(tx, DomainEvent(
                event_type="daily_evaluation.completed", source_module="system",
                payload={"local_date": day.isoformat(), "tokens_earned": result.tokens_earned, "clean": clean,
                         "streak": step.streak, "trust_domain": self._domain, "check_ins_due": facts.check_ins_due,
                         "check_ins_ok": facts.check_ins_ok},
                occurred_at=now))
            return True

        return bool(apply_transition(self._core, write=write))

    def correct_as_paused(self, days: list[date], *, reason: str, now: datetime) -> list[str]:
        """Owner, 2026-11-28 ("Take it back"): days that were evaluated as locked days although he was unlocked (the lock ended while the bot
        was off) are turned into paused days: their progress comes off the pool (never below 0), the streak goes back to the day before.
        Only the newest evaluated days, none of which earned a token; anything else changes nothing. The original numbers stay in the event.
        Idempotent: a day already paused is left alone. Returns the dates corrected."""
        wanted = sorted({d.isoformat() for d in days})
        if not wanted:
            return []

        def write(tx: Transaction, _s: object) -> list[str]:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM daily_evaluations WHERE user_id = ? AND local_date >= ? ORDER BY local_date",
                                                  (self._user, wanted[0]))]
            if not rows or any(r["local_date"] not in wanted for r in rows):
                return []                                        # a later day was evaluated since: not the newest days any more
            todo = [r for r in rows if not r["paused"]]
            if not todo or any(int(r["tokens_earned"] or 0) for r in todo):
                return []
            taken = sum((Decimal(str(r["progress"] or 0)) for r in todo), Decimal(0))
            pool_row = tx.fetch_one("SELECT pool FROM token_pool_state WHERE user_id = ?", (self._user,))
            pool = Decimal(pool_row["pool"]) if pool_row else Decimal(0)
            new_pool = max(Decimal(0), pool - taken)
            before = tx.fetch_one("SELECT streak_after FROM daily_evaluations WHERE user_id = ? AND local_date < ? ORDER BY local_date DESC LIMIT 1",
                                  (self._user, wanted[0]))
            streak = int(before["streak_after"]) if before is not None else 0
            for r in todo:
                tx.execute("UPDATE daily_evaluations SET paused = 1, progress = '0', clean = 0, bonus = '0', pool_after = ?, streak_after = ? "
                           "WHERE user_id = ? AND local_date = ?", (str(new_pool), streak, self._user, r["local_date"]))
            tx.execute("INSERT INTO token_pool_state (user_id, pool, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET pool = excluded.pool, updated_at = excluded.updated_at", (self._user, str(new_pool), _iso(now)))
            tx.execute("INSERT INTO clean_streak_state (user_id, streak, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET streak = excluded.streak, updated_at = excluded.updated_at", (self._user, streak, _iso(now)))
            write_event(tx, DomainEvent(
                event_type="daily_evaluation.corrected", source_module="system", occurred_at=now,
                payload={"reason": reason, "pool_before": str(pool), "pool_after": str(new_pool), "streak_after": streak,
                         "days": [{"local_date": r["local_date"], "progress": r["progress"], "clean": r["clean"], "streak_after": r["streak_after"],
                                   "bonus": r["bonus"], "pool_after": r["pool_after"]} for r in todo]}))
            return [r["local_date"] for r in todo]

        done = apply_transition(self._core, write=write) or []
        if done:
            logger.info("Daily evaluations corrected to paused days: %s (%s).", ", ".join(done), reason)
        return list(done)

    def _record_paused(self, day: date, facts: DayFacts, score: Decimal, now: datetime) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            if tx.fetch_one("SELECT 1 FROM daily_evaluations WHERE user_id = ? AND local_date = ?", (self._user, day.isoformat())):
                return False
            pool_row = tx.fetch_one("SELECT pool FROM token_pool_state WHERE user_id = ?", (self._user,))
            streak_row = tx.fetch_one("SELECT streak FROM clean_streak_state WHERE user_id = ?", (self._user,))
            tx.execute(
                "INSERT INTO daily_evaluations (user_id, local_date, goals_assigned, goals_met, check_ins_due, check_ins_ok, "
                "violations, trust_used, progress, pool_after, tokens_earned, clean, streak_after, bonus, created_at, paused) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '0', ?, 0, 0, ?, '0', ?, 1)",
                (self._user, day.isoformat(), facts.goals_assigned, facts.goals_met, facts.check_ins_due, facts.check_ins_ok, facts.violations,
                 str(score), pool_row["pool"] if pool_row else "0", int(streak_row["streak"]) if streak_row else 0, _iso(now)))
            write_event(tx, DomainEvent(
                event_type="daily_evaluation.completed", source_module="system",
                payload={"local_date": day.isoformat(), "tokens_earned": 0, "clean": False, "streak": int(streak_row["streak"]) if streak_row else 0,
                         "paused": True, "trust_domain": self._domain},
                occurred_at=now))
            return True

        return bool(apply_transition(self._core, write=write))
