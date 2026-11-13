"""Roadmap slice 4: `python -m cli economy`, read-only token pace."""

from __future__ import annotations

import io
from datetime import date, datetime, timezone

from cli.main import run_economy
from tests.coach.test_desk import World


def test_the_report_from_daily_evaluations(tmp_path) -> None:
    w = World(tmp_path)
    with w.core.transaction() as tx:
        for i, (progress, ok, tokens) in enumerate((("0.2", 1, 0), ("0", 1, 0), ("0.25", 2, 1))):
            tx.execute("INSERT INTO daily_evaluations (user_id, local_date, goals_assigned, goals_met, check_ins_due, check_ins_ok, violations, "
                       "trust_used, progress, pool_after, tokens_earned, clean, streak_after, bonus, created_at, paused) "
                       "VALUES (?, ?, 0, 0, 2, ?, 0, '0.6', ?, '0.1', ?, 0, 0, '0', ?, 0)",
                       ("u", f"2026-11-1{i}", ok, progress, tokens, "2026-11-14T00:00:00Z"))
    out = io.StringIO()
    assert run_economy(out=out, now=lambda: datetime(2026, 11, 14, tzinfo=timezone.utc), factory=lambda: (w.core, "u", date(2026, 11, 14))) == 0
    text = out.getvalue()
    assert "Last 7 days: 3 evaluated (0 on vacation), 1 token(s) earned, average progress 0.150 a day -> 6.7 days per token." in text
    assert "1 day(s) zeroed by a confirmed incident; check-ins 4/6 answered" in text


def test_it_writes_nothing(tmp_path) -> None:
    w = World(tmp_path)
    before = w.core.db_path.read_bytes()
    run_economy(out=io.StringIO(), now=lambda: datetime(2026, 11, 14, tzinfo=timezone.utc), factory=lambda: (w.core, "u", date(2026, 11, 14)))
    assert w.core.db_path.read_bytes() == before
