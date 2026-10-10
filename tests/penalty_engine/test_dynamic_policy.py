"""v0.39 section 11.9: dynamic Penalty Window duration, warning-only first minor offense, mode-dependent hard caps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from infrastructure.database import Database as CoreDatabase
from penalty_engine.window import target_active_hours
from system.penalty_policy import make_penalty_engine
from trust_manager.models import (
    BreachDirectness as B, ConfirmationSource, EvidenceConfidenceLevel, ImpactLevel as I, IncidentConfirmation,
    IncidentEvidence, IntentAssessment, RepetitionEvidence, SeverityTier,
)
from trust_manager.repository import TrustManager

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent
MINOR = (I.LOW, B.PARTIAL)
MODERATE = (I.MEDIUM, B.PARTIAL)
MAJOR = (I.HIGH, B.DIRECT)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.core = CoreDatabase(tmp_path / "t.db")
        with self.core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        self.tm = TrustManager(self.core.db_path, core=self.core)
        self.tm.create_domain(domain_id="d", display_name="D", description="d", created_via_consent_id="c", now=NOW)
        self.pe = make_penalty_engine(self.core)
        self.n = 0

    def set_trust(self, score: float) -> None:
        with self.core.transaction() as tx:
            tx.execute("UPDATE trust_domain_state SET score = ? WHERE domain_id = 'd'", (score,))

    def advanced(self) -> None:
        with self.core.transaction() as tx:
            tx.execute("UPDATE operating_mode_state SET current_mode = 'advanced' WHERE id = 1")

    def consume(self, kind, rule="r", at=NOW):
        """Registers, confirms and consumes an incident; returns the window or None."""
        self.n += 1
        impact, breach = kind
        inc = self.tm.register_incident_report(
            rule_group_id=rule, trust_domain="d", description=f"i{self.n}",
            evidence=IncidentEvidence(actual_or_potential_impact=impact, rule_breach_directness=breach,
                                      repetition=RepetitionEvidence(same_rule_confirmed_count=0, evaluation_window_days=30),
                                      evidence_confidence=EvidenceConfidenceLevel.HIGH, intentionality=IntentAssessment.UNCLEAR),
            now=at)
        self.tm.confirm_incident(inc.id, new_confirmation=IncidentConfirmation.CONFIRMED,
                                 source=ConfirmationSource.SYSTEM_VERIFIED, evidence_description="e", now=at)
        a = self.tm.get_incident_assessment(inc.id)
        return inc, self.pe.consume_confirmed_incident(inc.id, "d", rule, a.intrinsic_severity, a.cooperation, now=at)

    def windows(self):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT * FROM penalty_windows ORDER BY created_at")]

    def warnings(self):
        with self.core.transaction() as tx:
            return [r["incident_id"] for r in tx.fetch_all("SELECT incident_id FROM incident_warnings")]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


class TestDuration:
    @staticmethod
    def _expected(trust: float, lo: int, hi: int) -> int:
        return int(round((hi - trust * (hi - lo)) * 24))

    def test_a_moderate_incident_follows_the_formula_at_the_trust_when_it_is_consumed(self, w) -> None:
        w.set_trust(0.0)
        _, window = w.consume(MODERATE)
        trust_now = w.tm.get_domain_state("d").score      # the incident's own Trust drop is already applied
        assert window.max_target_hours == 336
        assert window.base_duration_hours == self._expected(trust_now, 3, 5)

    def test_higher_trust_gives_a_shorter_window(self, tmp_path) -> None:
        hours = []
        for i, trust in enumerate((0.0, 0.5, 1.0)):
            (tmp_path / str(i)).mkdir()
            world = World(tmp_path / str(i))
            world.set_trust(trust)
            hours.append(world.consume(MODERATE)[1].base_duration_hours)
        assert hours[0] > hours[1] > hours[2] and 72 <= hours[2] and hours[0] == 120

    def test_a_severe_incident_starts_at_most_half_the_mode_limit(self, w) -> None:
        w.set_trust(0.0)
        _, window = w.consume(MAJOR)
        assert window.base_duration_hours == 168 and window.base_duration_hours <= window.max_target_hours / 2

    def test_advanced_mode_has_longer_ranges_and_a_720_hour_cap(self, w) -> None:
        w.advanced()
        w.set_trust(0.0)
        _, window = w.consume(MAJOR)
        assert window.base_duration_hours == 360 and window.max_target_hours == 720
        assert w.windows()[0]["max_target_hours"] == 720

    def test_the_cap_is_fixed_when_the_window_starts(self, w) -> None:
        w.set_trust(0.0)
        w.consume(MODERATE)
        w.advanced()                                           # switching mode later does not change the open window
        assert w.windows()[0]["max_target_hours"] == 336


class TestWarningOnly:
    def test_a_first_minor_offense_is_a_warning_without_a_window(self, w) -> None:
        inc, window = w.consume(MINOR)
        assert window is None and w.windows() == [] and w.warnings() == [inc.id]
        with w.core.transaction() as tx:
            assert tx.fetch_one("SELECT 1 FROM domain_events WHERE event_type = 'penalty_warning.recorded'")

    def test_the_warning_still_costs_trust_through_the_incident_evidence(self, w) -> None:
        before = w.tm.get_domain_state("d").score
        w.consume(MINOR)
        assert w.tm.get_domain_state("d").score < before

    def test_the_second_minor_offense_of_the_same_rule_opens_a_minor_window(self, w) -> None:
        w.set_trust(0.0)
        w.consume(MINOR, rule="late")
        _, window = w.consume(MINOR, rule="late")
        assert window is not None and window.base_duration_hours == 72                 # 3 days at Trust 0

    def test_a_minor_offense_of_a_different_rule_is_a_new_first_offense(self, w) -> None:
        w.consume(MINOR, rule="a")
        _, window = w.consume(MINOR, rule="b")
        assert window is None and len(w.warnings()) == 2

    def test_consuming_the_same_incident_again_changes_nothing(self, w) -> None:
        inc, _ = w.consume(MINOR)
        a = w.tm.get_incident_assessment(inc.id)
        assert w.pe.consume_confirmed_incident(inc.id, "d", "r", a.intrinsic_severity, a.cooperation, now=NOW) is None
        assert w.warnings() == [inc.id] and w.windows() == []


class TestHeadroom:
    def test_extensions_use_the_reserved_half_but_never_pass_the_cap(self, w) -> None:
        w.set_trust(0.0)
        _, window = w.consume(MAJOR)                           # 168 h
        for i in range(12):
            w.consume(MAJOR, rule=f"x{i}", at=NOW + timedelta(minutes=i + 1))
        row = w.windows()[0]
        assert len(w.windows()) == 1
        assert row["base_duration_hours"] + row["extensions_hours"] <= 336
        assert row["extensions_hours"] > 0

    def test_advanced_headroom_is_the_other_half_of_720(self, w) -> None:
        w.advanced()
        w.set_trust(0.0)
        w.consume(MAJOR)                                       # 360 h
        for i in range(30):
            w.consume(MAJOR, rule=f"x{i}", at=NOW + timedelta(minutes=i + 1))
        row = w.windows()[0]
        assert 336 < row["base_duration_hours"] + row["extensions_hours"] <= 720


class TestLegacyBehaviourWithoutAPolicy:
    def test_an_engine_built_without_a_policy_keeps_the_fixed_base_and_the_336_cap(self, w) -> None:
        from penalty_engine.repository import PenaltyEngine
        w.pe = PenaltyEngine(w.core.db_path, core=w.core)
        _, window = w.consume(MINOR)
        assert window is not None and window.base_duration_hours == 24 and window.max_target_hours == 336


def test_severity_mapping() -> None:
    from penalty_engine.duration import ViolationSeverity as V
    from penalty_engine.policy import severity_of
    assert [severity_of(t) for t in SeverityTier] == [V.MINOR, V.MODERATE, V.SEVERE, V.SEVERE]
