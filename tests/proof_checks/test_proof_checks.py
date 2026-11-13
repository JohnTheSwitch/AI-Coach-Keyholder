"""Honesty and proof, slice 3 (owner, 2026-11-13): proof spot checks of timely Done reports. A real database, a fake Chaster, no network."""

from __future__ import annotations

import ast
import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from coach.desk import Outcome
from penalty_engine.repository import WARNING_ONLY_RULE_GROUPS
from proof_checks.service import CHECK_RATE, PROOF_DEADLINE, VOTE_LIMIT, Ports, ProofChecks, task_text
from infractions.signals import RULE_GROUPS, SignalKind
from supervision.models import TrustLevel
from tests.coach.test_desk import U, World

ROOT = Path(__file__).parent.parent.parent
MADE = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
DAY = MADE.date() + timedelta(days=1)
LOCK = "lock1"


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=timezone.utc)


class FakeChaster:
    def __init__(self) -> None:
        self.current: dict | None = None
        self.history: list[dict] = []
        self.assigned: list[str] = []
        self.unassigned = 0
        self.fail_reads = False

    def current_task(self, lock_id):
        return self.current

    def assign(self, lock_id, text):
        self.assigned.append(text)
        self.current = {"id": f"task{len(self.assigned)}", "task": text}
        return self.current["id"]

    def unassign(self, lock_id):
        self.unassigned += 1
        self.current = None

    def task_entry(self, lock_id, task_id):
        if self.fail_reads:
            raise RuntimeError("down")
        return next((h for h in self.history if h["id"] == task_id), None)

    def submit(self, when: datetime, status: str = "ongoing") -> None:
        task = self.current
        self.current = None
        self.history.append({"id": task["id"], "task": task["task"], "createdAt": when.isoformat().replace("+00:00", "Z"),
                             "peerVerification": {"_id": "rev" + task["id"], "status": status}})

    def vote(self, status: str) -> None:
        self.history[-1]["peerVerification"]["status"] = status


class P:
    def __init__(self, tmp_path, *, score=0.6, roll=0.0) -> None:
        self.w = World(tmp_path)
        d = self.w.desk
        d.local_now = lambda now: now
        d.to_utc = lambda day, t: datetime.combine(day, t, tzinfo=timezone.utc)
        d.quiet = (time(22, 0), time(7, 0))
        d.day_bounds = lambda day: (datetime.combine(day, time(0), tzinfo=timezone.utc), datetime.combine(day + timedelta(days=1), time(0), tzinfo=timezone.utc))
        self.chaster = FakeChaster()
        self.score = score
        self.halted = False
        self.failures: list[tuple[str, str]] = []
        self.checks = ProofChecks(self.w.core, user_id=U, honesty_domain="honesty", chaster=self.chaster, rng=lambda: roll, ports=Ports(
            lock_id=lambda now: LOCK, halted=lambda: self.halted, honesty_score=lambda: self.score, paused=lambda now: self.w.paused,
            today=lambda now: now.date(), local_clock=lambda i: i.strftime("%H:%M"), settle=d.settle_check,
            raise_failure=lambda row, reason, now: self.failures.append((row["commitment_text"], reason))))
        d.proof_selector = self.checks.select

    def commitment(self, text="Study for 30 minutes", part="evening") -> str:
        result = self.w.desk.submit(U, {"action": "propose_commitment", "text": text, "when": "tomorrow"}, MADE)
        self.w.desk.decide(U, result.proposal.id, accept=True, now=MADE, part=part)
        return self.w.rows("SELECT id FROM daily_commitments WHERE proposal_id = ?", result.proposal.id)[0]["id"]

    def done(self, cid, now=None) -> str:
        reply = self.w.desk.record_outcome(U, cid, Outcome.KEPT, now or at(21, 40))
        self.checks.start_pending(now or at(21, 40))
        return reply

    def events(self, kind) -> list[dict]:
        return [json.loads(r["payload_json"]) for r in self.w.rows("SELECT payload_json FROM domain_events WHERE event_type = ?", kind)]

    def check(self) -> dict:
        return self.w.rows("SELECT * FROM proof_checks")[0]

    def timing(self, cid) -> tuple[str, str]:
        row = self.w.rows("SELECT status, report_timing FROM daily_commitments WHERE id = ?", cid)[0]
        return row["status"], row["report_timing"]


