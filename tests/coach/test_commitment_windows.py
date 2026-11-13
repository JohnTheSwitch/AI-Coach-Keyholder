"""Honesty and proof, slice 1 (owner, 2026-11-13): a commitment's part of the day, "Done?" when it ends, and the report window."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

import pytest

from bot.action_buttons import COACH_TEMPLATE
from coach.desk import REPORT_GRACE, Outcome
from system.coach_dm import ask_card, proposal_card, review_card
from tests.coach.test_desk import U, World

MADE = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)          # proposed the day before, in the afternoon
DAY = MADE.date() + timedelta(days=1)                              # the commitment's own day


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=timezone.utc)


@pytest.fixture
def w(tmp_path) -> World:
    world = World(tmp_path)
    world.desk.local_now = lambda now: now
    world.desk.to_utc = lambda d, t: datetime.combine(d, t, tzinfo=timezone.utc)
    world.desk.quiet = (time(22, 0), time(7, 0))
    return world


def accept(w: World, part: str, *, now: datetime = MADE, text: str = "Study for 30 minutes") -> tuple[str, str]:
    result = w.desk.submit(U, {"action": "propose_commitment", "text": text, "when": "tomorrow"}, MADE)
    assert result.proposal is not None, result.reason
    reply = w.desk.decide(U, result.proposal.id, accept=True, now=now, part=part)
    rows = w.rows("SELECT id FROM daily_commitments WHERE proposal_id = ?", result.proposal.id)
    return reply, rows[0]["id"] if rows else result.proposal.id


def events(w: World, kind: str) -> int:
    return len(w.rows("SELECT 1 FROM domain_events WHERE event_type = ?", kind))


class TestParts:
    def test_the_parts_of_a_day_end_before_quiet_hours(self, w) -> None:
        assert w.desk.window_for("morning", DAY) == (at(7), at(12))
        assert w.desk.window_for("afternoon", DAY) == (at(12), at(18))
        assert w.desk.window_for("evening", DAY) == (at(18), at(21, 30))
        assert w.desk.window_for("any", DAY) == (at(7), at(21, 30))

    def test_only_parts_that_have_not_ended_are_offered(self, w) -> None:
        assert w.desk.open_parts(DAY, at(13)) == ("afternoon", "evening", "any")
        assert w.desk.open_parts(DAY, at(22)) == ()

    def test_without_windows_nothing_changes(self, tmp_path) -> None:
        plain = World(tmp_path)
        assert plain.desk.window_for("evening", DAY) is None and plain.desk.open_parts(DAY, at(13)) == ("morning", "afternoon", "evening", "any")


class TestAccepting:
    def test_accepting_a_part_stores_its_window_and_when_the_bot_asks(self, w) -> None:
        reply, cid = accept(w, "evening")
        assert "21:30" in reply
        row = w.rows("SELECT window_part, window_start, window_end, ask_at FROM daily_commitments WHERE id = ?", cid)[0]
        assert row["window_part"] == "evening" and row["window_start"].startswith(f"{DAY}T18:00") and row["ask_at"].startswith(f"{DAY}T21:30")

    def test_a_part_that_is_already_over_is_refused_and_the_proposal_stays(self, w) -> None:
        reply, pid = accept(w, "morning", now=at(13))
        assert reply == "That part of the day is already over. Pick a later one."
        assert w.rows("SELECT status FROM coach_proposals WHERE id = ?", pid)[0]["status"] == "pending"

    def test_any_time_after_the_waking_day_can_still_be_reported_but_is_never_asked(self, w) -> None:
        w.desk.quiet = (time(16, 0), time(7, 0))                          # the waking day of DAY ends at 15:30, inside the proposal's 24 hours
        _reply, cid = accept(w, "any", now=at(15, 45))
        row = w.rows("SELECT window_end, ask_at FROM daily_commitments WHERE id = ?", cid)[0]
        assert row["ask_at"] is None and row["window_end"].startswith(f"{DAY}T15:45")


class TestAsking:
    def test_done_is_asked_once_when_the_time_ends(self, w) -> None:
        _reply, cid = accept(w, "evening")
        queued = []
        assert w.desk.due_asks(at(21, 29), lambda tx, c, part: queued.append((c.id, part))) == 0
        assert w.desk.due_asks(at(21, 30), lambda tx, c, part: queued.append((c.id, part))) == 1
        assert w.desk.due_asks(at(21, 40), lambda tx, c, part: queued.append((c.id, part))) == 0
        assert queued == [(cid, "evening")] and events(w, "coach.commitment_asked") == 1

    def test_nothing_is_asked_during_a_vacation_or_after_the_report_window(self, w) -> None:
        accept(w, "evening")
        w.paused = True
        assert w.desk.due_asks(at(21, 30), lambda *a: pytest.fail("asked during a vacation")) == 0
        w.paused = False
        _r, late = accept(w, "afternoon", text="Read for 20 minutes")
        assert w.desk.due_asks(at(18) + REPORT_GRACE + timedelta(minutes=1), lambda *a: pytest.fail("asked too late")) == 0

    def test_still_doing_it_asks_once_more_but_never_in_quiet_hours(self, w) -> None:
        _reply, cid = accept(w, "evening")
        w.desk.due_asks(at(21, 30), lambda *a: None)
        assert w.desk.extend(U, cid, at(21, 35)) == "OK. I'll ask again at 22:00. A Kept counts until 23:30."
        assert w.desk.extend(U, cid, at(21, 40)) == "OK. Tell me when you're done: a Kept counts until 23:30."
        assert w.desk.due_asks(at(22), lambda *a: None) == 1


class TestReportWindow:
    def test_a_kept_inside_the_window_counts(self, w) -> None:
        _reply, cid = accept(w, "evening")
        assert w.desk.record_outcome(U, cid, Outcome.KEPT, at(21, 40)) == "Noted, well done."
        assert events(w, "coach.commitment_kept") == 1
        assert w.rows("SELECT report_timing FROM daily_commitments WHERE id = ?", cid)[0]["report_timing"] == "timely"

    def test_a_late_kept_is_recorded_but_earns_nothing_and_says_why(self, w) -> None:
        _reply, cid = accept(w, "evening")
        reply = w.desk.record_outcome(U, cid, Outcome.KEPT, at(23, 31))
        assert "too late to count" in reply and events(w, "coach.commitment_kept") == 0
        assert w.rows("SELECT status, report_timing FROM daily_commitments WHERE id = ?", cid)[0] == {"status": "kept", "report_timing": "late"}

    def test_a_kept_before_the_window_could_be_done_earns_nothing(self, w) -> None:
        _reply, cid = accept(w, "evening", text="Study for an hour")
        w.desk.record_outcome(U, cid, Outcome.KEPT, at(18, 30))
        assert events(w, "coach.commitment_kept") == 0
        assert w.rows("SELECT report_timing FROM daily_commitments WHERE id = ?", cid)[0]["report_timing"] == "too_soon"

    def test_owning_up_is_never_too_late(self, w) -> None:
        _reply, cid = accept(w, "evening")
        w.desk.record_outcome(U, cid, Outcome.NOT_KEPT, at(23, 59))
        assert events(w, "coach.commitment_missed") == 1

    def test_an_old_commitment_without_a_window_uses_the_waking_day(self, w) -> None:
        w.desk.to_utc = None
        _reply, cid = accept(w, "any")
        w.desk.to_utc = lambda d, t: datetime.combine(d, t, tzinfo=timezone.utc)
        assert w.rows("SELECT window_end FROM daily_commitments WHERE id = ?", cid)[0]["window_end"] is None
        assert w.desk.past_report_window(cid, at(23, 29)) is False and w.desk.past_report_window(cid, at(23, 31)) is True


class TestCards:
    def test_every_card_button_is_a_registered_coach_button(self, w) -> None:
        result = w.desk.submit(U, {"action": "propose_commitment", "text": "Study for 30 minutes", "when": "tomorrow"}, MADE)
        text, actions = proposal_card(result.proposal, ("afternoon", "evening", "any"))
        assert "When will you do it?" in text
        assert [a.label for a in actions] == ["Afternoon", "Evening", "Any time", "Decline"]
        commitment = type("C", (), {"id": result.proposal.id, "text": "Study", "for_date": DAY})()
        for _text, buttons in (proposal_card(result.proposal, ("any",)), ask_card(commitment, "evening"), review_card(commitment, late=True)):
            assert all(re.fullmatch(COACH_TEMPLATE, b.action_id) for b in buttons)
        assert ask_card(commitment, "evening")[0] == "**Study**: that was this evening's plan. Done?"
        assert "no longer counts" in review_card(commitment, late=True)[0]
