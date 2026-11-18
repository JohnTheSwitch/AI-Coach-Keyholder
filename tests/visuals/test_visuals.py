"""Owner, 2026-11-17 ("I want all of these"): coloured cards, task cards, the weekly card, progress bars, the bot's avatar and banner."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from application.models import ActionSpec, EmbedSpec
from messaging.discord_adapter import RichCard, send_dm
from messaging.models import ChannelType
from notifications.delivery import NotificationDelivery
from notifications.queue import DeliveryResult, Notification
from visuals.cards import avatar, banner, render
from visuals.style import KIND_STYLE, TASK_KINDS, bar, heading_for, style_for
from weekly_review.service import WeekFacts, week_card

PNG = b"\x89PNG"


class TestStyle:
    def test_a_known_kind_gets_its_colour_and_heading(self) -> None:
        spec = style_for("check_in_warning", "Warning: three check-ins were missed.")
        assert spec.color == "red" and spec.title == heading_for("check_in_warning") and spec.description.startswith("Warning")

    def test_an_unknown_kind_or_a_too_long_text_stays_plain(self) -> None:
        assert style_for("something_new", "hi") is None and style_for("check_in", "x" * 5000) is None and style_for("check_in", " ") is None

    def test_a_decided_outcome_shows_as_what_it_is(self) -> None:
        assert style_for("exemption_decision", "Declined. That isn't a reason.").color == "red"
        assert style_for("obedience_task", "Your task was verified. Good.").color == "green"

    def test_only_an_offer_of_a_task_gets_a_picture(self) -> None:
        assert style_for("obedience_task", "Your task was verified. Good.").card is None
        assert style_for("check_in", "Answer now.", offer=True).card is None
        card = style_for("obedience_task", "Your task for today is in Chaster now:\nKneel 20 minutes. Photo.\nBy 23:00.", offer=True).card
        assert card["headline"] == "Kneel 20 minutes." and card["type"] == "task"

    def test_a_task_card_shows_its_single_reward_and_the_board_its_count(self) -> None:
        one = style_for("obedience_task", "**Kneel for 20 minutes** Reward: +0.03 toward your next token.", offer=True).card
        assert one["headline"] == "Kneel for 20 minutes" and one["note"] == "+0.03 toward your next token"
        board = style_for("task_board", "This week's task board.\n**1.** Sink. Reward: +0.02 toward your next token.\n"
                                        "**2.** Desk. Reward: +0.03 toward your next token.", offer=True).card
        assert board["headline"] == "This week's task board" and board["note"] == "2 tasks, each with a reward"

    def test_every_task_kind_has_a_style(self) -> None:
        assert TASK_KINDS <= set(KIND_STYLE)

    def test_bar(self) -> None:
        assert bar(3, 10) == "▰" * 3 + "▱" * 7 and bar(20, 10) == "▰" * 10 and bar(1, 0) == "▱" * 10


class TestPictures:
    def test_cards_are_drawn(self) -> None:
        task = style_for("temptation", "Temptation: 20 minutes edging.", offer=True).card
        week = week_card(WeekFacts(start=date(2026, 11, 9), end=date(2026, 11, 15), check_ins_ok=5, check_ins_due=6,
                                   days=((date(2026, 11, 9), "clean"), (date(2026, 11, 10), "missed"))))
        assert render(task).startswith(PNG) and render(week).startswith(PNG)

    def test_a_bad_spec_costs_the_picture_never_the_message(self) -> None:
        assert render(None) is None and render({"type": "nope"}) is None and render({"type": "week", "days": "bad"}) is None

    def test_the_profile_pictures(self) -> None:
        assert avatar().startswith(PNG) and banner().startswith(PNG)


class TestTheWeekCard:
    def test_it_shows_only_what_the_text_says(self) -> None:
        f = WeekFacts(start=date(2026, 11, 9), end=date(2026, 11, 15), kept=2, not_kept=1, check_ins_ok=5, check_ins_due=6, tokens=1,
                      days=((date(2026, 11, 9), "clean"),))
        card = week_card(f)
        labels = [t["label"] for t in card["tiles"]]
        assert labels == ["Check-ins", "Commitments", "Tokens"] and card["days"] == [{"label": "Mon", "state": "clean"}]
        assert "trust" not in repr(card).lower() and "pool" not in repr(card).lower()


class Discord:
    def __init__(self) -> None:
        self.calls = []

    async def send_outcome(self, target, text, *, view=None, embed=None):
        self.calls.append((target, text, view, embed))
        from messaging.discord_adapter import DmOutcome
        return DmOutcome.DELIVERED


class Router:
    def __init__(self, channel) -> None:
        self.channel, self.calls = channel, []

    def channel_for(self, _user_id):
        return self.channel

    async def send_to_user(self, user_id, text):
        self.calls.append((user_id, text))
        return True


def _note(kind="check_in_warning", text="Warning: three check-ins were missed.", actions=(), embed=None):
    return Notification(id="1", user_id="u", kind=kind, text=text, embed=embed, actions=actions, immediate=False, attempts=0)


def _delivery(discord, router):
    return NotificationDelivery(discord=discord, discord_id_for=lambda _u: "42", router=router, style_for=style_for, heading_for=heading_for,
                                rich_for=lambda spec, heading: (spec, heading), view_for=lambda a: ("view", len(a)))


class TestDelivery:
    def test_a_plain_notice_read_on_discord_becomes_a_card_with_its_heading(self) -> None:
        discord, router = Discord(), Router(ChannelType.DISCORD)
        assert asyncio.run(_delivery(discord, router)(_note())) is DeliveryResult.DELIVERED
        (_, text, _, (spec, heading)), = discord.calls
        assert router.calls == [] and spec.color == "red" and heading == heading_for("check_in_warning") and text.startswith("Warning")

    def test_a_card_never_moves_a_notice_off_chaster_chat(self) -> None:
        discord, router = Discord(), Router(ChannelType.CHASTER)
        asyncio.run(_delivery(discord, router)(_note()))
        assert discord.calls == [] and router.calls == [("u", "Warning: three check-ins were missed.")]

    def test_an_unknown_kind_keeps_its_usual_path(self) -> None:
        discord, router = Discord(), Router(ChannelType.DISCORD)
        asyncio.run(_delivery(discord, router)(_note(kind="t", text="hi")))
        assert discord.calls == [] and router.calls == [("u", "hi")]

    def test_an_offer_gets_a_task_card_and_keeps_its_buttons(self) -> None:
        discord = Discord()
        asyncio.run(_delivery(discord, Router(ChannelType.CHASTER))(_note(kind="obedience_task", text="**Kneel 20 minutes** now.",
                                                                          actions=(ActionSpec(action_id="a", label="b"),))))
        (_, _, view, (spec, _)), = discord.calls
        assert view == ("view", 1) and spec.card["headline"] == "Kneel 20 minutes"

    def test_an_embed_of_its_own_is_kept(self) -> None:
        discord = Discord()
        own = EmbedSpec(title="Mine", color="green")
        asyncio.run(_delivery(discord, Router(ChannelType.DISCORD))(_note(kind="weekly_review", text="Your week.", embed=own)))
        assert discord.calls[0][3][0] is own


class TestTheDm:
    def test_a_rich_card_sends_its_heading_and_picture(self) -> None:
        sent = {}

        async def send(**kwargs):
            sent.update(kwargs)

        async def fetch_user(_id):
            return SimpleNamespace(send=send)

        client = SimpleNamespace(fetch_user=fetch_user, get_user=lambda _id: None, is_ready=lambda: True)
        asyncio.run(send_dm(client, 1, "text", embed=RichCard(embed="E", files=("F",), content="\U0001F514 Check-in")))
        assert sent == {"embed": "E", "files": ["F"], "content": "\U0001F514 Check-in"}
