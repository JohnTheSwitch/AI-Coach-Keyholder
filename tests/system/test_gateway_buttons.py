"""Owner, 2026-11-14: with multi-wearer on, every DM (the owner's too) goes through the gateway; its buttons and follow-up cards must survive."""

from __future__ import annotations

from application.models import ActionSpec, OutgoingMessage
from tests.system.test_tenant_gateway import OWNER_DISCORD, TestBotWiring, g, run  # noqa: F401

KEPT = ActionSpec(action_id="coach:kept:11111111-1111-4111-8111-111111111111", label="Kept")
ACCEPT = ActionSpec(action_id="coach:accept:22222222-2222-4222-8222-222222222222", label="Accept")


def _owner_reply(g):  # noqa: F811
    g._zero = lambda incoming: OutgoingMessage(text="owner reply", actions=(KEPT,), followups=(OutgoingMessage(text="proposal", actions=(ACCEPT,)),))
    g.gateway._zero_handler = g._zero


def test_the_gateway_keeps_the_buttons_and_follow_ups(g) -> None:  # noqa: F811
    _owner_reply(g)
    reply = g.say(OWNER_DISCORD, "hi")
    assert reply.actions == (KEPT,) and [f.text for f in reply.followups] == ["proposal"]


def test_the_bot_sends_them_through_the_gateway_path(g) -> None:  # noqa: F811
    _owner_reply(g)
    bot, message = TestBotWiring()._bot(g)
    m = message(OWNER_DISCORD, "hi")
    run(bot._on_message_via_gateway(m))
    calls = m.channel.send.call_args_list
    assert [c.args[0] for c in calls] == ["owner reply", "proposal"] and all("view" in c.kwargs for c in calls)
