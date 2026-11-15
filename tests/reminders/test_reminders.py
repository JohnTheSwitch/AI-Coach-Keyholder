"""Standing reminders at most once a week (owner, 2026-11-16, queue A0b)."""

from __future__ import annotations

from datetime import timedelta

from reminders.service import WEEK, take
from tests.lines.test_lines import core
from tests.obedience.test_obedience import MORNING


def test_shown_once_then_again_after_a_week(tmp_path) -> None:
    db = core(tmp_path)
    assert take(db, "u", "skin_check", MORNING) is True
    assert take(db, "u", "skin_check", MORNING + timedelta(days=3)) is False
    assert take(db, "u", "other", MORNING + timedelta(days=3)) is True          # each reminder on its own
    assert take(db, "u", "skin_check", MORNING + WEEK) is True
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT COUNT(*) AS n FROM domain_events WHERE event_type = 'reminders.shown'")["n"] == 3
