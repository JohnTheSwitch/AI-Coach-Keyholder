"""v0.39 Phase 3: check-in escalation to a low-severity Incident, and the strict-mode deadlines."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso
from supervision.checkin_evaluator import Verdict
from supervision.checkins import CheckInService, SendOutcome
from supervision.escalation import RULE_GROUP_ID, CheckInEscalation
from system.composition import _LOW_SEVERITY_EVIDENCE, build_rules_and_execution
from tests.supervision.test_checkins import CFG, MON, Env, Sender, local
from tests.system.test_phase2_composition import config
from tests.chaster_execution.test_wiring import core as wiring_core  # noqa: F401
from trust_manager.models import ConfirmationSource, IncidentConfirmation, SeverityTier

U = "wearer"
ROOT = Path(__file__).parent.parent.parent


class Incidents:
    """Fake edge: records what would be filed with the Trust Manager."""

    def __init__(self, fail_confirm=False) -> None:
        self.registered, self.confirmed, self.fail_confirm = [], [], fail_confirm

    def register(self, description, now):
        self.registered.append(description)
        return f"inc-{len(self.registered)}"

    def confirm(self, incident_id, evidence, now):
        if self.fail_confirm:
            raise RuntimeError("boom")
        self.confirmed.append(incident_id)


@pytest.fixture
def world(tmp_path):
    core = CoreDatabase(tmp_path / "t.db")
    with core.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    inc = Incidents()
    esc = CheckInEscalation(core.db_path, core=core, user_id=U, register_incident=inc.register, confirm_incident=inc.confirm)
    return core, inc, esc


def add(core, n, status, due):
    with core.transaction() as tx:
        tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, 2, ?, ?, ?, ?)", (f"c{n}", U, due.date().isoformat(), n, iso(due), status, iso(due), iso(due)))


T0 = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)


def at(hours):
    return T0 + timedelta(hours=hours)


class TestRule:
    def test_two_consecutive_misses_file_one_incident(self, world) -> None:
        core, inc, esc = world
        add(core, 1, "missed", at(0))
        esc.run(at(1))
        assert inc.registered == []
        add(core, 2, "missed", at(24))
        esc.run(at(25))
        assert len(inc.registered) == 1 and inc.confirmed == ["inc-1"]
        esc.run(at(26))
        assert len(inc.registered) == 1                       # idempotent

    def test_an_answer_in_between_breaks_the_streak(self, world) -> None:
        core, inc, esc = world
        add(core, 1, "missed", at(0))
        add(core, 2, "answered", at(24))
        add(core, 3, "missed", at(48))
        esc.run(at(49))
        assert inc.registered == []

    def test_uncertain_and_insufficient_answers_also_break_the_streak(self, world) -> None:
        core, inc, esc = world
        add(core, 1, "missed", at(0))
        add(core, 2, "answered_uncertain", at(24))
        add(core, 3, "missed", at(48))
        esc.run(at(49))
        assert inc.registered == []

    def test_skipped_and_undeliverable_prompts_are_not_misses_and_do_not_break_a_streak(self, world) -> None:
        core, inc, esc = world
        add(core, 1, "missed", at(0))
        add(core, 2, "undeliverable", at(12))
        add(core, 3, "skipped", at(13))
        add(core, 4, "needs_review", at(14))
        esc.run(at(20))
        assert inc.registered == []                           # still only one miss
        add(core, 5, "missed", at(24))
        esc.run(at(25))
        assert len(inc.registered) == 1

    def test_three_misses_within_48_hours_even_with_answers_between(self, world) -> None:
        core, inc, esc = world
        add(core, 1, "missed", at(0))
        add(core, 2, "answered", at(12))
        add(core, 3, "missed", at(24))
        add(core, 4, "answered", at(30))
        add(core, 5, "missed", at(40))
        esc.run(at(41))
        assert len(inc.registered) == 1 and "48 hours" in inc.registered[0]

    def test_three_misses_spread_over_more_than_48_hours_do_not_count(self, world) -> None:
        core, inc, esc = world
        add(core, 1, "missed", at(0))
        add(core, 2, "answered", at(12))
        add(core, 3, "missed", at(30))
        add(core, 4, "answered", at(40))
        add(core, 5, "missed", at(60))
        esc.run(at(61))                                       # the first miss is 61 h old
        assert inc.registered == []

    def test_misses_are_used_up_by_an_escalation(self, world) -> None:
        core, inc, esc = world
        add(core, 1, "missed", at(0))
        add(core, 2, "missed", at(24))
        esc.run(at(25))
        add(core, 3, "missed", at(48))
        esc.run(at(49))
        assert len(inc.registered) == 1                       # miss 3 is a new streak of one
        add(core, 4, "missed", at(72))
        esc.run(at(73))
        assert len(inc.registered) == 2                       # misses 3 + 4

    def test_a_crash_between_registering_and_confirming_is_recovered_without_a_duplicate(self, tmp_path) -> None:
        core = CoreDatabase(tmp_path / "t.db")
        with core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        inc = Incidents(fail_confirm=True)
        esc = CheckInEscalation(core.db_path, core=core, user_id=U, register_incident=inc.register, confirm_incident=inc.confirm)
        add(core, 1, "missed", at(0))
        add(core, 2, "missed", at(24))
        esc.run(at(25))
        assert len(inc.registered) == 1 and inc.confirmed == []
        inc.fail_confirm = False
        esc.run(at(26))
        assert len(inc.registered) == 1 and inc.confirmed == ["inc-1"]
        with core.transaction() as tx:
            assert [r["state"] for r in tx.fetch_all("SELECT state FROM check_in_escalations")] == ["confirmed"]

    def test_another_user_is_never_escalated(self, world) -> None:
        core, inc, esc = world
        with core.transaction() as tx:
            for n in (1, 2):
                tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                           "VALUES (?, 'other', '2026-10-12', ?, 2, ?, 'missed', ?, ?)", (f"o{n}", n, iso(at(n)), iso(at(n)), iso(at(n))))
        esc.run(at(30))
        assert inc.registered == []


class TestThroughTheRealTrustManager:
    def _real(self, tmp_path):
        env = Env(tmp_path, score=0.6)
        tm = env.tm
        esc = CheckInEscalation(
            env.core.db_path, core=env.core, user_id=U,
            register_incident=lambda d, now: tm.register_incident_report(
                rule_group_id=RULE_GROUP_ID, trust_domain="d", description=d, evidence=_LOW_SEVERITY_EVIDENCE, now=now).id,
            confirm_incident=lambda i, e, now: tm.confirm_incident(
                i, new_confirmation=IncidentConfirmation.CONFIRMED, source=ConfirmationSource.SYSTEM_VERIFIED,
                evidence_description=e, now=now),
        )
        return env, esc, tm

    def test_the_incident_is_confirmed_minor_and_reaches_the_penalty_engine_path(self, tmp_path) -> None:
        env, esc, tm = self._real(tmp_path)
        add(env.core, 1, "missed", at(0))
        add(env.core, 2, "missed", at(24))
        before = tm.get_domain_state("d").score
        esc.run(at(25))
        with env.core.transaction() as tx:
            inc = tx.fetch_one("SELECT * FROM incidents")
            events = [r["event_type"] for r in tx.fetch_all("SELECT event_type FROM domain_events ORDER BY rowid")]
        assert inc["rule_group_id"] == RULE_GROUP_ID and inc["confirmation"] == "confirmed"
        assert "incident.reported" in events and "incident.confirmation_changed" in events
        assert tm.get_incident_assessment(inc["id"]).intrinsic_severity is SeverityTier.MINOR
        assert tm.get_domain_state("d").score < before


class TestStrictDeadlines:
    def _env(self, tmp_path, strict):
        env = Env(tmp_path, score=0.3)
        env.svc = CheckInService(
            env.core.db_path, core=env.core, window_config=CFG, user_id=U, trust_domain="d", get_trust_score=lambda: env.score,
            sender=env.sender, evaluate=env._evaluate, rand_fraction=lambda: 0.5, is_strict=lambda: strict[0],
        )
        return env

    def _sent_deadline(self, env):
        env.svc.tick(local(MON, 9, 0))
        due = env.first_due()
        env.svc.tick(due)
        row = env.rows()[0]
        sent = datetime.fromisoformat(row["sent_at"].replace("Z", "+00:00"))
        return datetime.fromisoformat(row["deadline_at"].replace("Z", "+00:00")) - sent

    def test_normal_mode_gives_60_minutes(self, tmp_path) -> None:
        assert self._sent_deadline(self._env(tmp_path, [False])) == timedelta(minutes=60)

    def test_strict_supervision_mode_gives_30_minutes(self, tmp_path) -> None:
        assert self._sent_deadline(self._env(tmp_path, [True])) == timedelta(minutes=30)

    def test_the_follow_up_window_drops_from_30_to_15_minutes(self, tmp_path) -> None:
        for strict, expected in ((False, 30), (True, 15)):
            sub = tmp_path / str(strict)
            sub.mkdir()
            env = self._env(sub, [strict])
            env.verdicts = [Verdict.INSUFFICIENT]
            env.svc.tick(local(MON, 9, 0))
            due = env.first_due()
            env.svc.tick(due)
            assert env.svc.on_user_message(U, "meh", due + timedelta(minutes=5))
            env.svc.tick(due + timedelta(minutes=6))
            row = env.rows()[0]
            until = datetime.fromisoformat(row["follow_up_until"].replace("Z", "+00:00"))
            assert until - (due + timedelta(minutes=6)) == timedelta(minutes=expected)

    def test_a_failing_supervision_lookup_falls_back_to_the_normal_deadline(self, tmp_path) -> None:
        def boom():
            raise RuntimeError("x")

        env = self._env(tmp_path, [False])
        env.svc._is_strict = boom
        assert self._sent_deadline(env) == timedelta(minutes=60)


class TestWiring:
    def test_check_ins_default_to_the_routine_domain(self) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["check_in_trust_domain"].default == "routine"
        f = Config.__dataclass_fields__
        assert (f["checkin_response_timeout_minutes"].default, f["checkin_strict_response_timeout_minutes"].default) == (60, 30)
        assert (f["checkin_follow_up_minutes"].default, f["checkin_strict_follow_up_minutes"].default) == (30, 15)

    def test_composition_wires_strict_mode_and_escalation(self, wiring_core) -> None:
        w = build_rules_and_execution(
            config(wiring_core, enable_check_ins=True, enable_supervision=True, check_in_trust_domain="routine"),
            wiring_core, check_in_sender=Sender())
        assert w.check_ins._escalation is not None
        assert w.check_ins._strict_timeouts == (timedelta(minutes=30), timedelta(minutes=15))
        assert w.check_ins._timeouts == (timedelta(minutes=60), timedelta(minutes=30))
        assert w.check_ins._is_strict() is False
