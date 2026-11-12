"""Asking the bot for a hygiene opening (owner's option A): buttons, a weekly allowance, and a health need that nothing limits. Fake Chaster, no network."""

from __future__ import annotations

from datetime import timedelta

import pytest

from chaster_execution.temporary_opening import OpeningOutcome, OpeningResult, OpeningStatus
from exemptions.service import ALLOWANCE_WINDOW, HEALTH_MINUTES, is_hygiene_request
from tests.exemptions.test_exemption_opening import FakeOpening, World as OpeningWorld
from tests.exemptions.test_exemptions import OWNER, OWNER_DISCORD, WEARER, WEARER_DISCORD
from tests.system.test_penalty_pipeline import NOW

BREAKS, MINUTES = 3, 30


class World(OpeningWorld):
    def __init__(self, tmp_path, opening=None, **kwargs) -> None:
        super().__init__(tmp_path, opening, **kwargs)
        self.service._allowance = lambda: (BREAKS, MINUTES)

    def ask(self, at=NOW):
        self.at = at
        return self.service.start_hygiene(WEARER, at)

    def press(self, card, label, at=NOW):
        self.at = at
        action = next(a for a in card.actions if a.label == label)
        _, name, ident = action.action_id.split(":")
        return self.dm.press(discord_user_id=WEARER_DISCORD, action=name, ident=ident)

    def row(self):
        return self.q("SELECT * FROM wear_exemptions ORDER BY created_at DESC, rowid DESC")[0]

    def take_break(self, at):
        reply = self.press(self.ask(at), "Open it", at)
        self.opening.state = OpeningStatus(is_open=False, relocked_at=at + timedelta(minutes=10))
        self.press(reply, "I'm back", at + timedelta(minutes=11))
        self.opening.state = OpeningStatus(is_open=True, relocked_at=None)


class TestTheWords:
    @pytest.mark.parametrize("text", ["can i get a hygiene opening for the shower?", "hygiene opening", "I need a hygiene break", "Hygienické otevření prosím", "otevři zámek kvůli hygieně"])
    def test_these_ask(self, text) -> None:
        assert is_hygiene_request(text)

    @pytest.mark.parametrize("text", ["I will go get a shower", "my hygiene is fine", "the opening of the shop", "", None])
    def test_these_do_not(self, text) -> None:
        assert not is_hygiene_request(text)


