"""v0.39 section 11.9: the midnight evaluation (token pool + clean-day streak), end to end on a real database."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from goal_management.models import GoalOutcome
from goal_management.repository import GoalManager
from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso
from release_engine.service import ReleaseEngine
from schedule_windows.windows import WindowConfig
from system.daily_evaluation import MAX_CATCH_UP_DAYS, DailyEvaluationTask
from trust_manager.models import EvidenceType
from tests.chaster_execution.test_wiring import core as wiring_core  # noqa: F401
from trust_manager.repository import TrustManager

ROOT = Path(__file__).parent.parent.parent
PRAGUE = ZoneInfo("Europe/Prague")
CFG = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00",
                         work_start="07:00", work_end="16:30", holiday_country="CZ")
U = "wearer"
MON, TUE, WED = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)


def local(d: date, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=PRAGUE).astimezone(timezone.utc)


class World:
    def __init__(self, tmp_path: Path, domain: str | None = "routine") -> None:
        self.core = CoreDatabase(tmp_path / "t.db")
        with self.core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        self.tm = TrustManager(self.core.db_path, core=self.core)
        if domain:
            self.tm.create_domain(domain_id=domain, display_name="R", description="d", created_via_consent_id="c",
                                  now=local(MON - timedelta(days=30)))
        self.engine = ReleaseEngine(self.core.db_path, core=self.core, window_config=CFG, min_offset_minutes=30,
                                    max_offset_minutes=210, max_hold_hours=72)
        self.gm = GoalManager(self.core.db_path, core=self.core)
        self.task = DailyEvaluationTask(self.core.db_path, core=self.core, window_config=CFG, user_id=U,
                                        trust_domain="routine", engine=self.engine, trust=self.tm)

    def goal(self, d: date, outcome: GoalOutcome | None, name="g"):
        g = self.gm.create_goal(name, "d", "routine", "user_proposed", now=local(d, 8))
        if outcome is not None:
            self.gm.record_evidence(g.goal_group_id, g.current_version_id, local(d, 0), local(d, 20), outcome, "p", "check_in",
                                    now=local(d, 21))
        return g

    def check_in(self, d: date, n: int, status: str):
        with self.core.transaction() as tx:
            tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, 2, ?, ?, ?, ?)", (f"c{d}{n}", U, d.isoformat(), n, iso(local(d, 18)), status,
                                                               iso(local(d, 18)), iso(local(d, 18))))

    def rows(self):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT * FROM daily_evaluations ORDER BY local_date")]

    def score(self):
        return self.tm.get_domain_state("routine").score

    def pool(self):
        with self.core.transaction() as tx:
            r = tx.fetch_one("SELECT pool FROM token_pool_state WHERE user_id = ?", (U,))
        return Decimal(r["pool"])

    def streak(self):
        with self.core.transaction() as tx:
            r = tx.fetch_one("SELECT streak FROM clean_streak_state WHERE user_id = ?", (U,))
        return r["streak"]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


class TestScheduling:
    def test_the_first_cycle_after_midnight_evaluates_yesterday_once(self, w) -> None:
        assert w.task.run(local(TUE, 0, 1)) == [MON.isoformat()]
        assert w.task.run(local(TUE, 0, 2)) == [] and w.task.run(local(TUE, 12)) == []
        assert [r["local_date"] for r in w.rows()] == [MON.isoformat()]

    def test_the_first_run_does_not_backfill_old_days(self, w) -> None:
        assert w.task.run(local(WED, 10)) == [TUE.isoformat()]

    def test_missed_days_are_caught_up_oldest_first(self, w) -> None:
        w.task.run(local(TUE, 1))
        assert w.task.run(local(MON + timedelta(days=4), 1)) == [(MON + timedelta(days=i)).isoformat() for i in (1, 2, 3)]

    def test_catch_up_is_bounded(self, w) -> None:
        w.task.run(local(TUE, 1))
        done = w.task.run(local(MON + timedelta(days=60), 1))
        assert len(done) == MAX_CATCH_UP_DAYS and done[-1] == (MON + timedelta(days=59)).isoformat()

    def test_a_missing_trust_domain_evaluates_nothing(self, tmp_path) -> None:
        world = World(tmp_path, domain=None)
        assert world.task.run(local(TUE, 1)) == [] and world.rows() == []

    def test_dst_change_does_not_skip_or_repeat_a_day(self, w) -> None:
        last_sunday = date(2026, 10, 25)                      # clocks go back: a 25-hour day
        assert w.task.run(local(last_sunday + timedelta(days=1), 0, 5)) == [last_sunday.isoformat()]


class TestTokenPool:
    def test_a_goalless_clean_day_uses_the_full_goal_ratio(self, w) -> None:
        w.task.run(local(TUE, 1))
        row = w.rows()[0]
        assert row["goals_assigned"] == 0 and row["clean"] == 1
        trust = Decimal(row["trust_used"])
        assert Decimal(row["progress"]) == ((Decimal("0.75") + Decimal("0.25") * trust) * Decimal("0.286")).quantize(Decimal("0.000001"))

    def test_progress_accumulates_and_pays_one_token_at_one(self, w) -> None:
        day = MON
        for _ in range(7):
            w.task.run(local(day + timedelta(days=1), 1))
            day += timedelta(days=1)
        total = w.engine.get_balance(U)
        assert total >= 1                                      # ~0.2 per day at the default trust: one token after ~5-6 days
        assert w.pool() < 1
        with w.core.transaction() as tx:
            reasons = {r["reason_code"] for r in tx.fetch_all("SELECT reason_code FROM token_ledger WHERE entry_type = 'earn'")}
        assert reasons == {"token_pool.earned"}

    def test_a_pool_close_to_one_pays_out_and_keeps_the_remainder(self, w) -> None:
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO token_pool_state (user_id, pool, updated_at) VALUES (?, '0.95', ?)", (U, iso(local(MON))))
        w.task.run(local(TUE, 1))
        assert w.engine.get_balance(U) == 1 and Decimal("0") <= w.pool() < Decimal("0.5")

    def test_goal_outcomes_drive_the_progress(self, tmp_path) -> None:
        world = World(tmp_path)
        world.goal(MON, GoalOutcome.MET, "a")
        world.goal(MON, GoalOutcome.MISSED, "b")
        world.task.run(local(TUE, 1))
        row = world.rows()[0]
        assert (row["goals_assigned"], row["goals_met"], row["clean"]) == (2, 1, 0)

    def test_a_goal_completed_that_day_counts_as_met_and_never_earns_by_itself(self, tmp_path) -> None:
        world = World(tmp_path)
        g = world.goal(MON, None, "a")
        world.gm.complete_goal(g.goal_group_id, "done", now=local(MON, 12))
        assert world.engine.get_balance(U) == 0               # completion alone grants nothing
        world.task.run(local(TUE, 1))
        row = world.rows()[0]
        assert (row["goals_assigned"], row["goals_met"]) == (1, 1)

    def test_partially_met_is_not_met(self, tmp_path) -> None:
        world = World(tmp_path)
        world.goal(MON, GoalOutcome.PARTIALLY_MET)
        world.task.run(local(TUE, 1))
        assert world.rows()[0]["goals_met"] == 0 and world.rows()[0]["clean"] == 0

    def test_the_latest_evidence_of_a_goal_decides(self, tmp_path) -> None:
        world = World(tmp_path)
        g = world.goal(MON, GoalOutcome.MISSED)
        world.gm.record_evidence(g.goal_group_id, g.current_version_id, local(MON, 0), local(MON, 22), GoalOutcome.MET, "p",
                                 "check_in", now=local(MON, 23))
        world.task.run(local(TUE, 1))
        row = world.rows()[0]
        assert (row["goals_assigned"], row["goals_met"]) == (1, 1)

    def test_evaluation_is_atomic_and_idempotent_for_tokens(self, w) -> None:
        with w.core.transaction() as tx:
            tx.execute("INSERT INTO token_pool_state (user_id, pool, updated_at) VALUES (?, '0.99', ?)", (U, iso(local(MON))))
        w.task.run(local(TUE, 1))
        w.task.run(local(TUE, 2))
        assert w.engine.get_balance(U) == 1


class TestCleanStreak:
    def test_clean_days_grow_the_streak_and_record_trust_evidence(self, w) -> None:
        before = w.score()
        for i in range(1, 4):
            w.task.run(local(MON + timedelta(days=i), 1))
        assert w.streak() == 3
        with w.core.transaction() as tx:
            ev = tx.fetch_all("SELECT raw_weight, evidence_type, source_entity_type FROM trust_evidence "
                              "WHERE evidence_type = ?", (EvidenceType.CLEAN_STREAK_BONUS.value,))
        assert len(ev) == 3 and {e["raw_weight"] for e in ev} == {0.008} and ev[0]["source_entity_type"] == "daily_evaluation"
        assert w.score() > before

    def test_recovery_accelerates_after_day_three_and_seven(self, w) -> None:
        for i in range(1, 10):
            w.task.run(local(MON + timedelta(days=i), 1))
        with w.core.transaction() as tx:
            weights = [r["raw_weight"] for r in tx.fetch_all(
                "SELECT raw_weight FROM trust_evidence WHERE evidence_type = 'clean_streak_bonus' ORDER BY created_at, rowid")]
        assert weights[:3] == [0.008] * 3 and weights[3:7] == [0.015] * 4 and weights[7:] == [0.025] * 2

    def test_a_missed_check_in_resets_the_streak(self, w) -> None:
        w.task.run(local(TUE, 1))
        w.check_in(TUE, 1, "missed")
        w.task.run(local(WED, 1))
        assert w.streak() == 0 and w.rows()[-1]["clean"] == 0

    def test_a_failed_goal_resets_the_streak(self, w) -> None:
        w.task.run(local(TUE, 1))
        w.goal(TUE, GoalOutcome.MISSED)
        w.task.run(local(WED, 1))
        assert w.streak() == 0

    def test_an_insufficient_check_in_is_not_clean_but_an_uncertain_one_is(self, w) -> None:
        w.check_in(MON, 1, "insufficient")
        w.task.run(local(TUE, 1))
        assert w.rows()[0]["clean"] == 0
        w.check_in(TUE, 1, "answered_uncertain")
        w.check_in(TUE, 2, "answered")
        w.task.run(local(WED, 1))
        assert w.rows()[1]["clean"] == 1 and w.rows()[1]["check_ins_due"] == 2

    def test_skipped_and_undeliverable_check_ins_do_not_count(self, w) -> None:
        w.check_in(MON, 1, "skipped")
        w.check_in(MON, 2, "undeliverable")
        w.task.run(local(TUE, 1))
        assert w.rows()[0]["check_ins_due"] == 0 and w.rows()[0]["clean"] == 1

    def test_a_confirmed_incident_makes_the_day_dirty(self, w) -> None:
        from system.composition import _LOW_SEVERITY_EVIDENCE
        from trust_manager.models import ConfirmationSource, IncidentConfirmation
        inc = w.tm.register_incident_report(rule_group_id="x", trust_domain="routine", description="d",
                                            evidence=_LOW_SEVERITY_EVIDENCE, now=local(MON, 12))
        w.tm.confirm_incident(inc.id, new_confirmation=IncidentConfirmation.CONFIRMED, source=ConfirmationSource.SYSTEM_VERIFIED,
                              evidence_description="e", now=local(MON, 12))
        w.task.run(local(TUE, 1))
        assert w.rows()[0]["violations"] == 1 and w.rows()[0]["clean"] == 0 and w.streak() == 0

    def test_trust_never_exceeds_one(self, w) -> None:
        for i in range(1, 60):
            w.task.run(local(MON + timedelta(days=i), 1))
        assert w.score() <= 1.0

    def test_a_goalless_checkinless_day_is_clean(self, w) -> None:
        w.task.run(local(TUE, 1))
        assert w.rows()[0]["clean"] == 1 and w.streak() == 1


class TestWiring:
    def test_a_completed_goal_no_longer_earns_a_token_by_itself(self, tmp_path) -> None:
        from infrastructure.consumer_registry import process_pending_events
        from penalty_engine.repository import PenaltyEngine
        from recovery_plan.repository import RecoveryPlanManager
        from system.startup import build_consumer_registry
        world = World(tmp_path)
        g = world.goal(MON, None)
        world.gm.complete_goal(g.goal_group_id, "done", now=local(MON, 12))
        registry = build_consumer_registry(world.tm, PenaltyEngine(world.core.db_path, core=world.core),
                                           RecoveryPlanManager(world.core.db_path, core=world.core),
                                           release_engine=world.engine, release_user_id=U)
        process_pending_events(world.core, registry, claimant="t", now=local(MON, 13))
        assert world.engine.get_balance(U) == 0

    def test_the_cycle_runs_the_daily_tick_before_the_release_tick(self, tmp_path) -> None:
        from infrastructure.clock import FrozenClock
        from system.periodic import run_periodic_cycle
        world = World(tmp_path)
        order: list[str] = []

        class Daily:
            def run(self, now):
                order.append("daily")

        class Release:
            engine = world.engine
            user_id = U

            def run(self, now):
                order.append("release")

            def sync(self, now):
                order.append("sync")

        assert run_periodic_cycle(world.core, "p", FrozenClock(local(TUE, 1)), rules_engine=None, executor=None,
                                  release_task=Release(), daily=Daily())
        assert order == ["daily", "release", "sync"]

    def test_the_daily_evaluation_is_wired_with_the_release_engine_only(self) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["daily_evaluation_trust_domain"].default == "routine"

    def test_composition_builds_the_daily_task_with_the_release_engine(self, wiring_core) -> None:
        from tests.chaster_execution.test_wiring import _chaster_parts
        from tests.system.test_phase2_composition import config
        from system.composition import build_rules_and_execution
        wiring = build_rules_and_execution(config(wiring_core, enable_release_engine=True), wiring_core, **_chaster_parts(wiring_core))
        assert wiring.daily is not None and wiring.daily._domain == "routine" and wiring.daily._user == "w"
        off = build_rules_and_execution(config(wiring_core), wiring_core, **_chaster_parts(wiring_core))
        assert off.daily is None


class TestOwnerRules20261113:
    """Owner, 2026-11-13: the pool no longer fills on bad days, and one incident no longer spoils days it should not."""

    def _incident(self, w, rule="x", domain="routine", at=None):
        from system.composition import _LOW_SEVERITY_EVIDENCE
        from trust_manager.models import ConfirmationSource, IncidentConfirmation
        if domain != "routine" and w.tm.get_domain_state(domain) is None:
            w.tm.create_domain(domain_id=domain, display_name="H", description="d", created_via_consent_id="c", now=local(MON - timedelta(days=30)))
        inc = w.tm.register_incident_report(rule_group_id=rule, trust_domain=domain, description="d", evidence=_LOW_SEVERITY_EVIDENCE,
                                            now=at or local(MON, 12))
        w.tm.confirm_incident(inc.id, new_confirmation=IncidentConfirmation.CONFIRMED, source=ConfirmationSource.SYSTEM_VERIFIED,
                              evidence_description="e", now=at or local(MON, 12))

    def test_a_goalless_day_uses_the_check_in_share(self, w) -> None:
        w.check_in(MON, 1, "answered")
        for n in (2, 3):
            w.check_in(MON, n, "missed")
        w.task.run(local(TUE, 1))
        row = w.rows()[0]
        trust = Decimal(row["trust_used"])
        expected = ((Decimal("0.75") / 3 + Decimal("0.25") * trust) * Decimal("0.286")).quantize(Decimal("0.000001"))
        assert Decimal(row["progress"]) == expected

    def test_all_check_ins_missed_earn_only_the_trust_part(self, w) -> None:
        for n in (1, 2, 3):
            w.check_in(MON, n, "missed")
        w.task.run(local(TUE, 1))
        row = w.rows()[0]
        assert Decimal(row["progress"]) == (Decimal("0.25") * Decimal(row["trust_used"]) * Decimal("0.286")).quantize(Decimal("0.000001"))

    def test_a_day_with_an_incident_in_any_domain_adds_nothing(self, w) -> None:
        self._incident(w, domain="hyg")
        w.task.run(local(TUE, 1))
        assert Decimal(w.rows()[0]["progress"]) == 0 and w.pool() == 0

    def test_another_domains_incident_does_not_spoil_the_day(self, w) -> None:
        self._incident(w, domain="hyg")
        w.task.run(local(TUE, 1))
        row = w.rows()[0]
        assert row["violations"] == 0 and row["clean"] == 1 and w.streak() == 1

    def test_the_repeated_misses_incident_does_not_spoil_the_next_day(self, w) -> None:
        self._incident(w, rule="CHECKIN_REPEATED_NONCOMPLIANCE", at=local(MON, 6))
        w.check_in(MON, 1, "answered")
        w.task.run(local(TUE, 1))
        row = w.rows()[0]
        assert row["violations"] == 0 and row["clean"] == 1 and Decimal(row["progress"]) > 0
