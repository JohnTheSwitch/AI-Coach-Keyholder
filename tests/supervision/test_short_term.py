"""Owner, 2026-11-13: Supervision Mode follows short-term Trust (the share of good evidence in a recent window); the long-term score keeps the
privileges and the check-in level. A real database; no network."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from infrastructure.time_format import iso
from supervision.models import (
    CHECK_IN_DOMAIN_WINDOW, ENTER_BELOW, EXIT_AT_OR_ABOVE, MIN_PIECES_TO_ENTER, OTHER_DOMAIN_WINDOW, decide,
)
from supervision.repository import SupervisionService
from tests.supervision.test_supervision import NOW, core  # noqa: F401
from trust_manager.repository import TrustManager
from trust_manager.short_term import EXCLUDED_TYPES, determine_short_term_trust_in_transaction, short_term_from

ROOT = Path(__file__).parent.parent.parent
_ids = itertools.count()


def put(core, weight, at, kind="checkin_missed", domain="d", confidence=1.0):  # noqa: F811
    """One piece of evidence, as the Trust Manager stores it (written directly: these tests are about reading it)."""
    if weight < 0 and kind == "checkin_answered":
        kind = "checkin_missed"
    with core.transaction() as tx:
        tx.execute("INSERT INTO trust_evidence (id, domain_id, created_at, evidence_type, source_entity_type, source_entity_id, raw_weight, "
                   "evidence_confidence, explanation) VALUES (?, ?, ?, ?, 'test', ?, ?, ?, 'e')",
                   (f"e{next(_ids)}", domain, iso(at), kind, f"s{next(_ids)}", weight, confidence))


def good(core, at, domain="d"):  # noqa: F811
    put(core, 0.02, at, "checkin_answered", domain)


def bad(core, at, weight=-0.06, domain="d"):  # noqa: F811
    put(core, weight, at, "checkin_missed", domain)


def service(core, check_in_domain="d"):  # noqa: F811
    return SupervisionService(core.db_path, core=core, windows={check_in_domain: CHECK_IN_DOMAIN_WINDOW})


def evaluate(core, svc, at, domain="d"):  # noqa: F811
    with core.transaction() as tx:
        return svc.evaluate_in_transaction(tx, domain_id=domain, now=at)


class TestTheMeasure:
    def test_it_is_the_share_of_good_evidence(self) -> None:
        st = short_term_from([("checkin_answered", 0.02, 1.0)] * 3 + [("checkin_missed", -0.02, 1.0)])
        assert st.score == pytest.approx(0.75) and st.pieces == 4

    def test_no_evidence_is_unknown_not_zero(self) -> None:
        assert short_term_from([]).score is None

    def test_the_clean_streak_bonus_does_not_count_the_check_ins_twice(self) -> None:
        assert "clean_streak_bonus" in EXCLUDED_TYPES
        st = short_term_from([("clean_streak_bonus", 0.025, 1.0), ("checkin_missed", -0.02, 1.0)])
        assert st.score == 0.0 and st.pieces == 1

    def test_confidence_scales_and_the_cap_holds(self) -> None:
        st = short_term_from([("incident_impact", -2.0, 1.0), ("checkin_answered", 0.5, 1.0), ("x", 0.2, 0.5)])
        assert st.bad == pytest.approx(0.5) and st.good == pytest.approx(0.6)

    def test_only_the_window_counts(self, core) -> None:  # noqa: F811
        bad(core, NOW - timedelta(days=8))
        good(core, NOW - timedelta(days=1))
        with core.transaction() as tx:
            week = determine_short_term_trust_in_transaction(tx, "d", now=NOW, window=timedelta(days=7))
            fortnight = determine_short_term_trust_in_transaction(tx, "d", now=NOW, window=timedelta(days=14))
            future = determine_short_term_trust_in_transaction(tx, "d", now=NOW - timedelta(days=2), window=timedelta(days=14))
        assert week.score == 1.0 and fortnight.score < 0.5 and future.pieces == 1

    def test_the_owner_read(self, core) -> None:  # noqa: F811
        good(core, NOW)
        assert TrustManager(core.db_path, core=core).get_short_term_trust("d", now=NOW + timedelta(minutes=1)).score == 1.0


class TestTheRule:
    @pytest.mark.parametrize("active,score,pieces,expected", [
        (False, 0.39, 5, "entered"), (False, 0.39, 4, None), (False, ENTER_BELOW, 9, None), (False, 0.0, 50, "entered"),
        (False, None, 0, None), (True, 0.59, 9, None), (True, EXIT_AT_OR_ABOVE, 1, "exited"), (True, 1.0, 1, "exited"),
        (True, None, 0, None), (True, 0.1, 9, None), (False, 0.5, 9, None),
    ])
    def test_decide(self, active, score, pieces, expected) -> None:
        assert decide(active, score, pieces) == expected

    def test_the_numbers(self) -> None:
        assert (ENTER_BELOW, EXIT_AT_OR_ABOVE, MIN_PIECES_TO_ENTER) == (0.40, 0.60, 5)
        assert CHECK_IN_DOMAIN_WINDOW == timedelta(days=7) and OTHER_DOMAIN_WINDOW == timedelta(days=14)


class TestSupervision:
    def test_a_weak_recent_record_starts_it(self, core) -> None:  # noqa: F811
        svc = service(core)
        for i in range(4):
            bad(core, NOW - timedelta(hours=i + 1))
        assert evaluate(core, svc, NOW) is None                  # four pieces: too thin
        bad(core, NOW - timedelta(minutes=5))
        assert evaluate(core, svc, NOW) == "entered" and svc.is_active("d") and svc.is_active()

    def test_a_low_long_term_score_alone_does_not(self, core) -> None:  # noqa: F811
        with core.transaction() as tx:
            tx.execute("UPDATE trust_domain_state SET score = 0.0, confidence = 0.99 WHERE domain_id = 'd'")
        svc = service(core)
        for i in range(6):
            good(core, NOW - timedelta(hours=i + 1))
        assert evaluate(core, svc, NOW) is None and not svc.is_active("d")

    def test_a_good_recent_record_ends_it(self, core) -> None:  # noqa: F811
        svc = service(core)
        for i in range(5):
            bad(core, NOW - timedelta(hours=i + 1), weight=-0.02)
        evaluate(core, svc, NOW)
        for i in range(7):
            good(core, NOW + timedelta(hours=i + 1))
        assert evaluate(core, svc, NOW + timedelta(hours=8)) is None        # 0.14 good against 0.10 bad: 0.58, still in the band
        good(core, NOW + timedelta(hours=9))
        assert evaluate(core, svc, NOW + timedelta(hours=10)) == "exited" and not svc.is_active("d")

    def test_bad_evidence_ageing_out_ends_it_on_the_daily_evaluation(self, core) -> None:  # noqa: F811
        svc = service(core)
        for i in range(6):
            bad(core, NOW - timedelta(hours=i + 1))
        evaluate(core, svc, NOW)
        good(core, NOW + timedelta(days=2))
        daily = SimpleNamespace(payload={"trust_domain": "d"}, occurred_at=NOW + timedelta(days=6))
        with core.transaction() as tx:
            svc.on_daily_evaluation(tx, daily)
        assert svc.is_active("d")                                              # the misses are still in the week
        with core.transaction() as tx:
            svc.on_daily_evaluation(tx, SimpleNamespace(payload={"trust_domain": "d"}, occurred_at=NOW + timedelta(days=7, minutes=1)))
        assert not svc.is_active("d")

    def test_with_no_evidence_at_all_it_stays(self, core) -> None:  # noqa: F811
        svc = service(core)
        for i in range(6):
            bad(core, NOW - timedelta(hours=i + 1))
        evaluate(core, svc, NOW)
        assert evaluate(core, svc, NOW + timedelta(days=30)) is None and svc.is_active("d")

    def test_another_domain_looks_back_two_weeks(self, core) -> None:  # noqa: F811
        TrustManager(core.db_path, core=core).create_domain(domain_id="hyg", display_name="H", description="d", created_via_consent_id="c",
                                                            now=NOW - timedelta(days=30))
        svc = service(core)
        for i in range(5):
            bad(core, NOW - timedelta(hours=i + 1), domain="hyg")
        assert evaluate(core, svc, NOW, "hyg") == "entered"
        good(core, NOW + timedelta(days=3), domain="hyg")
        assert evaluate(core, svc, NOW + timedelta(days=8), "hyg") is None     # a week later the incidents still count there
        assert evaluate(core, svc, NOW + timedelta(days=14, minutes=1), "hyg") == "exited"

    def test_a_domain_no_longer_tracked_ends_it(self, core) -> None:  # noqa: F811
        svc = service(core)
        for i in range(5):
            bad(core, NOW - timedelta(hours=i + 1))
        evaluate(core, svc, NOW)
        with core.transaction() as tx:
            tx.execute("UPDATE trust_domains SET is_active = 0 WHERE domain_id = 'd'")
            svc.on_daily_evaluation(tx, SimpleNamespace(payload={}, occurred_at=NOW + timedelta(days=1)))
        assert not svc.is_active()

    def test_events_only_on_change_and_without_scores(self, core) -> None:  # noqa: F811
        svc = service(core)
        for i in range(5):
            bad(core, NOW - timedelta(hours=i + 1))
        evaluate(core, svc, NOW)
        evaluate(core, svc, NOW + timedelta(minutes=1))
        for i in range(30):                                  # 0.60 good against 0.30 bad: 0.67
            good(core, NOW + timedelta(hours=i + 1))
        evaluate(core, svc, NOW + timedelta(days=2))
        evaluate(core, svc, NOW + timedelta(days=2, minutes=1))
        with core.transaction() as tx:
            rows = tx.fetch_all("SELECT event_type, payload_json FROM domain_events WHERE source_module = 'supervision' ORDER BY rowid")
        assert [r["event_type"] for r in rows] == ["supervision.entered", "supervision.exited"]
        assert all('"domain_id": "d"' in r["payload_json"] and "score" not in r["payload_json"] for r in rows)


class TestTheLiveHistory:
    """The owner's routine evidence up to 2026-11-12 (weights and times only), then every check-in answered, three a day."""

    MISSES = [("2026-11-05T18:14", -0.02), ("2026-11-06T18:10", -0.02), ("2026-11-06T20:06", -0.04), ("2026-11-08T11:54", -0.06),
              ("2026-11-08T16:14", -0.06), ("2026-11-08T19:03", -0.06), ("2026-11-09T17:39", -0.06), ("2026-11-09T19:31", -0.06),
              ("2026-11-09T20:36", -0.06), ("2026-11-10T15:37", -0.01), ("2026-11-10T17:06", -0.01), ("2026-11-10T17:57", -0.01),
              ("2026-11-11T16:19", -0.02), ("2026-11-11T19:17", -0.04), ("2026-11-12T05:45", -0.06), ("2026-11-12T16:49", -0.06),
              ("2026-11-12T18:49", -0.06), ("2026-11-12T19:46", -0.045)]

    def test_a_good_week_ends_it_in_about_six_days_not_three(self, core) -> None:  # noqa: F811
        svc = service(core)
        for at, weight in self.MISSES:
            bad(core, datetime.fromisoformat(at).replace(tzinfo=timezone.utc), weight=weight)
        start = datetime(2026, 11, 13, tzinfo=timezone.utc)
        assert evaluate(core, svc, start) == "entered"
        out = None
        for day in range(1, 15):
            for hour in (16, 18, 20):
                good(core, start + timedelta(days=day - 1, hours=hour))
            if evaluate(core, svc, start + timedelta(days=day, minutes=30)) == "exited":
                out = day
                break
        assert out is not None and 4 <= out <= 7, out


class TestWiring:
    def test_the_check_in_domain_gets_the_week(self, core) -> None:  # noqa: F811
        from system.composition import build_rules_and_execution
        cfg = SimpleNamespace(db_path=core.db_path, enable_rules_engine=False, enable_chaster_execution=False, enable_supervision=True,
                              check_in_trust_domain="routine")
        svc = build_rules_and_execution(cfg, core).supervision
        assert svc.window_for("routine") == CHECK_IN_DOMAIN_WINDOW and svc.window_for("hygiene") == OTHER_DOMAIN_WINDOW

    def test_the_daily_evaluation_is_the_clock(self) -> None:
        text = (ROOT / "system" / "startup.py").read_text(encoding="utf-8")
        assert 'register("daily_evaluation.completed", "supervision", supervision.on_daily_evaluation)' in text
        assert "supervision.on_good_opening" not in text and "supervision.on_incident_confirmation)" not in text
