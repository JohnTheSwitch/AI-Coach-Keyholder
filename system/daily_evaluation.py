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
run evaluates only yesterday. Goal definition of a day: a goal is "assigned" for date d when its progress period ended on
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

__all__ = ["DailyEvaluationTask", "DayFacts", "MAX_CATCH_UP_DAYS"]

MAX_CATCH_UP_DAYS = 7
_CHECK_IN_CONCLUDED = ("answered", "answered_uncertain", "insufficient", "missed")


@dataclass(frozen=True)
class DayFacts:
    goals_assigned: int
    goals_met: int
    check_ins_due: int
    check_ins_ok: int
    violations: int


class DailyEvaluationTask:
    def __init__(
        self, db_path: str | Path, *, core: CoreDatabase | None = None, window_config: WindowConfig, user_id: str,
        trust_domain: str, engine: ReleaseEngine, trust: TrustManager, conditioning=None,
    ) -> None:
        """`conditioning` (release_engine.conditioning.ProgressiveConditioning, Phase F4, only while ENABLE_SLAVE_TRAINING is on) sets the pool's
        payout threshold to base(mode) * factor; without it the threshold is 1.0."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._cfg = window_config
        self._user = user_id
        self._domain = trust_domain
        self._engine = engine
        self._trust = trust
        self._conditioning = conditioning

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
        violations = sum(1 for c in self._trust.get_confirmed_incidents_since(start) if c.created_at < end)
        return DayFacts(
            goals_assigned=len(outcome), goals_met=sum(outcome.values()), check_ins_due=len(check_ins),
            check_ins_ok=sum(1 for c in check_ins if c["status"] in ("answered", "answered_uncertain")),
            violations=violations,
        )

    def _evaluate(self, day: date, now: datetime) -> bool:
        state = self._trust.get_domain_state(self._domain)
        if state is None:
            logger.warning("Daily evaluation skipped: the Trust domain is not available.")
            return False
        score = Decimal(str(min(1.0, max(0.0, state.score))))
        facts = self._facts(day)
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
            result = apply_day(pool_before, facts.goals_met, facts.goals_assigned, score, threshold)
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
                         "streak": step.streak},
                occurred_at=now))
            return True

        return bool(apply_transition(self._core, write=write))
