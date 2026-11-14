"""Owner, 2026-11-14: a second card is its own Discord message, with its own buttons right under it. No network."""

from __future__ import annotations

from unittest.mock import Mock

from application.models import ActionSpec, OutgoingMessage
from tests.bot.test_discord_bot import FIXED_TIME, _complete_onboarding, _fake_dm_message, _run, bot, config, core  # noqa: F401


def test_each_card_is_sent_as_its_own_message(bot) -> None:  # noqa: F811
    _complete_onboarding(bot)
    reply = OutgoingMessage(text="Nice day.\n\n**Your commitment**", actions=(ActionSpec(action_id="coach:kept:11111111-1111-4111-8111-111111111111", label="Kept"),),
                            followups=(OutgoingMessage(text="**Proposed commitment**", actions=(ActionSpec(action_id="coach:accept:22222222-2222-4222-8222-222222222222", label="Accept"),)),))
    bot.application_service.handle_message = Mock(return_value=reply)
    message = _fake_dm_message("We were busy today", message_id=700)
    _run(bot._handle_dm(message, received_at=FIXED_TIME))
    calls = message.channel.send.call_args_list
    assert [c.args[0] for c in calls] == ["Nice day.\n\n**Your commitment**", "**Proposed commitment**"]
    assert all("view" in c.kwargs for c in calls)
