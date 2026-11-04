"""Phase J3: the gates between a model request and the Goal Management service. A real database; no model and no network."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coach.actions import ActionKind
from coach.desk import (
    DECLINE_MEMORY, MAX_ACTIVE_GOALS, MAX_COMMITMENTS_PER_DAY, MAX_PROPOSALS_PER_DAY, OUTCOME_WINDOW, PROPOSAL_TTL, CoachDesk, Outcome,
)
from goal_management.repository import GoalManager
from tests.application.test_lock_and_task_commands import _apply_migrations
from infrastructure.database import Database as CoreDatabase

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)
U = "wearer"
ROOT = Path(__file__).parent.parent.parent
GOAL = {"action": "propose_goal", "title": "Run", "target_description": "Three runs a week"}
COMMIT = {"action": "propose_commitment", "text": "Study for 30 minutes", "when": "tomorrow"}


class World:
    def __init__(self, tmp_path, **kw) -> None:
        self.core = CoreDatabase(tmp_path / "coach.db")
        _apply_migrations(self.core)
        self.goals = GoalManager(self.core.db_path, core=self.core)
        self.locked = False
        self.paused = False
        self.task = kw.pop("task", None)
        self.desk = CoachDesk(self.core, user_id=U, trust_domain="routine", goals=self.goals, today=lambda n: n.date(),
                              is_locked=lambda _u: self.locked, paused=lambda _n: self.paused, active_task=lambda _u: self.task)

    def goal(self, title="Study", target="An hour a day", at=NOW):
        return self.goals.create_goal(title, target, "routine", "user_proposed", now=at).goal_group_id

    def submit(self, raw=None, *, now=NOW, user=U, message=""):
        return self.desk.submit(user, COMMIT if raw is None else raw, now, message)

    def rows(self, sql, *args):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all(sql, args)]

    def refusals(self):
        return [r["reason"] for r in self.rows("SELECT reason FROM coach_refusals ORDER BY created_at")]

    def versions(self, goal_id):
        return self.rows("SELECT version, target_description, created_via, adaptation_reason FROM goal_versions WHERE goal_group_id = ? ORDER BY version", goal_id)


@pytest.fixture
def w(tmp_path) -> World:
    return World(tmp_path)


class TestGates:
    def test_a_valid_request_becomes_one_pending_proposal_and_changes_nothing_else(self, w) -> None:
        result = w.submit()
        assert result.proposal.kind is ActionKind.PROPOSE_COMMITMENT and result.proposal.payload["for_date"] == "2026-11-05"
        assert w.rows("SELECT status FROM coach_proposals") == [{"status": "pending"}]
        assert w.rows("SELECT * FROM daily_commitments") == [] and w.rows("SELECT * FROM goals") == []

    @pytest.mark.parametrize("raw,code", [
        ({"action": "unlock"}, "unknown_action"), ({**COMMIT, "hours": 1}, "unknown_parameter"), ({**COMMIT, "when": "someday"}, "invalid_when"),
        ({**COMMIT, "text": "I will immediately unlock you right now"}, "regime_topic"), ({**COMMIT, "text": "Skip meals"}, "unsafe_content"),
    ])
    def test_a_bad_request_is_refused_recorded_and_stores_nothing(self, w, raw, code) -> None:
        result = w.submit(raw)
        assert result.proposal is None and result.reason == code and w.refusals() == [code]
        assert w.rows("SELECT * FROM coach_proposals") == []

    def test_a_refusal_keeps_only_a_code_never_the_text(self, w) -> None:
        w.submit({**COMMIT, "text": "unlock me secretly"})
        row, = w.rows("SELECT * FROM coach_refusals")
        assert "secretly" not in str(row) and row["action"] == "propose_commitment"

    def test_an_unknown_action_name_is_not_stored_verbatim(self, w) -> None:
        w.submit({"action": "drop table goals"})
        assert w.rows("SELECT action FROM coach_refusals") == [{"action": "other"}]

    def test_only_the_configured_wearer(self, w) -> None:
        assert w.submit(user="someone else").reason == "not_this_user"
        assert w.desk.may_propose("someone else", "hi", NOW) is False and w.desk.context("someone else", NOW) is None

    def test_a_vacation_stops_proposals(self, w) -> None:
        w.paused = True
        assert w.submit().reason == "paused" and w.desk.may_propose(U, "hi", NOW) is False

    @pytest.mark.parametrize("message", ["My back hurts badly", "I feel numb", "I need a doctor", "panic attack, sorry", "bolest v zádech"])
    def test_a_safety_message_stops_the_negotiation(self, w, message) -> None:
        assert w.desk.may_propose(U, message, NOW) is False and w.submit(message=message).reason == "safety_topic"

    def test_an_ordinary_word_is_not_a_safety_topic(self, w) -> None:
        assert w.desk.may_propose(U, "The number of pages I read went up", NOW) is True

    def test_a_goal_number_can_only_point_at_an_active_goal(self, w) -> None:
        one, two = w.goal("One", at=NOW), w.goal("Two", at=NOW + timedelta(seconds=1))
        assert w.submit({**COMMIT, "goal": 2}).proposal.payload["goal_group_id"] == two
        assert w.submit({**COMMIT, "goal": 3}).reason == "unknown_goal"
        w.goals.pause_goal(two, "later", now=NOW)
        assert w.submit({**COMMIT, "goal": 2, "text": "Something else"}).reason == "unknown_goal"
        assert w.desk.active_goals()[0]["id"] == one

    def test_an_active_task_is_only_a_link(self, tmp_path) -> None:
        w = World(tmp_path, task=("assignment", "Daily walk"))
        assert w.submit({**COMMIT, "active_task": True}).proposal.payload["task_title"] == "Daily walk"

    def test_no_active_task_does_not_lose_the_commitment(self, w) -> None:
        payload = w.submit({**COMMIT, "active_task": True}).proposal.payload
        assert payload["text"] == COMMIT["text"] and payload["task_assignment_id"] is None

    def test_goals_are_limited(self, w) -> None:
        for i in range(MAX_ACTIVE_GOALS):
            w.goal(f"G{i}", at=NOW + timedelta(seconds=i))
        assert w.submit(GOAL).reason == "too_many_goals"

    def test_commitments_per_day_are_limited(self, w) -> None:
        for i in range(MAX_COMMITMENTS_PER_DAY):
            day = NOW + timedelta(hours=i * 24 - 24 * MAX_COMMITMENTS_PER_DAY)
            with w.core.transaction() as tx:
                tx.execute("INSERT INTO coach_proposals (id, user_id, local_date, kind, payload_json, fingerprint, status, created_at, expires_at) "
                           "VALUES (?, ?, ?, 'propose_commitment', '{}', '', 'accepted', ?, ?)", (f"p{i}", U, day.date().isoformat(), day.isoformat(), day.isoformat()))
                tx.execute("INSERT INTO daily_commitments (id, user_id, for_date, text, proposal_id, status, created_at) VALUES (?, ?, '2026-11-05', 'x', ?, 'open', 'x')",
                           (f"c{i}", U, f"p{i}"))
        assert w.submit().reason == "too_many_commitments"

    def test_at_most_a_few_proposals_a_day(self, w) -> None:
        for i in range(MAX_PROPOSALS_PER_DAY):
            assert w.submit({**COMMIT, "text": f"Thing number {i}"}, now=NOW + timedelta(minutes=i)).proposal is not None
        assert w.submit({**COMMIT, "text": "One more"}, now=NOW + timedelta(minutes=9)).reason == "daily_limit"
        assert w.desk.may_propose(U, "hi", NOW + timedelta(minutes=9)) is False
        assert w.submit({**COMMIT, "text": "One more"}, now=NOW + timedelta(days=1)).proposal is not None

    def test_a_broken_database_is_a_refusal_not_a_crash(self, w) -> None:
        w.desk._active_task = lambda _u: 1 / 0
        assert w.submit({**COMMIT, "active_task": True}).reason == "internal_error"


class TestNegotiation:
    def test_a_counter_offer_replaces_the_waiting_proposal(self, w) -> None:
        first = w.submit().proposal
        second = w.submit({**COMMIT, "text": "Study for 25 minutes"}, now=NOW + timedelta(minutes=1)).proposal
        assert w.rows("SELECT id, status FROM coach_proposals ORDER BY created_at") == [{"id": first.id, "status": "superseded"}, {"id": second.id, "status": "pending"}]
        assert "replaced" in w.desk.decide(U, first.id, accept=True, now=NOW + timedelta(minutes=2))
        assert w.rows("SELECT * FROM daily_commitments") == []

    def test_a_negotiation_cannot_change_kind(self, w) -> None:
        w.goal()
        w.submit()
        adapt = {"action": "adapt_goal", "goal": 1, "target_description": "25 minutes", "reason": "Too much."}
        assert w.submit(adapt, now=NOW + timedelta(minutes=1)).reason == "other_kind_pending"

    def test_nothing_like_a_declined_proposal_comes_back(self, w) -> None:
        proposal = w.submit().proposal
        w.desk.decide(U, proposal.id, accept=False, now=NOW)
        assert w.submit({**COMMIT, "text": "Study 30 minutes"}, now=NOW + timedelta(hours=1)).reason == "declined_recently"
        assert w.submit({**COMMIT, "text": "Read twenty pages of a novel"}, now=NOW + timedelta(hours=1)).proposal is not None

    def test_the_memory_of_a_decline_ends(self, w) -> None:
        w.desk.decide(U, w.submit().proposal.id, accept=False, now=NOW)
        assert w.submit(now=NOW + DECLINE_MEMORY + timedelta(hours=1)).proposal is not None

    def test_a_decline_of_another_kind_does_not_block(self, w) -> None:
        w.desk.decide(U, w.submit(GOAL).proposal.id, accept=False, now=NOW)
        assert w.submit({**COMMIT, "text": "Run three runs a week"}, now=NOW + timedelta(hours=1)).proposal is not None


class TestDecisions:
    def test_accepting_a_goal_creates_it_exactly_as_proposed(self, w) -> None:
        proposal = w.submit(GOAL).proposal
        assert "goal" in w.desk.decide(U, proposal.id, accept=True, now=NOW)
        goal, = w.desk.active_goals()
        assert (goal["title"], goal["target"]) == ("Run", "Three runs a week")
        assert w.rows("SELECT created_via, trust_domain FROM goal_versions") == [{"created_via": "coach_proposed_user_approved", "trust_domain": "routine"}]
        assert w.rows("SELECT status, result_id FROM coach_proposals") == [{"status": "accepted", "result_id": goal["id"]}]

    def test_declining_changes_nothing_but_the_card(self, w) -> None:
        proposal = w.submit(GOAL).proposal
        w.desk.decide(U, proposal.id, accept=False, now=NOW)
        assert w.desk.active_goals() == [] and w.rows("SELECT status FROM coach_proposals") == [{"status": "declined"}]

    def test_accepting_a_commitment_stores_it(self, w) -> None:
        goal_id = w.goal()
        proposal = w.submit({**COMMIT, "goal": 1}).proposal
        w.desk.decide(U, proposal.id, accept=True, now=NOW)
        row, = w.rows("SELECT for_date, text, goal_group_id, status FROM daily_commitments")
        assert row == {"for_date": "2026-11-05", "text": COMMIT["text"], "goal_group_id": goal_id, "status": "open"}

    def test_an_adaptation_goes_through_goal_management_and_keeps_history(self, w) -> None:
        goal_id = w.goal()
        adapt = {"action": "adapt_goal", "goal": 1, "target_description": "Half an hour a day", "reason": "An hour was too much."}
        w.desk.decide(U, w.submit(adapt).proposal.id, accept=True, now=NOW)
        assert [(v["version"], v["target_description"]) for v in w.versions(goal_id)] == [(1, "An hour a day"), (2, "Half an hour a day")]
        assert w.versions(goal_id)[1]["adaptation_reason"] == "An hour was too much."
        assert w.rows("SELECT proposed_change, status FROM goal_change_proposals") == [{"proposed_change": "adapt_target", "status": "accepted"}]

    def test_a_second_press_applies_nothing(self, w) -> None:
        proposal = w.submit(GOAL).proposal
        w.desk.decide(U, proposal.id, accept=True, now=NOW)
        assert w.desk.decide(U, proposal.id, accept=True, now=NOW) == "You already accepted that."
        assert w.desk.decide(U, proposal.id, accept=False, now=NOW) == "You already accepted that."
        assert len(w.rows("SELECT * FROM goals")) == 1

    def test_someone_else_cannot_decide(self, w) -> None:
        proposal = w.submit(GOAL).proposal
        assert "isn't yours" in w.desk.decide("stranger", proposal.id, accept=True, now=NOW)
        assert "isn't yours" in w.desk.decide(U, "no-such-id", accept=True, now=NOW)
        assert w.rows("SELECT status FROM coach_proposals") == [{"status": "pending"}] and w.desk.active_goals() == []

    def test_an_expired_card_does_nothing(self, w) -> None:
        proposal = w.submit(GOAL).proposal
        assert "expired" in w.desk.decide(U, proposal.id, accept=True, now=NOW + PROPOSAL_TTL + timedelta(minutes=1))
        assert w.desk.active_goals() == []

    def test_a_failure_puts_the_card_back_and_it_can_be_pressed_again(self, w) -> None:
        proposal = w.submit(GOAL).proposal
        original, w.goals.create_goal = w.goals.create_goal, lambda *a, **k: 1 / 0
        assert "nothing was changed" in w.desk.decide(U, proposal.id, accept=True, now=NOW)
        w.goals.create_goal = original
        assert "goal" in w.desk.decide(U, proposal.id, accept=True, now=NOW) and len(w.desk.active_goals()) == 1

    def test_a_goal_that_went_away_fails_cleanly(self, w) -> None:
        goal_id = w.goal()
        adapt = {"action": "adapt_goal", "goal": 1, "target_description": "x", "reason": "y"}
        proposal = w.submit(adapt).proposal
        w.goals.pause_goal(goal_id, "later", now=NOW)
        assert "nothing was changed" in w.desk.decide(U, proposal.id, accept=True, now=NOW)
        assert len(w.versions(goal_id)) == 1


class TestTheIronContract:
    ADAPT = {"action": "adapt_goal", "goal": 1, "target_description": "x", "reason": "y"}

    def test_an_agreed_target_cannot_be_changed_while_locked(self, w) -> None:
        w.goal()
        w.locked = True
        assert w.submit(self.ADAPT).reason == "locked"

    def test_new_goals_and_commitments_are_fine_while_locked(self, w) -> None:
        w.locked = True
        assert w.submit(GOAL).proposal is not None

    def test_the_lock_is_checked_again_when_the_button_is_pressed(self, w) -> None:
        goal_id = w.goal()
        proposal = w.submit(self.ADAPT).proposal
        w.locked = True
        assert "Not while you are locked" in w.desk.decide(U, proposal.id, accept=True, now=NOW)
        assert len(w.versions(goal_id)) == 1 and w.rows("SELECT status FROM coach_proposals") == [{"status": "pending"}]


class TestCommitmentsHaveNoConsequence:
    def accepted(self, w, when="today"):
        w.desk.decide(U, w.submit({**COMMIT, "when": when}).proposal.id, accept=True, now=NOW)
        return w.rows("SELECT id FROM daily_commitments")[0]["id"]

    @pytest.mark.parametrize("outcome", list(Outcome))
    def test_an_outcome_is_recorded_once_and_touches_nothing_else(self, w, outcome) -> None:
        commitment = self.accepted(w)
        before = {t: len(w.rows(f"SELECT * FROM {t}")) for t in ("goal_evidence", "goal_evaluations", "trust_evidence", "daily_evaluations", "penalty_windows")}
        assert w.desk.record_outcome(U, commitment, outcome, NOW) != "That commitment isn't open."
        assert w.desk.record_outcome(U, commitment, Outcome.KEPT, NOW) == "That commitment isn't open."
        assert w.rows("SELECT status FROM daily_commitments") == [{"status": outcome.value}]
        assert {t: len(w.rows(f"SELECT * FROM {t}")) for t in before} == before

    def test_someone_else_cannot_report(self, w) -> None:
        commitment = self.accepted(w)
        assert w.desk.record_outcome("stranger", commitment, Outcome.KEPT, NOW) == "That commitment isn't open."

    def test_the_commitment_to_review_is_from_before_today(self, w) -> None:
        self.accepted(w, "today")
        assert w.desk.commitment_to_review(NOW) is None
        assert w.desk.commitment_to_review(NOW + timedelta(days=1)).text == COMMIT["text"]

    def test_an_unanswered_commitment_lapses_at_no_cost(self, w) -> None:
        self.accepted(w, "today")
        later = NOW + OUTCOME_WINDOW + timedelta(days=2)
        assert w.desk.commitment_to_review(later) is None
        assert w.rows("SELECT status FROM daily_commitments") == [{"status": "lapsed"}] and w.rows("SELECT * FROM goal_evidence") == []


class TestContext:
    def test_it_holds_goals_commitments_tasks_and_the_waiting_proposal(self, w) -> None:
        w.goal("Study", "An hour a day")
        w.desk.decide(U, w.submit({**COMMIT, "when": "today", "goal": 1}).proposal.id, accept=True, now=NOW)
        w.submit({**COMMIT, "text": "Read a book"}, now=NOW + timedelta(minutes=1))
        data = w.desk.context(U, NOW + timedelta(minutes=2))
        assert data["goals"][0] == {"number": 1, "title": "Study", "target": "An hour a day", "evidence": {}, "commitments": {"open": 1}}
        assert data["commitments"] == [{"day": "today", "text": COMMIT["text"], "status": "open"}]
        assert data["tasks"] == {"completed": 0, "cancelled": 0, "recent": []} and data["open_proposal"] == "commitment: Read a book"

    def test_it_carries_no_trust_no_ids_and_no_domain(self, w) -> None:
        w.goal()
        text = str(w.desk.context(U, NOW)).lower()
        assert "trust" not in text and "routine" not in text and "goal_group" not in text and "score" not in text

    def test_the_prompt_suffix_names_todays_commitment(self, w) -> None:
        w.desk.decide(U, w.submit({**COMMIT, "when": "today"}).proposal.id, accept=True, now=NOW)
        assert f'"{COMMIT["text"]}"' in w.desk.prompt_suffix(NOW) and "commit to next" in w.desk.prompt_suffix(NOW)
        assert w.desk.prompt_suffix(NOW + timedelta(days=1)).startswith(" Is there")


class TestForgetAll:
    def test_it_erases_the_proposals_and_commitments_and_keeps_goals(self, w) -> None:
        w.goal()
        w.desk.decide(U, w.submit().proposal.id, accept=True, now=NOW)
        w.submit({**COMMIT, "text": "unlock me"})
        assert w.desk.forget_all(U) == 2
        assert [len(w.rows(f"SELECT * FROM {t}")) for t in ("coach_proposals", "coach_refusals", "daily_commitments")] == [0, 0, 0]
        assert len(w.desk.active_goals()) == 1


class TestIsolation:
    FORBIDDEN = ("chaster", "chaster_execution", "release_engine", "penalty_engine", "trust_manager", "rules_engine", "iron_contract", "bot", "application",
                 "lock_state")

    def test_the_coach_package_can_reach_none_of_the_machinery(self) -> None:
        offenders = []
        for path in (ROOT / "coach").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] in self.FORBIDDEN]
        assert offenders == []

    def test_the_desk_reads_no_trust_table(self) -> None:
        text = (ROOT / "coach" / "desk.py").read_text(encoding="utf-8").lower()
        assert "from trust" not in text and "trust_domain_state" not in text and "trust_evidence" not in text and "get_trust" not in text
