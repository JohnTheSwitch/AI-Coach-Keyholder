"""G2 (owner, 2026-11-15): health uses are counted; from the 3rd in 7 days the keyholder asks once a week; nothing is restricted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from infrastructure.time_format import iso
from system.health_pattern import HealthPattern, describe, health_uses
from tests.lines.test_lines import core

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
U = "wearer"


def _uses(c, opening=0, exemption=0, task=0, question=0, at=NOW - timedelta(days=1)):
    with c.transaction() as tx:
        for i in range(opening):
            tx.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at, decided_at, kind, minutes) VALUES (?, ?, 'ended', ?, ?, 'hygiene_health', 60)",
                       (f"o{i}", U, iso(at), iso(at)))
        for i in range(exemption):
            tx.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at, decided_at, kind, category, hours, decided_by) "
                       "VALUES (?, ?, 'ended', ?, ?, 'exemption', 'health', 24, 'judge_health')", (f"e{i}", U, iso(at), iso(at)))
        for i in range(task):
            tx.execute("INSERT INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, outcome, decided_at) "
                       "VALUES (?, ?, ?, 'x', 't', ?, 'cancelled', ?, 'health_or_safety', ?)", (f"t{i}", U, f"2026-11-0{i + 1}", iso(at), iso(at), iso(at)))
        for i in range(question):
            tx.execute("INSERT INTO infraction_signals (id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, "
                       "created_at, decided_at, cleared_reason) VALUES (?, ?, 'lock_claim', ?, 'routine', 'd', 'cleared', ?, ?, ?, ?, 'health')",
                       (f"q{i}", U, f"s{i}", iso(at), f"inc{i}", iso(at), iso(at)))


def _pattern(c):
    return HealthPattern(c, user_id=U, iso_week=lambda now: "2026-W46")


def test_all_four_kinds_are_counted(tmp_path) -> None:
    c = core(tmp_path)
    _uses(c, opening=1, exemption=1, task=1, question=1)
    uses = health_uses(c, U, NOW - timedelta(days=7), NOW)
    assert sorted(u["what"] for u in uses) == ["exemption", "opening", "question", "task"]
    assert describe(uses).count("1 ") == 4


def test_two_uses_ask_nothing_three_ask_once_a_week(tmp_path) -> None:
    c = core(tmp_path)
    _uses(c, opening=2)
    assert _pattern(c).tick(NOW) is False
    _uses(c, task=1)
    assert _pattern(c).tick(NOW) is True and _pattern(c).tick(NOW + timedelta(hours=1)) is False
    with c.transaction() as tx:
        note = tx.fetch_one("SELECT text FROM notifications WHERE kind = 'health_pattern'")["text"]
    assert "3 health uses" in note and "never counts against you" in note


def test_old_uses_and_vacation_do_not_count(tmp_path) -> None:
    c = core(tmp_path)
    _uses(c, opening=3, at=NOW - timedelta(days=8))
    assert _pattern(c).tick(NOW) is False
    _uses(c, exemption=3)
    assert HealthPattern(c, user_id=U, iso_week=lambda n: "w", paused=lambda n: True).tick(NOW) is False
