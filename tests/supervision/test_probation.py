"""Owner, 2026-11-13: leaving Supervision Mode was too slow (from 0.00, about 24 perfect days in a row). Now a good check-in earns +0.02; leaving
supervision follows short-term Trust (tests/supervision/test_short_term.py). No network."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from trust_manager.checkin_scoring import CHECKIN_ANSWERED_WEIGHT, CHECKIN_MISSED_WEIGHTS, checkin_evidence_spec
from trust_manager.models import EvidenceType


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


class TestHygiene:
    """Owner, 2026-11-13: hygiene Trust had no way up. (The way out of supervision is short-term Trust now: test_short_term.py.)"""

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
