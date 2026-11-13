"""Supervision state (hysteresis), Trust levels, and the real event wiring."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from supervision.models import (
    TrustLevel,
    check_ins_per_day,
    hygiene_interval_days,
    trust_level,
)
from supervision.repository import SupervisionService
from system.composition import build_rules_and_execution
from system.startup import on_system_startup
from trust_manager.repository import TrustManager

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    TrustManager(c.db_path, core=c).create_domain(
        domain_id="d", display_name="D", description="d", created_via_consent_id="c1", now=NOW)
    return c


class TestLevels:
    @pytest.mark.parametrize("score,level", [
        (0.0, 1), (0.40, 1), (0.4499, 1), (0.45, 2), (0.6999, 2), (0.70, 3), (0.8499, 3), (0.85, 4), (1.0, 4),
    ])
    def test_boundaries(self, score, level) -> None:
        assert trust_level(score) == TrustLevel(level)

    @pytest.mark.parametrize("score", [-0.01, 1.01])
    def test_out_of_range(self, score) -> None:
        with pytest.raises(ValueError):
            trust_level(score)

    def test_check_ins_per_day(self) -> None:
        assert [check_ins_per_day(TrustLevel(i)) for i in (4, 3, 2, 1)] == [0, 1, 2, 3]

    def test_hygiene_calendar_day_intervals(self) -> None:
        assert [hygiene_interval_days(TrustLevel(i)) for i in (4, 3, 2, 1)] == [1, 2, 3, 4]


class TestRealWiring:
    def _drop_trust(self, core):
        tm = TrustManager(core.db_path, core=core)
        for i in range(5):          # five setbacks: short-term Trust 0.00 from enough pieces (owner, 2026-11-13)
            tm.record_goal_assessment_evidence(assessment_id=f"a{i}", trust_domain="d", relevant_to_trust=True,
                                               direction="setback", rationale="r", now=NOW + timedelta(minutes=i))
        assert tm.get_domain_state("d").score < 0.40

    def test_supervision_starts_from_real_trust_recalculations(self, core) -> None:
        self._drop_trust(core)
        svc = SupervisionService(core.db_path, core=core)
        on_system_startup(core, "p", FrozenClock(NOW + timedelta(hours=1)), None, svc)
        assert svc.is_active("d") is True

    def test_without_the_service_nothing_is_recorded(self, core) -> None:
        self._drop_trust(core)
        on_system_startup(core, "p", FrozenClock(NOW + timedelta(hours=1)))
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM supervision_states")["n"] == 0

    def test_redelivery_does_not_double_count(self, core) -> None:
        self._drop_trust(core)
        svc = SupervisionService(core.db_path, core=core)
        on_system_startup(core, "p1", FrozenClock(NOW + timedelta(hours=1)), None, svc)
        with core.transaction() as tx:
            tx.execute("UPDATE domain_events SET published_at = NULL WHERE event_type = 'trust_domain.recalculated'")
        on_system_startup(core, "p2", FrozenClock(NOW + timedelta(hours=2)), None, svc)      # consumer dedup holds
        with core.transaction() as tx:
            row = tx.fetch_one("SELECT recovery_streak FROM supervision_states WHERE domain_id = 'd'")
        assert row["recovery_streak"] == 0

    def test_flag_controls_composition(self, core) -> None:
        base = dict(db_path=core.db_path, enable_rules_engine=False, enable_chaster_execution=False)
        assert build_rules_and_execution(SimpleNamespace(**base, enable_supervision=False), core).supervision is None
        assert build_rules_and_execution(SimpleNamespace(**base, enable_supervision=True), core).supervision is not None

    def test_default_config_is_on(self) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["enable_supervision"].default is True
        assert "ENABLE_SUPERVISION=true" in (ROOT / ".env.example").read_text(encoding="utf-8")


class TestIsolation:
    def test_supervision_cannot_reach_a_model_chaster_or_discord(self) -> None:
        forbidden = ("discord", "bot", "conversation_engine", "application", "chaster", "chaster_execution", "ai")
        for path in (ROOT / "supervision").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                for n in names:
                    assert n.split(".")[0] not in forbidden, (path.name, n)