@pytest.fixture
def p(tmp_path) -> P:
    return P(tmp_path)


class TestSelection:
    def test_rates_follow_the_honesty_level(self) -> None:
        assert CHECK_RATE == {TrustLevel.LEVEL_4: 0.10, TrustLevel.LEVEL_3: 0.20, TrustLevel.LEVEL_2: 0.35, TrustLevel.LEVEL_1: 0.50}

    @pytest.mark.parametrize("score, roll, picked", [(0.9, 0.09, True), (0.9, 0.11, False), (0.6, 0.34, True), (0.3, 0.49, True), (0.3, 0.51, False)])
    def test_the_roll_against_the_rate(self, tmp_path, score, roll, picked) -> None:
        p = P(tmp_path, score=score, roll=roll)
        cid = p.commitment()
        p.done(cid)
        assert (p.timing(cid)[1] == "checking") is picked
        assert bool(p.events("coach.commitment_kept")) is not picked

    def test_no_honesty_domain_means_no_checks(self, tmp_path) -> None:
        p = P(tmp_path, score=None)
        cid = p.commitment()
        assert p.done(cid) == "Noted, well done." and p.timing(cid) == ("kept", "timely")

    def test_never_during_a_vacation_or_for_a_health_topic_or_a_late_report(self, p) -> None:
        assert p.checks.select("c-health", "Rest my back, it hurts badly", at(21, 40)) is False      # the coach refuses such a commitment anyway
        p.w.paused = True
        assert p.checks.select("c-vacation", "Study for 30 minutes", at(21, 40)) is False
        p.w.paused = False
        assert p.w.rows("SELECT * FROM proof_checks") == []
        late = p.commitment(text="Read for 20 minutes", part="afternoon")
        p.done(late, now=at(23))
        assert p.timing(late) == ("kept", "late") and p.w.rows("SELECT * FROM proof_checks") == []

    def test_at_most_one_a_day(self, p) -> None:
        first, second = p.commitment(), p.commitment(text="Read for 20 minutes")
        p.done(first)
        p.chaster.submit(at(21, 50), "verified")
        p.checks.poll(at(21, 55))
        p.done(second, now=at(21, 58))
        assert len(p.w.rows("SELECT * FROM proof_checks")) == 1 and p.timing(second) == ("kept", "timely")


class TestStart:
    def test_the_task_and_the_challenge(self, p) -> None:
        cid = p.commitment()
        assert p.done(cid) == "Noted. This one gets a spot check: see my next message."
        assert p.chaster.assigned == [task_text("Study for 30 minutes")] == ["Show: Study for 30 minutes"]
        row = p.check()
        assert row["status"] == "assigned" and row["deadline"].startswith(f"{DAY}T22:40")
        note = p.w.rows("SELECT text, actions_json, immediate FROM notifications WHERE kind = 'proof_check'")[0]
        assert "until 22:40" in note["text"] and note["immediate"] == 1
        assert [a["label"] for a in json.loads(note["actions_json"])] == ["I can't show it now", "I didn't actually do it"]

    @pytest.mark.parametrize("why", ["halted", "open_task", "no_lock"])
    def test_a_check_that_cannot_start_is_cancelled_and_the_report_counts(self, p, why) -> None:
        if why == "halted":
            p.halted = True
        elif why == "open_task":
            p.chaster.current = {"id": "theirs", "task": "Something the wearer has"}
        else:
            p.checks._ports = Ports(**{**p.checks._ports.__dict__, "lock_id": lambda now: None})
        cid = p.commitment()
        p.done(cid)
        assert p.check()["status"] == "cancelled" and p.timing(cid) == ("kept", "proven")
        assert len(p.events("coach.commitment_kept")) == 1 and p.chaster.assigned == []


