"""
TT29-TT33 (trust_manager_technical_design.md 15.3): the Goal Accountability
Assessment -> TrustEvidence integration, through the real domain services.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from infrastructure.database import Database as CoreDatabase
from trust_manager.models import EvidenceType
from trust_manager.repository import TrustManager

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    migrations = Path(__file__).parent.parent.parent / "database" / "migrations"
    with c.raw_connection() as conn:
        for path in sorted(migrations.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


@pytest.fixture
def tm(core: CoreDatabase) -> TrustManager:
    t = TrustManager(core.db_path, core=core)
    t.create_domain(domain_id="fitness", display_name="Fitness", description="d", created_via_consent_id="c1", now=NOW)
    return t


def _record(tm: TrustManager, *, aid="a1", relevant=True, direction="progress", domain="fitness"):
    return tm.record_goal_assessment_evidence(
        assessment_id=aid, trust_domain=domain, relevant_to_trust=relevant,
        direction=direction, rationale="because", now=NOW,
    )


def _evidence_count(core: CoreDatabase) -> int:
    with core.transaction() as tx:
        return tx.fetch_one("SELECT COUNT(*) AS n FROM trust_evidence")["n"]


class TestGoalAssessmentEvidence:
    def test_tt29_not_relevant_writes_nothing(self, tm, core) -> None:
        assert _record(tm, relevant=False) is None
        assert _evidence_count(core) == 0

    def test_tt30_neutral_writes_nothing(self, tm, core) -> None:
        assert _record(tm, direction="neutral") is None
        assert _evidence_count(core) == 0

    def test_tt31_progress_writes_goal_progress(self, tm) -> None:
        ev = _record(tm, direction="progress")
        assert ev.evidence_type == EvidenceType.GOAL_PROGRESS
        assert ev.raw_weight == 0.05 and ev.evidence_confidence == 1.0

    def test_tt32_setback_writes_goal_setback(self, tm) -> None:
        ev = _record(tm, direction="setback")
        assert ev.evidence_type == EvidenceType.GOAL_SETBACK
        assert ev.raw_weight == -0.08

    def test_tt33_redelivery_produces_one_row_and_one_recalculation(self, tm, core) -> None:
        assert _record(tm) is not None
        assert _record(tm) is None
        assert _evidence_count(core) == 1
        with core.transaction() as tx:
            n = tx.fetch_one("SELECT COUNT(*) AS n FROM trust_recalculations WHERE triggered_by = 'goal_assessment'")["n"]
        assert n == 1

    def test_progress_raises_and_setback_lowers_the_score(self, tm) -> None:
        before = tm.get_domain_state("fitness").score
        _record(tm, aid="a1", direction="progress")
        up = tm.get_domain_state("fitness").score
        _record(tm, aid="a2", direction="setback")
        down = tm.get_domain_state("fitness").score
        assert up == pytest.approx(before + 0.05)
        assert down == pytest.approx(up - 0.08)

    def test_evidence_is_consumed_by_the_recalculation(self, tm, core) -> None:
        _record(tm)
        with core.transaction() as tx:
            n = tx.fetch_one("SELECT COUNT(*) AS n FROM trust_recalculation_evidence")["n"]
        assert n == 1

    def test_unknown_domain_writes_nothing(self, tm, core) -> None:
        assert _record(tm, domain="nope") is None
        assert _evidence_count(core) == 0

    def test_inactive_domain_writes_nothing(self, tm, core) -> None:
        tm.deactivate_domain("fitness", via_consent_id="c2", now=NOW)
        assert _record(tm) is None
        assert _evidence_count(core) == 0

    def test_unknown_direction_is_rejected(self, tm) -> None:
        with pytest.raises(ValueError):
            _record(tm, direction="sideways")
