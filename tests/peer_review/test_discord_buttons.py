"""Phase I4 one-click decisions: the persistent Discord buttons on the owner's incident notice, the adapter attaching them, the loop
passing them, and the decision handler behind them. No network, no real Discord connection."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from messaging.discord_adapter import DiscordMessagingAdapter
from peer_review.discord_buttons import (CUSTOM_ID_TEMPLATE, HANDLER_ATTRIBUTE, PeerReviewButtonHandler, PeerReviewDecisionButton,
                                         custom_id, decision_view)
from peer_review.watch import DecisionOutcome
from peer_review.wiring import peer_review_loop
from tests.peer_review.test_peer_review import NOW, OWNER, World, task  # noqa: F401

OWNER_DISCORD = "1031954281479274497"


def _incident(w) -> str:
    w.set(tasks=[task("pv1", "ongoing")])
    w.watch.poll(NOW)
    w.set(tasks=[task("pv1", "rejected")])
    w.watch.poll(NOW + timedelta(minutes=1))
    return w.incidents()[0]["id"]


def _handler(w, owner_discord=OWNER_DISCORD, owner=OWNER):
    return PeerReviewButtonHandler(w.decisions, owner_user_id=owner, owner_discord_id=owner_discord, now=lambda: NOW)


class TestHandler:
    def test_the_owner_confirms_with_one_click(self, tmp_path) -> None:
        w = World(tmp_path)
        incident_id = _incident(w)
        assert _handler(w).decide(discord_user_id=OWNER_DISCORD, incident_id=incident_id, confirm=True) is DecisionOutcome.CONFIRMED
        assert w.incidents()[0]["confirmation"] == "confirmed"
        assert _handler(w).decide(discord_user_id=OWNER_DISCORD, incident_id=incident_id, confirm=False) is DecisionOutcome.ALREADY_DECIDED

    def test_the_owner_dismisses(self, tmp_path) -> None:
        w = World(tmp_path)
        incident_id = _incident(w)
        assert _handler(w).decide(discord_user_id=OWNER_DISCORD, incident_id=incident_id, confirm=False) is DecisionOutcome.DISMISSED
        assert w.incidents()[0]["confirmation"] == "unconfirmed"

    @pytest.mark.parametrize("handler_kw, clicker", [({}, "999"), ({"owner_discord": None}, OWNER_DISCORD), ({"owner": None}, OWNER_DISCORD)])
    def test_nobody_else_can_decide(self, tmp_path, handler_kw, clicker) -> None:
        w = World(tmp_path)
        incident_id = _incident(w)
        assert _handler(w, **handler_kw).decide(discord_user_id=clicker, incident_id=incident_id, confirm=True) is DecisionOutcome.NOT_OWNER
        assert w.incidents()[0]["confirmation"] == "unconfirmed"

    def test_an_unknown_or_malformed_incident(self, tmp_path) -> None:
        w = World(tmp_path)
        _incident(w)
        assert _handler(w).decide(discord_user_id=OWNER_DISCORD, incident_id="x", confirm=True) is DecisionOutcome.NOT_FOUND
        assert _handler(w).decide(discord_user_id=OWNER_DISCORD, incident_id="0" * 36, confirm=True) is DecisionOutcome.NOT_FOUND

    def test_every_outcome_has_a_reply(self) -> None:
        assert all(PeerReviewButtonHandler.reply(o) for o in DecisionOutcome)


class TestButtons:
    def test_the_view_has_both_persistent_buttons(self) -> None:
        incident_id = "12345678-1234-1234-1234-123456789abc"

        async def build():
            return decision_view(incident_id)

        view = asyncio.run(build())
        assert view.timeout is None and view.is_persistent()
        labels = [(i.item.label, i.item.style, i.item.custom_id) for i in view.children]
        assert labels == [("Confirm Penalty", discord.ButtonStyle.danger, custom_id("confirm", incident_id)),
                          ("Dismiss Incident", discord.ButtonStyle.secondary, custom_id("dismiss", incident_id))]
        assert all(re.fullmatch(CUSTOM_ID_TEMPLATE, i.item.custom_id) for i in view.children)
        assert all(len(i.item.custom_id) <= 100 for i in view.children)                     # Discord's custom id limit

    def test_the_template_restores_a_button_after_a_restart(self) -> None:
        cid = custom_id("dismiss", "12345678-1234-1234-1234-123456789abc")

        async def restore():
            return await PeerReviewDecisionButton.from_custom_id(MagicMock(), MagicMock(), re.fullmatch(CUSTOM_ID_TEMPLATE, cid))

        button = asyncio.run(restore())
        assert (button.action, button.incident_id) == ("dismiss", "12345678-1234-1234-1234-123456789abc")
        assert re.fullmatch(CUSTOM_ID_TEMPLATE, "peer_review:delete:12345678-1234-1234-1234-123456789abc") is None

    def _click(self, tmp_path, *, clicker=OWNER_DISCORD, action="confirm", handler=True):
        w = World(tmp_path)
        incident_id = _incident(w)
        interaction = MagicMock()
        interaction.user.id = int(clicker)
        interaction.client = SimpleNamespace(**{HANDLER_ATTRIBUTE: _handler(w) if handler else None})
        interaction.message.content = "Community review rejected a task photo"
        interaction.response.edit_message = AsyncMock()
        interaction.response.send_message = AsyncMock()

        async def click():
            await PeerReviewDecisionButton(action, incident_id).callback(interaction)

        asyncio.run(click())
        return w, interaction

    def test_a_click_decides_and_removes_the_buttons(self, tmp_path) -> None:
        w, interaction = self._click(tmp_path)
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert kwargs["view"] is None and "Confirmed by you" in kwargs["content"]
        assert kwargs["content"].startswith("Community review rejected a task photo")
        assert w.incidents()[0]["confirmation"] == "confirmed"

    def test_a_dismiss_click(self, tmp_path) -> None:
        w, interaction = self._click(tmp_path, action="dismiss")
        assert "Dismissed by you" in interaction.response.edit_message.await_args.kwargs["content"]
        assert w.decisions.rows()[0].owner_decision == "dismissed"

    def test_a_stranger_gets_an_ephemeral_refusal_and_nothing_changes(self, tmp_path) -> None:
        w, interaction = self._click(tmp_path, clicker="999")
        interaction.response.edit_message.assert_not_awaited()
        args, kwargs = interaction.response.send_message.await_args
        assert args == ("Only the keyholder can decide this.",) and kwargs == {"ephemeral": True}
        assert w.incidents()[0]["confirmation"] == "unconfirmed"

    def test_without_a_handler_it_points_to_the_cli(self, tmp_path) -> None:
        w, interaction = self._click(tmp_path, handler=False)
        assert "use the CLI" in interaction.response.send_message.await_args.args[0]
        assert w.incidents()[0]["confirmation"] == "unconfirmed"


class TestDelivery:
    def test_the_adapter_attaches_the_view_to_the_last_chunk(self) -> None:
        user = MagicMock()
        user.send = AsyncMock()
        client = MagicMock()
        client.is_ready.return_value = True
        client.fetch_user = AsyncMock(return_value=user)
        adapter = DiscordMessagingAdapter()
        adapter.attach(client)
        view = object()
        assert asyncio.run(adapter.send_message("123", "a" * 1990 + "\n" + "b" * 100, view=view)) is True
        calls = user.send.await_args_list
        assert len(calls) == 2 and "view" not in calls[0].kwargs and calls[1].kwargs == {"view": view}

    def test_without_a_view_nothing_changes(self) -> None:
        user = MagicMock()
        user.send = AsyncMock()
        client = MagicMock()
        client.is_ready.return_value = True
        client.fetch_user = AsyncMock(return_value=user)
        adapter = DiscordMessagingAdapter()
        adapter.attach(client)
        assert asyncio.run(adapter.send_message("123", "hello")) is True
        assert user.send.await_args.args == ("hello",) and user.send.await_args.kwargs == {}

    def test_the_loop_sends_the_notice_with_the_buttons(self, tmp_path) -> None:
        w = World(tmp_path)
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "rejected")])
        sent = []

        async def send(target, text, *, view=None):
            sent.append((target, text, view))
            return True

        async def run():
            clock = SimpleNamespace(now=lambda: NOW + timedelta(minutes=5))
            loop_task = asyncio.create_task(peer_review_loop(w.watch, clock, interval_seconds=3600, send=send, owner_target=OWNER_DISCORD,
                                                             initial_delay_seconds=0, view_for=lambda incident_id: ("view", incident_id)))
            for _ in range(1000):
                await asyncio.sleep(0.01)
                if sent and not w.watch.pending_notifications():
                    break
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

        asyncio.run(run())
        (target, text, view), = sent
        incident_id = w.incidents()[0]["id"]
        assert target == OWNER_DISCORD and view == ("view", incident_id) and "Decide with the buttons below" in text


def test_the_bot_registers_the_persistent_buttons_and_the_handler() -> None:
    from pathlib import Path
    text = (Path(__file__).parent.parent.parent / "bot" / "discord_bot.py").read_text(encoding="utf-8")
    assert "self.add_dynamic_items(PeerReviewDecisionButton)" in text and "bot.peer_review_buttons = PeerReviewButtonHandler(" in text
    assert "view_for=decision_view" in text
