"""Owner, 2026-11-13: a kept commitment earns Trust (before, commitments could only lower it), and a goal counts through the commitments tied to it
(before, nothing ever recorded goal evidence). Strict Supervision Mode keeps the normal time to answer. A real database; no model and no network."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from coach.desk import Outcome
from goal_management.repository import record_reported_result_in_transaction
from tests.coach.test_desk import NOW, U, World
from tests.coach.test_desk import COMMIT
from trust_manager.checkin_scoring import COMMITMENT_KEPT_WEIGHT, COMMITMENT_NOT_KEPT_WEIGHT
from trust_manager.models import EvidenceType
from trust_manager.repository import TrustManager

ROOT = Path(__file__).parent.parent.parent


def bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


@pytest.fixture
def w(tmp_path) -> World:
    world = World(tmp_path)
    world.desk.local_now = lambda now: now - timedelta(hours=8)
    world.desk.day_bounds = bounds
    return world


def accepted(w, goal_id=None) -> str:
    w.desk.decide(U, w.submit({**COMMIT, "when": "today"}).proposal.id, accept=True, now=NOW)
    commitment = w.rows("SELECT id FROM daily_commitments")[0]["id"]
    if goal_id:
        with w.core.transaction() as tx:
            tx.execute("UPDATE daily_commitments SET goal_group_id = ? WHERE id = ?", (goal_id, commitment))
    return commitment


def events(w, kind):
    return [json.loads(r["payload_json"]) for r in w.rows("SELECT payload_json FROM domain_events WHERE event_type = ?", kind)]


class TestAKeptCommitmentEarns:
    def test_it_mirrors_the_cost(self) -> None:
        assert COMMITMENT_KEPT_WEIGHT == -COMMITMENT_NOT_KEPT_WEIGHT == 0.01

    def test_kept_raises_the_event_once(self, w) -> None:
        commitment = accepted(w)
        w.desk.record_outcome(U, commitment, Outcome.KEPT, NOW)
        w.desk.record_outcome(U, commitment, Outcome.KEPT, NOW)
        assert [e["commitment_id"] for e in events(w, "coach.commitment_kept")] == [commitment]
        assert events(w, "coach.commitment_missed") == []

    @pytest.mark.parametrize("outcome", [Outcome.PARTLY, Outcome.NOT_KEPT])
    def test_partly_and_not_kept_do_not_earn(self, w, outcome) -> None:
        w.desk.record_outcome(U, accepted(w), outcome, NOW)
        assert events(w, "coach.commitment_kept") == []

    def test_not_during_a_vacation(self, w) -> None:
        commitment = accepted(w)
        w.paused = True
        w.desk.record_outcome(U, commitment, Outcome.KEPT, NOW)
        assert events(w, "coach.commitment_kept") == []

    def test_the_evidence_raises_the_score_once(self, tmp_path) -> None:
        world = World(tmp_path)
        tm = TrustManager(world.core.db_path, core=world.core)
        tm.create_domain(domain_id="routine", display_name="R", description="d", created_via_consent_id="c", now=NOW - timedelta(days=1))
        with world.core.transaction() as tx:
            tx.execute("UPDATE trust_domain_state SET score = 0.5 WHERE domain_id = 'routine'")
        for _ in range(2):
            with world.core.transaction() as tx:
                tm._record_commitment_kept_in_transaction(tx, commitment_id="c1", trust_domain="routine", now=NOW)
        assert abs(tm.get_domain_state("routine").score - 0.51) < 1e-9
        assert world.rows("SELECT evidence_type FROM trust_evidence") == [{"evidence_type": EvidenceType.COMMITMENT_KEPT.value}]


class TestAGoalCountsThroughItsCommitments:
    @pytest.mark.parametrize("outcome,result", [(Outcome.KEPT, "met"), (Outcome.NOT_KEPT, "missed")])
    def test_a_goal_commitment_reports_the_day(self, w, outcome, result) -> None:
        goal = w.goal()
        commitment = accepted(w, goal)
        w.desk.record_outcome(U, commitment, outcome, NOW)
        (e,) = events(w, "coach.goal_result_reported")
        start, end = bounds(NOW.date())
        assert e["goal_group_id"] == goal and e["outcome"] == result and e["commitment_id"] == commitment
        assert start <= datetime.fromisoformat(e["period_end"].replace("Z", "+00:00")) < end

    def test_a_late_report_still_lands_in_its_day(self, w) -> None:
        commitment = accepted(w, w.goal())
        w.desk.record_outcome(U, commitment, Outcome.KEPT, NOW + timedelta(days=2))
        (e,) = events(w, "coach.goal_result_reported")
        start, end = bounds(NOW.date())
        assert start <= datetime.fromisoformat(e["period_end"].replace("Z", "+00:00")) < end

    def test_partly_or_no_goal_reports_nothing(self, w) -> None:
        w.desk.record_outcome(U, accepted(w, w.goal()), Outcome.PARTLY, NOW)
        assert events(w, "coach.goal_result_reported") == []

    def test_without_a_goal_nothing(self, w) -> None:
        w.desk.record_outcome(U, accepted(w), Outcome.KEPT, NOW)
        assert events(w, "coach.goal_result_reported") == []

    def test_the_consumer_records_evidence_once(self, w) -> None:
        goal = w.goal()
        start, end = bounds(NOW.date())
        for _ in range(2):
            with w.core.transaction() as tx:
                record_reported_result_in_transaction(tx, goal_group_id=goal, commitment_id="c1", outcome="met",
                                                      period_start=start.isoformat(), period_end=NOW.isoformat(), now=NOW)
        rows = w.rows("SELECT outcome, source FROM goal_evidence WHERE goal_group_id = ?", goal)
        assert rows == [{"outcome": "met", "source": "user_report"}]
        assert len(w.rows("SELECT 1 FROM domain_events WHERE event_type = 'goal_evidence.recorded'")) == 1

    def test_an_inactive_goal_gets_none(self, w) -> None:
        goal = w.goal()
        with w.core.transaction() as tx:
            tx.execute("UPDATE goals SET status = 'paused' WHERE goal_group_id = ?", (goal,))
            assert record_reported_result_in_transaction(tx, goal_group_id=goal, commitment_id="c1", outcome="met",
                                                         period_start=NOW.isoformat(), period_end=NOW.isoformat(), now=NOW) is False

    def test_the_daily_evaluation_then_counts_the_goal(self, tmp_path) -> None:
        from tests.system.test_daily_evaluation import MON, TUE, World as DayWorld, local
        world = DayWorld(tmp_path)
        goal = world.gm.create_goal("Bed", "Earlier", "routine", "user_proposed", now=local(MON, 8)).goal_group_id
        with world.core.transaction() as tx:
            record_reported_result_in_transaction(tx, goal_group_id=goal, commitment_id="c1", outcome="missed",
                                                  period_start=local(MON).isoformat(), period_end=local(MON, 21).isoformat(), now=local(MON, 21))
        world.task.run(local(TUE, 1))
        row = world.rows()[0]
        assert row["goals_assigned"] == 1 and row["goals_met"] == 0 and row["clean"] == 0

    def test_the_wiring(self) -> None:
        text = (ROOT / "system" / "startup.py").read_text(encoding="utf-8")
        assert 'register("coach.commitment_kept", "trust_manager"' in text
        assert 'register("coach.goal_result_reported", "goal_management"' in text
        assert "day_bounds=" in (ROOT / "bot" / "discord_bot.py").read_text(encoding="utf-8")


class TestStrictModeKeepsTheNormalTimeToAnswer:
    def test_the_deadline_is_the_normal_one(self, tmp_path) -> None:
        from tests.supervision.test_checkins import Env, local, MON
        e = Env(tmp_path, score=0.1)                    # strict Supervision Mode
        e.svc.tick(local(MON, 9))
        due = e.first_due()
        e.svc.tick(due)
        with e.svc._core.transaction() as tx:
            row = tx.fetch_one("SELECT sent_at, deadline_at FROM check_ins WHERE status = 'sent'")
        sent = datetime.fromisoformat(row["sent_at"].replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(row["deadline_at"].replace("Z", "+00:00"))
        assert deadline - sent == timedelta(minutes=60)
