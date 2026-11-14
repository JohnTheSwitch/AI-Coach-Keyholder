"""Roadmap slice 5: the weekly review, Sunday 19:00 local, the week in plain facts written by code."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from application.service import ApplicationService
from system.daily_evaluation import get_daily_evaluations
from tests.application.test_lock_and_task_commands import FIXED_TIME, _complete_onboarding, _incoming, core  # noqa: F401
from tests.coach.test_desk import U, World
from weekly_review.service import Ports, WeekFacts, WeeklyReview, render_week

MONDAY = date(2026, 11, 9)
SUNDAY_1900 = datetime(2026, 11, 15, 19, 0, tzinfo=timezone.utc)      # tests use UTC as local time


def _commitment(w: World, i: int, for_date: date, status: str, timing: str | None) -> None:
    with w.core.transaction() as tx:
        tx.execute("INSERT INTO coach_proposals (id, user_id, local_date, kind, payload_json, fingerprint, status, created_at, expires_at) "
                   "VALUES (?, ?, ?, 'propose_commitment', '{}', ?, 'accepted', ?, ?)",
                   (f"p{i}", U, for_date.isoformat(), f"f{i}", "2026-11-09T08:00:00Z", "2026-11-09T09:00:00Z"))
        tx.execute("INSERT INTO daily_commitments (id, user_id, for_date, text, proposal_id, status, created_at, report_timing) "
                   "VALUES (?, ?, ?, 'Read 20 pages', ?, ?, ?, ?)", (f"c{i}", U, for_date.isoformat(), f"p{i}", status, "2026-11-09T08:00:00Z", timing))


def _evaluation(w: World, day: date, *, due=2, ok=2, tokens=0, paused=0) -> None:
    with w.core.transaction() as tx:
        tx.execute("INSERT INTO daily_evaluations (user_id, local_date, goals_assigned, goals_met, check_ins_due, check_ins_ok, violations, trust_used, "
                   "progress, pool_after, tokens_earned, clean, streak_after, bonus, created_at, paused) "
                   "VALUES (?, ?, 0, 0, ?, ?, 0, '0.6', '0.2', '0.1', ?, 0, 0, '0', ?, ?)",
                   (U, day.isoformat(), due, ok, tokens, "2026-11-15T00:00:00Z", paused))


def _review(w: World, *, paused=False, checks=None) -> WeeklyReview:
    return WeeklyReview(w.core, user_id=U, ports=Ports(
        local=lambda now: now, paused=lambda _now: paused, commitments=w.desk.get_commitments_between,
        evaluations=lambda s, e: get_daily_evaluations(w.core, U, s, e), checks=checks))


def _notifications(w: World) -> list[dict]:
    return w.rows("SELECT kind, text, dedupe_key FROM notifications WHERE kind = 'weekly_review'")


def test_the_week_in_plain_facts() -> None:
    text = render_week(WeekFacts(start=MONDAY, end=MONDAY + timedelta(days=6), kept=4, partly=1, not_kept=1, unanswered=1, outside_time=1,
                                 checks=2, checks_passed=1, checks_waiting=1, evaluated=6, vacation_days=1, check_ins_ok=9, check_ins_due=10, tokens=1))
    assert text == (
        "Your week, Mon 9 Nov to Sun 15 Nov.\n"
        "Commitments: 7 (4 kept, 1 partly, 1 not kept, 1 never answered). 1 reported outside its time, so it didn't count.\n"
        "Spot checks: 2 (1 passed, 1 still open).\n"
        "Check-ins (Mon 9 Nov to Sat 14 Nov): 9 of 10 answered.\n"
        "Tokens earned (Mon 9 Nov to Sat 14 Nov): 1. 1 day on vacation.\n"
        "Today is counted after midnight. `weekly review off` stops this message.")


def test_an_empty_week_says_so_and_no_spot_check_line_without_them() -> None:
    text = render_week(WeekFacts(start=MONDAY, end=MONDAY + timedelta(days=6)))
    assert "Commitments: none this week." in text and "Spot checks" not in text and "no finished days to count yet" in text
    assert "Trust" not in text


def test_facts_from_the_owners_tables(tmp_path) -> None:
    w = World(tmp_path)
    _commitment(w, 1, MONDAY, "kept", "timely")
    _commitment(w, 2, MONDAY, "kept", "late")
    _commitment(w, 3, MONDAY + timedelta(days=2), "partly", "too_soon")
    _commitment(w, 4, MONDAY + timedelta(days=3), "not_kept", None)
    _commitment(w, 5, MONDAY + timedelta(days=4), "kept", "unproven")
    _commitment(w, 6, MONDAY + timedelta(days=6), "open", None)
    _commitment(w, 7, MONDAY - timedelta(days=1), "kept", "timely")               # last week
    for i in range(6):
        _evaluation(w, MONDAY + timedelta(days=i), tokens=1 if i == 2 else 0, paused=1 if i == 5 else 0)
    _evaluation(w, MONDAY - timedelta(days=1), tokens=5)                           # last week
    checks = [{"status": "passed"}, {"status": "failed"}, {"status": "declined"}, {"status": "submitted"}, {"status": "cancelled"}]
    f = _review(w, checks=lambda s, e: checks).facts(MONDAY)
    assert (f.kept, f.partly, f.not_kept, f.open, f.outside_time, f.unproven) == (3, 1, 1, 1, 2, 1)
    assert (f.checks, f.checks_passed, f.checks_failed, f.checks_declined, f.checks_waiting) == (4, 1, 1, 1, 1)
    assert (f.evaluated, f.vacation_days, f.check_ins_ok, f.check_ins_due, f.tokens) == (6, 1, 10, 10, 1)


class TestSunday:
    def test_sent_once_on_sunday_from_1900(self, tmp_path) -> None:
        w = World(tmp_path)
        review = _review(w)
        assert review.run(SUNDAY_1900 - timedelta(minutes=1)) is None
        assert review.run(SUNDAY_1900 - timedelta(days=1)) is None                # Saturday
        assert review.run(SUNDAY_1900) == "sent"
        assert review.run(SUNDAY_1900 + timedelta(hours=1)) is None
        [note] = _notifications(w)
        assert note["text"].startswith("Your week, Mon 9 Nov to Sun 15 Nov.") and note["dedupe_key"] == "weekly_review:2026-11-09"
        assert w.rows("SELECT outcome FROM weekly_reviews") == [{"outcome": "sent"}]
        assert w.rows("SELECT event_type FROM domain_events WHERE event_type LIKE 'weekly_review.%'") == [{"event_type": "weekly_review.week_decided"}]

    def test_a_missed_sunday_is_not_sent_on_monday(self, tmp_path) -> None:
        w = World(tmp_path)
        assert _review(w).run(SUNDAY_1900 + timedelta(hours=6)) is None           # Monday 01:00
        assert _notifications(w) == []

    def test_vacation_skips_the_week_for_good(self, tmp_path) -> None:
        w = World(tmp_path)
        assert _review(w, paused=True).run(SUNDAY_1900) == "vacation"
        assert _review(w, paused=False).run(SUNDAY_1900 + timedelta(hours=2)) is None
        assert _notifications(w) == []

    def test_off_sends_nothing_and_on_again_resumes(self, tmp_path) -> None:
        w = World(tmp_path)
        review = _review(w)
        assert review.is_on(U)
        assert review.set(U, True, now=SUNDAY_1900) is False                      # already on: no row, no event
        assert review.set(U, False, now=SUNDAY_1900) is True and review.set(U, False, now=SUNDAY_1900) is False
        assert review.run(SUNDAY_1900) is None and _notifications(w) == []
        assert review.set(U, True, now=SUNDAY_1900) is True
        assert review.run(SUNDAY_1900 + timedelta(minutes=5)) == "sent"
        events = [r["event_type"] for r in w.rows("SELECT event_type FROM domain_events WHERE event_type LIKE 'weekly_review.%' ORDER BY rowid")]
        assert events == ["weekly_review.turned_off", "weekly_review.turned_on", "weekly_review.week_decided"]


class TestCommands:
    def say(self, service, text):
        return service.handle_message(_incoming(text, now=FIXED_TIME)).text

    def test_unavailable_without_the_coach(self, core) -> None:  # noqa: F811
        service = ApplicationService(core.db_path, core=core)
        _complete_onboarding(service)
        assert self.say(service, "weekly review off") == "The weekly review is not available on this bot."

    def test_on_off_status(self, core) -> None:  # noqa: F811
        service = ApplicationService(core.db_path, core=core)
        _complete_onboarding(service)
        with core.transaction() as tx:
            user_id = tx.fetch_one("SELECT id FROM user_accounts")["id"]
        service.weekly_review = WeeklyReview(core, user_id=user_id, ports=Ports(local=lambda n: n, paused=lambda n: False,
                                                                                  commitments=lambda s, e: [], evaluations=lambda s, e: []))
        assert "is on" in self.say(service, "weekly review")
        assert self.say(service, "Weekly review off") == "The weekly review is off. `weekly review on` starts it again."
        assert "is off" in self.say(service, "weekly review")
        assert self.say(service, "weekly review on").startswith("The weekly review is on")