class TestOutcomes:
    def test_verified_counts_the_report_and_adds_honesty(self, p) -> None:
        cid = p.commitment()
        p.done(cid)
        p.chaster.submit(at(22, 10))
        assert p.checks.poll(at(22, 12)) == 0 and p.check()["status"] == "submitted"
        p.chaster.vote("verified")
        assert p.checks.poll(at(23)) == 1
        assert p.check()["outcome"] == "verified" and p.timing(cid) == ("kept", "proven")
        assert p.events("coach.commitment_kept") and p.events("proof_checks.check_passed") == [{"check_id": p.check()["id"], "trust_domain": "honesty"}]
        assert p.failures == []

    def test_rejected_earns_nothing_and_asks_the_wearer(self, p) -> None:
        cid = p.commitment()
        p.done(cid)
        p.chaster.submit(at(22, 10), "rejected")
        p.checks.poll(at(22, 12))
        assert p.timing(cid) == ("kept", "unproven") and p.failures == [("Study for 30 minutes", "rejected")]
        assert not p.events("coach.commitment_kept") and not p.events("proof_checks.check_passed")

    def test_nothing_by_the_deadline_is_missed_and_the_task_is_taken_back(self, p) -> None:
        cid = p.commitment()
        p.done(cid)
        assert p.checks.poll(at(21, 40) + PROOF_DEADLINE) == 0               # the deadline itself still waits (clock skew)
        p.checks.poll(at(21, 40) + PROOF_DEADLINE + timedelta(minutes=3))
        assert p.check()["outcome"] == "missed" and p.chaster.unassigned == 1 and p.failures == [("Study for 30 minutes", "missed")]

    def test_a_photo_after_the_deadline_is_late(self, p) -> None:
        p.done(p.commitment())
        p.chaster.submit(at(22, 50), "verified")
        p.checks.poll(at(22, 51))
        assert p.check()["outcome"] == "late" and p.failures[0][1] == "late"

    def test_a_vote_that_never_ends_counts_for_the_wearer(self, p) -> None:
        cid = p.commitment()
        p.done(cid)
        p.chaster.submit(at(22))
        p.checks.poll(at(22) + VOTE_LIMIT + timedelta(minutes=1))
        assert p.check()["outcome"] == "unknown" and p.timing(cid) == ("kept", "proven") and not p.events("proof_checks.check_passed")

    def test_an_unreadable_chaster_is_never_a_miss(self, p) -> None:
        cid = p.commitment()
        p.done(cid)
        p.chaster.fail_reads = True
        p.checks.poll(at(23))
        assert p.check()["status"] == "assigned"
        p.checks.poll(at(23) + timedelta(hours=7))
        assert p.check()["outcome"] == "unknown" and p.timing(cid) == ("kept", "proven") and p.failures == []


class TestButtons:
    def test_the_free_pass_then_not_kept(self, tmp_path) -> None:
        p = P(tmp_path)
        cid = p.commitment()
        p.done(cid)
        check = p.check()["id"]
        assert "free pass" in p.checks.press(user_id=U, check_id=check, action="pass", now=at(21, 45))
        assert p.timing(cid) == ("kept", "unproven") and p.chaster.unassigned == 1 and p.failures == []
        nxt = p.commitment(text="Read for 20 minutes")
        p.checks._ports = Ports(**{**p.checks._ports.__dict__, "today": lambda now: now.date() + timedelta(days=1)})   # another day
        p.done(nxt, now=at(21, 50))
        second = [r for r in p.w.rows("SELECT * FROM proof_checks") if r["commitment_id"] == nxt][0]["id"]
        assert "already used" in p.checks.press(user_id=U, check_id=second, action="pass", now=at(21, 55))
        assert p.timing(nxt) == ("not_kept", "retracted") and p.events("coach.commitment_missed")

    def test_owning_up_is_not_kept_and_nothing_in_honesty(self, p) -> None:
        cid = p.commitment()
        p.done(cid)
        reply = p.checks.press(user_id=U, check_id=p.check()["id"], action="retract", now=at(21, 45))
        assert "nothing more" in reply and p.timing(cid) == ("not_kept", "retracted")
        assert p.events("coach.commitment_missed") and p.failures == [] and not p.events("proof_checks.check_passed")

    def test_someone_else_cannot_press_and_a_closed_check_stays_closed(self, p) -> None:
        p.done(p.commitment())
        check = p.check()["id"]
        assert p.checks.press(user_id="other", check_id=check, action="pass", now=at(21, 45)) == "That isn't yours to decide."
        p.chaster.submit(at(21, 50))
        p.checks.poll(at(21, 51))
        assert p.checks.press(user_id=U, check_id=check, action="retract", now=at(21, 52)) == "Your photo is already in review."