class TestAQuestionOpensNothing:
    def test_asking_only_offers_buttons(self, tmp_path) -> None:
        w = World(tmp_path)
        card = w.ask()
        assert [a.label for a in card.actions] == ["Open it", "It's a health need", "Not now"] and "30-minute" in card.text
        assert w.opening.opens == []

    def test_not_now_leaves_nothing_behind(self, tmp_path) -> None:
        w = World(tmp_path)
        assert "nothing was opened" in w.press(w.ask(), "Not now").text
        assert w.q("SELECT * FROM wear_exemptions") == [] and w.opening.opens == []

    def test_a_card_answers_only_its_own_user(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.ask().actions[0].action_id.split(":")[2]
        assert "no longer open" in w.dm.press(discord_user_id=OWNER_DISCORD, action="hopen", ident=ident).text
        assert w.opening.opens == []

    def test_without_a_configured_lock_the_model_answers_as_before(self, tmp_path) -> None:
        w = World(tmp_path)
        w.service._opening = None
        assert w.ask() is None


class TestWithinTheAllowance:
    def test_the_press_opens_at_once_with_no_keyholder(self, tmp_path) -> None:
        w = World(tmp_path)
        reply = w.press(w.ask(), "Open it")
        row = w.row()
        assert w.opening.opens == [MINUTES / 60] and row["kind"] == "hygiene" and row["decided_by"] == "allowance"
        assert "30 minutes" in reply.text and [a.label for a in reply.actions] == ["I'm back"]
        assert w.notices("exemption_request") == []

    def test_it_expires_after_the_session(self, tmp_path) -> None:
        w = World(tmp_path)
        w.press(w.ask(), "Open it")
        assert w.row()["expires_at"].startswith((NOW + timedelta(minutes=MINUTES)).isoformat()[:16])

    def test_im_back_is_checked_the_same_way(self, tmp_path) -> None:
        w = World(tmp_path)
        reply = w.press(w.ask(), "Open it")
        assert "still shows your lock as open" in w.press(reply, "I'm back", NOW + timedelta(minutes=5)).text
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(minutes=20))
        assert "relocked" in w.press(reply, "I'm back", NOW + timedelta(minutes=21)).text
        assert w.row()["end_reason"] == "relocked_on_time" and w.opening.restored

    def test_late_is_reported(self, tmp_path) -> None:
        w = World(tmp_path)
        reply = w.press(w.ask(), "Open it")
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(minutes=MINUTES + 5))
        w.press(reply, "I'm back", NOW + timedelta(hours=1))
        assert w.row()["end_reason"] == "relocked_late"

    def test_a_break_does_not_pause_a_penalty_window(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        at = NOW + timedelta(hours=1)
        w.press(w.ask(at), "Open it", at)
        w.run(at + timedelta(minutes=1))
        assert w.window().status.value == "active" and w.opening.opens


class TestPastTheAllowance:
    def _used(self, w):
        for day in range(BREAKS):
            w.take_break(NOW + timedelta(days=day))
        return NOW + timedelta(days=BREAKS)

    def test_the_card_changes(self, tmp_path) -> None:
        w = World(tmp_path)
        at = self._used(w)
        assert [a.label for a in w.ask(at).actions] == ["Ask my keyholder", "It's a health need", "Not now"]

    def test_it_goes_to_the_keyholder_and_opens_only_on_approval(self, tmp_path) -> None:
        w = World(tmp_path)
        at = self._used(w)
        calls = len(w.opening.opens)
        w.press(w.ask(at), "Ask my keyholder", at)
        card = w.notices("exemption_request")[0]
        assert card["user_id"] == OWNER and len(w.opening.opens) == calls
        ident = w.row()["id"]
        w.at = at
        w.dm.press(discord_user_id=OWNER_DISCORD, action="approve", ident=ident)
        assert len(w.opening.opens) == calls + 1

    def test_a_decline_opens_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        at = self._used(w)
        calls = len(w.opening.opens)
        w.press(w.ask(at), "Ask my keyholder", at)
        w.dm.press(discord_user_id=OWNER_DISCORD, action="decline", ident=w.row()["id"])
        assert len(w.opening.opens) == calls and "wasn't approved" in w.notices("exemption_decision")[-1]["text"]

    def test_an_old_card_cannot_slip_past_the_limit(self, tmp_path) -> None:
        """The limit is read again at the press, not only when the card was shown."""
        w = World(tmp_path)
        for day in range(BREAKS - 1):
            w.take_break(NOW + timedelta(days=day))
        at = NOW + timedelta(days=BREAKS)
        card = w.ask(at)
        w.service._allowance = lambda: (BREAKS - 1, MINUTES)
        calls = len(w.opening.opens)
        assert "Sent to your keyholder" in w.press(card, "Open it", at).text and len(w.opening.opens) == calls

    def test_the_week_rolls(self, tmp_path) -> None:
        w = World(tmp_path)
        at = self._used(w) + ALLOWANCE_WINDOW
        assert w.ask(at).actions[0].label == "Open it"


class TestAHealthNeedIsOutsideAllOfIt:
    def test_it_opens_even_past_the_allowance(self, tmp_path) -> None:
        w = World(tmp_path)
        w.service._allowance = lambda: (0, MINUTES)
        w.press(w.ask(), "It's a health need")
        assert w.opening.opens == [HEALTH_MINUTES / 60] and w.row()["kind"] == "hygiene_health" and w.notices("exemption_request") == []

    def test_it_never_counts(self, tmp_path) -> None:
        w = World(tmp_path)
        for day in range(6):
            at = NOW + timedelta(days=day)
            reply = w.press(w.ask(at), "It's a health need", at)
            w.opening.state = OpeningStatus(is_open=False, relocked_at=at + timedelta(minutes=5))
            w.press(reply, "I'm back", at + timedelta(minutes=6))
            w.opening.state = OpeningStatus(is_open=True, relocked_at=None)
        assert w.ask(NOW + timedelta(days=7)).actions[0].label == "Open it"

    def test_lateness_is_never_judged(self, tmp_path) -> None:
        w = World(tmp_path)
        reply = w.press(w.ask(), "It's a health need")
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(hours=9))
        w.press(reply, "I'm back", NOW + timedelta(hours=9))
        assert w.row()["end_reason"] == "relocked_on_time" and w.q("SELECT * FROM notifications WHERE text LIKE '%approved time%'") == []

    def test_it_is_not_chased(self, tmp_path) -> None:
        w = World(tmp_path)
        w.press(w.ask(), "It's a health need")
        w.service.tick(NOW + timedelta(hours=3))
        assert w.notices("exemption_overdue") == []


class TestWhenItCannotOpen:
    def test_a_halt_is_said_plainly(self, tmp_path) -> None:
        w = World(tmp_path, FakeOpening(OpeningResult(outcome=OpeningOutcome.HALTED)))
        assert "couldn't open it" in w.press(w.ask(), "Open it").text
        assert "not allowed to act" in w.notices("exemption_opening")[0]["text"]

    def test_a_failed_opening_does_not_use_up_the_allowance(self, tmp_path) -> None:
        w = World(tmp_path, FakeOpening(OpeningResult(outcome=OpeningOutcome.UNAVAILABLE, detail="http_400")))
        w.press(w.ask(), "Open it")
        assert w.service._planned_this_week(WEARER, NOW) == 0

    def test_one_at_a_time(self, tmp_path) -> None:
        w = World(tmp_path)
        w.press(w.ask(), "Open it")
        assert "already have an exemption running" in w.ask(NOW + timedelta(minutes=1)).text


class TestTheDecisionLog:
    def test_every_step_of_a_hygiene_opening_is_logged(self, tmp_path, caplog) -> None:
        import logging
        from infrastructure.decision_log import DECISIONS_LOGGER
        w = World(tmp_path)
        with caplog.at_level(logging.INFO, logger=DECISIONS_LOGGER):
            w.take_break(NOW)
        text = "\n".join(r.getMessage() for r in caplog.records)
        for step in ("offered within this week's allowance", "approved by the allowance", "Chaster opening: opened", "Lock closed again on Chaster",
                     "Exemption ended"):
            assert step in text, step
