"""Owner, 2026-11-13: leaving Supervision Mode was too slow (from 0.00, about 24 perfect days in a row). Now a good check-in earns +0.02, and seven
clean days in a row (each with an answered check-in) end it whatever the score. No network."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from supervision.models import PROBATION_CLEAN_DAYS
from supervision.repository import SupervisionService
from tests.supervision.test_supervision import NOW, core  # noqa: F401
from trust_manager.checkin_scoring import CHECKIN_ANSWERED_WEIGHT, CHECKIN_MISSED_WEIGHTS, checkin_evidence_spec
from trust_manager.models import EvidenceType


def day(clean=True, due=3, ok=3, paused=False, domain="d", n=0):
    payload = {"local_date": f"2026-01-{n + 1:02d}", "clean": clean, "check_ins_due": due, "check_ins_ok": ok, "trust_domain": domain, "streak": 0}
    if paused:
        payload["paused"] = True
    return SimpleNamespace(payload=payload, occurred_at=NOW + timedelta(days=n))


def supervised(core):  # noqa: F811
    svc = SupervisionService(core.db_path, core=core)
    with core.transaction() as tx:
        assert svc.evaluate_in_transaction(tx, domain_id="d", score=0.0, confidence=0.99, now=NOW, previous_score=0.1) == "entered"
    return svc


def run(core, svc, *days):  # noqa: F811
    for d in days:
        with core.transaction() as tx:
            svc.on_daily_evaluation(tx, d)
    return svc.is_active("d")


class TestAGoodCheckInEarnsTrust:
    def test_it_matches_a_first_miss(self) -> None:
        kind, weight, confidence = checkin_evidence_spec("answered")
        assert kind is EvidenceType.CHECKIN_ANSWERED and weight == CHECKIN_ANSWERED_WEIGHT == -CHECKIN_MISSED_WEIGHTS[1] and confidence == 1.0

    def test_an_answered_check_in_is_an_event(self, tmp_path) -> None:
        from tests.supervision.test_checkins import Env, local, MON
        e = Env(tmp_path, score=0.6)
        e.svc.tick(local(MON, 9))
        due = e.first_due()
        e.svc.tick(due)
        e.svc.on_user_message("wearer", "Worked on the project for two hours and went running.", due + timedelta(minutes=5))
        e.svc.tick(due + timedelta(minutes=6))
        assert len(e.events("check_in.answered")) == 1

    def test_the_evidence_raises_the_score(self, tmp_path) -> None:
        from tests.supervision.test_checkins import Env, local, MON
        e = Env(tmp_path, score=0.6)
        before = e.tm.get_domain_state("d").score
        e.tm.record_check_in_evidence(check_in_id="c-1", trust_domain="d", kind="answered", now=local(MON, 10))
        e.tm.record_check_in_evidence(check_in_id="c-1", trust_domain="d", kind="answered", now=local(MON, 11))     # once per check-in
        assert abs(e.tm.get_domain_state("d").score - (before + CHECKIN_ANSWERED_WEIGHT)) < 1e-9

    def test_the_release_check_in_never_earns(self, tmp_path) -> None:
        from tests.supervision.test_checkins import Env, local, MON
        e = Env(tmp_path, score=0.6)
        e.svc.open_release_check_in("How was today?", local(MON, 18))
        e.svc.on_user_message("wearer", "Good, worked and trained.", local(MON, 18, 5))
        e.svc.tick(local(MON, 18, 6))
        assert e.events("check_in.answered") == []


class TestProbation:
    def test_seven_clean_days_end_it_whatever_the_score(self, core) -> None:  # noqa: F811
        svc = supervised(core)
        assert run(core, svc, *[day(n=i) for i in range(PROBATION_CLEAN_DAYS - 1)]) is True
        assert run(core, svc, day(n=PROBATION_CLEAN_DAYS)) is False

    def test_a_bad_day_starts_the_run_over(self, core) -> None:  # noqa: F811
        svc = supervised(core)
        days = [day(n=i) for i in range(5)] + [day(clean=False, ok=1, n=5)] + [day(n=6 + i) for i in range(5)]
        assert run(core, svc, *days) is True

    def test_days_with_nothing_asked_neither_count_nor_break_it(self, core) -> None:  # noqa: F811
        svc = supervised(core)
        days = [day(n=i) for i in range(3)] + [day(due=0, ok=0, n=3 + i) for i in range(10)] + [day(n=13 + i) for i in range(3)]
        assert run(core, svc, *days) is True
        assert run(core, svc, day(n=20)) is False

    def test_a_vacation_day_neither(self, core) -> None:  # noqa: F811
        svc = supervised(core)
        assert run(core, svc, *[day(paused=True, clean=False, n=i) for i in range(10)]) is True

    def test_another_domain_is_untouched(self, core) -> None:  # noqa: F811
        svc = supervised(core)
        assert run(core, svc, *[day(domain="other", n=i) for i in range(10)]) is True

    def test_after_it_a_rising_score_does_not_put_you_back(self, core) -> None:  # noqa: F811
        svc = supervised(core)
        run(core, svc, *[day(n=i) for i in range(PROBATION_CLEAN_DAYS)])
        with core.transaction() as tx:
            assert svc.evaluate_in_transaction(tx, domain_id="d", score=0.12, confidence=0.99, now=NOW + timedelta(days=8), previous_score=0.10) is None
            assert svc.evaluate_in_transaction(tx, domain_id="d", score=0.06, confidence=0.99, now=NOW + timedelta(days=9), previous_score=0.12) == "entered"

    def test_the_exit_is_an_event_and_a_decision_line(self, core) -> None:  # noqa: F811
        svc = supervised(core)
        run(core, svc, *[day(n=i) for i in range(PROBATION_CLEAN_DAYS)])
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM domain_events WHERE event_type = 'supervision.exited'")["n"] == 1

    def test_the_daily_evaluation_sends_what_probation_needs(self) -> None:
        from pathlib import Path
        source = Path("system/daily_evaluation.py").read_text(encoding="utf-8")
        assert '"trust_domain": self._domain, "check_ins_due": facts.check_ins_due' in source
        assert 'registry.register("daily_evaluation.completed", "supervision", supervision.on_daily_evaluation)' in Path("system/startup.py").read_text(encoding="utf-8")


class TestHygiene:
    """Owner, 2026-11-13: hygiene Trust had no way up, and hygiene supervision no way out."""

    def _supervised(self, core):  # noqa: F811
        svc = SupervisionService(core.db_path, core=core)
        with core.transaction() as tx:
            svc.evaluate_in_transaction(tx, domain_id="d", score=0.2, confidence=0.5, now=NOW, previous_score=0.3)
        return svc

    def _event(self, payload, at):
        return SimpleNamespace(payload=payload, occurred_at=at)

    def _pass(self, core, svc, at, handler="on_daily_evaluation", **payload):  # noqa: F811
        with core.transaction() as tx:
            getattr(svc, handler)(tx, self._event(payload or {"trust_domain": "other", "clean": False, "check_ins_due": 0, "check_ins_ok": 0}, at))
        return svc.is_active("d")

    def test_fourteen_quiet_days_with_a_good_opening_end_it(self, core) -> None:  # noqa: F811
        svc = self._supervised(core)
        self._pass(core, svc, NOW + timedelta(days=2), "on_good_opening", trust_domain="d")
        assert self._pass(core, svc, NOW + timedelta(days=13)) is True
        assert self._pass(core, svc, NOW + timedelta(days=14, minutes=1)) is False

    def test_without_any_opening_it_stays(self, core) -> None:  # noqa: F811
        svc = self._supervised(core)
        assert self._pass(core, svc, NOW + timedelta(days=30)) is True

    def test_a_confirmed_incident_starts_the_run_over(self, core) -> None:  # noqa: F811
        svc = self._supervised(core)
        self._pass(core, svc, NOW + timedelta(days=1), "on_good_opening", trust_domain="d")
        self._pass(core, svc, NOW + timedelta(days=10), "on_incident_confirmation", trust_domain="d", new_confirmation="confirmed")
        assert self._pass(core, svc, NOW + timedelta(days=15)) is True
        self._pass(core, svc, NOW + timedelta(days=16), "on_good_opening", trust_domain="d")
        assert self._pass(core, svc, NOW + timedelta(days=24, minutes=1)) is False

    def test_an_on_time_planned_opening_is_an_event_and_earns_trust(self, tmp_path) -> None:
        from tests.exemptions.test_exemption_opening import World
        from trust_manager.checkin_scoring import HYGIENE_ON_TIME_WEIGHT
        from trust_manager.models import EvidenceType
        w = World(tmp_path)
        w.service.hygiene_trust_domain = "hyg"
        w.service._allowance = lambda: (3, 20)
        card = w.service.start_hygiene("wearer-1", w.at)
        ident = card.actions[0].action_id.split(":")[2]
        w.service.press_hygiene("wearer-1", ident, "hopen", w.at)
        w.opening.state = SimpleNamespace(is_open=False, relocked_at=w.at + timedelta(minutes=15))
        w.service.end_early("wearer-1", ident, w.at + timedelta(minutes=16))
        assert w.q("SELECT COUNT(*) AS n FROM domain_events WHERE event_type = 'hygiene_opening.relocked_on_time'")[0]["n"] == 1
        assert HYGIENE_ON_TIME_WEIGHT == 0.02 and EvidenceType.HYGIENE_ON_TIME.value == "hygiene_on_time"

    def test_a_health_opening_or_a_late_one_is_no_event(self, tmp_path) -> None:
        from tests.exemptions.test_exemption_opening import World
        w = World(tmp_path)
        w.service.hygiene_trust_domain = "hyg"
        w.service._allowance = lambda: (3, 20)
        card = w.service.start_hygiene("wearer-1", w.at)
        ident = card.actions[0].action_id.split(":")[2]
        w.service.press_hygiene("wearer-1", ident, "hopen", w.at)
        w.opening.state = SimpleNamespace(is_open=False, relocked_at=w.at + timedelta(minutes=45))
        w.service.end_early("wearer-1", ident, w.at + timedelta(minutes=46))
        assert w.q("SELECT COUNT(*) AS n FROM domain_events WHERE event_type = 'hygiene_opening.relocked_on_time'")[0]["n"] == 0