class TestRules:
    def test_a_failed_check_is_warning_only_in_honesty(self) -> None:
        assert RULE_GROUPS[SignalKind.PROOF_FAILED] == "FALSE_REPORT" and "FALSE_REPORT" in WARNING_ONLY_RULE_GROUPS

    def test_the_review_watch_can_tell_a_spot_check(self, p) -> None:
        p.done(p.commitment())
        assert p.checks.is_check_task("task1") and not p.checks.is_check_task("other") and not p.checks.is_check_task(None)

    def test_it_never_touches_time_freeze_or_unlock(self) -> None:
        source = (ROOT / "proof_checks" / "chaster_tasks.py").read_text(encoding="utf-8")
        for banned in ("/unlock", "/freeze", "update-time", "/archive", "add_time", "remove_time"):
            assert banned not in source
        for path in (ROOT / "proof_checks").glob("*.py"):
            imported = {n.module for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.ImportFrom) and n.module}
            assert not any(m.startswith(("chaster_execution", "release_engine", "penalty_engine")) for m in imported), path


class TestOnByDefault:
    """Owner, 2026-11-13: on by default through setup (with consent), at level 3; additional wearers only once their invitation says so."""

    def test_setup_names_the_honesty_domain_and_tenants_do_not_get_it(self, tmp_path) -> None:
        from types import SimpleNamespace
        from system.tenant_factory import tenant_config
        from tests.system.test_tenant_factory import World as TenantWorld
        from trust_manager.domain_admin import named_trust_domains
        base = TenantWorld(tmp_path).factory._shared.config
        assert base.enable_proof_checks is True and "honesty" in named_trust_domains(base)
        assert "honesty" not in named_trust_domains(tenant_config(base, db_path=tmp_path / "t.sqlite", wearer_user_id="u"))
        assert "honesty" not in named_trust_domains(SimpleNamespace(enable_proof_checks=False, honesty_trust_domain="honesty"))

    def test_honesty_starts_at_level_3_and_the_consent_says_so(self, tmp_path) -> None:
        from datetime import datetime, timezone
        from trust_manager.domain_admin import ensure_domains_with_consent
        from trust_manager.repository import TrustManager
        w = World(tmp_path)
        ensure_domains_with_consent(w.core, ["honesty", "routine"], explanation="test", comment="test", now=datetime(2026, 11, 13, tzinfo=timezone.utc))
        trust = TrustManager(w.core.db_path, core=w.core)
        assert trust.get_domain_state("honesty").score == 0.75 and trust.get_domain_state("routine").score == 0.6
        shown = w.rows("SELECT explanation_shown FROM consent_log WHERE target_id = 'trust_domain:honesty'")[0]["explanation_shown"]
        assert "starts at score 0.75" in shown


class TestWhatIsNeverChecked:
    @pytest.mark.parametrize("text, shown", [
        ("Study for 30 minutes", True), ("Do a full workout", True), ("Read 20 pages", True), ("Clean the bedroom", True), ("Notes for the exam", True),
        ("Go to bed 30 minutes earlier", False), ("No porn today", False), ("Drink less coffee", False), ("Don’t snack after 8", False),
        ("Skip dessert", False), ("Nebudu pít alkohol", False), ("Jít spát do 23:00", False)])
    def test_what_cannot_be_photographed_is_never_challenged(self, text, shown) -> None:
        from proof_checks.service import can_be_shown
        assert can_be_shown(text) is shown

    def test_a_bedtime_commitment_is_not_picked(self, p) -> None:
        cid = p.commitment(text="Go to bed 30 minutes earlier")
        p.done(cid)
        assert p.w.rows("SELECT * FROM proof_checks") == [] and p.timing(cid) == ("kept", "timely")

    def test_no_check_reaches_into_quiet_hours(self, p) -> None:
        p.checks._ports = Ports(**{**p.checks._ports.__dict__, "quiet_between": lambda start, end: end.hour >= 22})
        assert p.checks.select("c1", "Study for 30 minutes", at(21, 10)) is False
        assert p.checks.select("c2", "Study for 30 minutes", at(20, 10)) is True


def test_conditioning_outcomes_are_in_the_decision_log() -> None:
    source = (ROOT / "release_engine" / "conditioning.py").read_text(encoding="utf-8")
    assert "release_decision(_LOGGED.get(outcome" in source and "factor" not in source.split("_LOGGED = {")[1].split("}")[0].lower().replace("the factor is", "")
