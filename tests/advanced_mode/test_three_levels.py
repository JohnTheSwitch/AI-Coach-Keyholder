"""Owner, 2026-11-16: three levels, beginner / standard / advanced; beginner is close to the owner's own start."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from advanced_mode.models import OperatingMode, is_lowering
from advanced_mode.repository import AdvancedMode, AdvancedModeAdministration
from tests.release_engine.test_progressive_conditioning import U, W

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def _beginner(core) -> None:
    with core.transaction() as tx:
        tx.execute("UPDATE operating_mode_state SET current_mode = 'beginner' WHERE id = 1")


def test_the_order_of_the_levels() -> None:
    assert is_lowering(OperatingMode.STANDARD, OperatingMode.BEGINNER) and is_lowering(OperatingMode.ADVANCED, OperatingMode.STANDARD)
    assert not is_lowering(OperatingMode.BEGINNER, OperatingMode.STANDARD) and not is_lowering(OperatingMode.STANDARD, OperatingMode.ADVANCED)


def test_beginner_tokens_are_the_owners_start(tmp_path) -> None:
    w = W(tmp_path)
    _beginner(w.core)
    with w.core.transaction() as tx:
        assert w.cond.threshold_in(tx, U) == D("1.0")
    for i in range(21):
        w.release(day_offset=i)
        w.drain()
    assert w.cond.factor(U) == D("1.500") and w.outcomes().count("increased") == 20      # +0.025 twenty times, then capped


def test_a_new_wearer_starts_at_beginner_once(tmp_path) -> None:
    w = W(tmp_path)
    admin = AdvancedModeAdministration(w.core.db_path, core=w.core)
    assert admin.start_new_wearer_at_beginner(now=NOW) is True
    assert AdvancedMode(w.core.db_path, core=w.core).get_current_mode().current_mode is OperatingMode.BEGINNER
    assert admin.start_new_wearer_at_beginner(now=NOW) is False


def test_beginner_lines_are_half(tmp_path) -> None:
    from types import SimpleNamespace
    from lines.service import LineService
    from tests.lines.test_lines import core
    db = core(tmp_path)
    _beginner(db)
    lines = LineService(db, user_id=U)
    with db.transaction() as tx:
        lines.consumers()["penalty_warning.recorded"](tx, SimpleNamespace(payload={"incident_id": "i1", "rule_group_id": "HYGIENE_MISUSE"},
                                                                           occurred_at=NOW))
    assert lines.open_lines(U) == 10
    assert lines.assign_release("", NOW) is True
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT required FROM line_assignments WHERE rule_group_id = 'RELEASE'")["required"] == 10


def test_graduation_is_asked_once_after_four_weeks_without_a_severe_slip(tmp_path) -> None:
    from system.graduation import GRADUATE_AFTER, graduation_tick
    w = W(tmp_path)
    AdvancedModeAdministration(w.core.db_path, core=w.core).start_new_wearer_at_beginner(now=NOW)
    assert graduation_tick(w.core, U, NOW + timedelta(days=27)) is False
    assert graduation_tick(w.core, U, NOW + GRADUATE_AFTER) is True
    assert graduation_tick(w.core, U, NOW + GRADUATE_AFTER + timedelta(days=1)) is False
