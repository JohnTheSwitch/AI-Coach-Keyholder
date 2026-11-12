"""Owner, 2026-11-12, option A: what the wearer owns up to by themselves becomes a finding marked as self-disclosed. Only their own "Yes" decides it.
No network; the example sentences are made up, not the owner's words."""

from __future__ import annotations

from datetime import timedelta

import pytest

from exemptions.service import is_hygiene_request
from infractions.self_report import is_hygiene_confession
from infractions.signals import APPEAL_WINDOW, GRACE, SELF_REPORT_QUESTION, get_recent_questions
from tests.infractions.test_detectors import NOW, USER, World


class TestReadingAConfession:
    @pytest.mark.parametrize("text", [
        "I edged in the shower during my hygiene break", "during the hygiene opening I touched myself", "I came while I was unlocked for hygiene",
        "Ve sprše jsem se honil", "when the lock was opened I played with myself"])
    def test_these_are_offered_a_card(self, text) -> None:
        assert is_hygiene_confession(text)

    @pytest.mark.parametrize("text", ["Can I get a hygiene opening?", "I edged last week but I was locked", "The shower was cold today", "", None])
    def test_these_are_not(self, text) -> None:
        assert not is_hygiene_confession(text)


class TestOnlyARequestOpensTheHygieneCard:
    @pytest.mark.parametrize("text", ["Are you not worried about me misusing the hygiene opening?", "the hygiene opening yesterday was too short for me",
                                      "I edged in the shower during my hygiene opening"])
    def test_talk_about_an_opening_is_not_a_request(self, text) -> None:
        assert not is_hygiene_request(text)

    @pytest.mark.parametrize("text", ["hygiene opening", "hygiene opening please", "Can I get a hygiene opening?", "I need a hygiene break"])
    def test_a_request_still_is(self, text) -> None:
        assert is_hygiene_request(text)


class TestTheWearersOwnYes:
    def _offer(self, w):
        return w.signals.offer_self_report(user_id=USER, statement="made-up confession", trust_domain="hygiene", now=NOW)

    def test_yes_counts_after_the_appeal_time_and_as_self_disclosed(self, tmp_path) -> None:
        w = World(tmp_path)
        signal_id = self._offer(w)
        assert w.status() == [("self_report_hygiene", "offered")] and w.incidents() == []
        reply = w.signals.press(user_id=USER, signal_id=signal_id, action="own", now=NOW)
        assert "weighs less" in reply.text and [a.label for a in reply.actions] == ["Appeal"]
        w.advance(NOW + APPEAL_WINDOW - timedelta(minutes=1))
        assert w.status() == [("self_report_hygiene", "decided")]
        w.advance(NOW + APPEAL_WINDOW + timedelta(minutes=1))
        assert w.status() == [("self_report_hygiene", "confirmed")] and w.confirmed and w.confirmed[-1][1] is True

    def test_an_approved_opening_never_excuses_it(self, tmp_path) -> None:
        w = World(tmp_path)
        signal_id = self._offer(w)
        w.signals.press(user_id=USER, signal_id=signal_id, action="own", now=NOW)
        w.signals._excuses.reason = lambda *a, **k: "approved_exemption"
        w.advance(NOW + APPEAL_WINDOW + timedelta(minutes=1))
        assert w.status() == [("self_report_hygiene", "confirmed")]

    def test_no_records_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        signal_id = self._offer(w)
        assert "Nothing is recorded" in w.signals.press(user_id=USER, signal_id=signal_id, action="disown", now=NOW).text
        w.advance(NOW + GRACE * 2)
        assert w.status() == [("self_report_hygiene", "cleared")] and w.incidents() == [] and w.confirmed == []

    def test_an_unanswered_card_lapses_without_a_trace(self, tmp_path) -> None:
        w = World(tmp_path)
        self._offer(w)
        w.advance(NOW + GRACE + timedelta(minutes=1))
        assert w.status() == [("self_report_hygiene", "cleared")] and w.confirmed == []

    def test_one_open_card_at_a_time(self, tmp_path) -> None:
        w = World(tmp_path)
        assert self._offer(w) == self._offer(w)

    def test_the_model_knows_a_card_is_waiting(self, tmp_path) -> None:
        w = World(tmp_path)
        self._offer(w)
        assert get_recent_questions(w.core, USER, NOW, local=lambda d: d)[0]["stage"] == "awaiting_confirmation"


class TestTheCardGoesUnderTheReply:
    def test_the_reply_keeps_its_text_and_gains_the_question(self) -> None:
        from application.models import ActionSpec, OutgoingMessage
        from application.service import ApplicationService
        service = ApplicationService.__new__(ApplicationService)
        service.self_reports = lambda user_id, text, now: (SELF_REPORT_QUESTION, (ActionSpec(action_id="infraction:own:x", label="Yes, I did"),))
        out = service._with_self_report(OutgoingMessage(text="Bold of you."), "u", "text", NOW)
        assert out.text.startswith("Bold of you.") and "Owning up?" in out.text and out.actions[-1].label == "Yes, I did"

    def test_the_voice_never_dodges_the_topic(self) -> None:
        from conversation_engine.prompt_builder import VOICE_GUIDE
        assert "never dodge or refuse the topic" in VOICE_GUIDE and "only explicit description stays out" in VOICE_GUIDE
