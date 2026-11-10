"""Owner, 2026-11-10: what could not run while the PC was off runs when it starts. No network, no live Discord."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import discord

from bot.discord_bot import REPLAY_MAX_AGE
from infrastructure.time_format import iso
from tests.bot.test_discord_bot import FIXED_TIME, _complete_onboarding, _fake_dm_message, _run, bot, config, core  # noqa: F401
from tests.infractions.test_detectors import GRACE, NOW, World
from tests.supervision.test_checkins import MON, U, Env, local


class History:
    def __init__(self, messages) -> None:
        self.messages, self.after = messages, None

    def __call__(self, *, after, oldest_first, limit):
        self.after = after.id
        messages = [m for m in self.messages if m.id > after.id]

        async def gen():
            for m in messages:
                yield m

        return gen()


def _offline(bot, messages):  # noqa: F811
    channel = Mock()
    channel.history = History(messages)
    user = Mock(dm_channel=channel)
    bot.fetch_user = AsyncMock(return_value=user)
    return channel


def _sent_at(message, at):
    message.created_at = at
    return message


class TestMessagesSentWhileOffline:
    def test_they_are_handled_on_start_and_marked_late(self, bot) -> None:  # noqa: F811
        _complete_onboarding(bot)
        missed = _sent_at(_fake_dm_message("help", message_id=500), FIXED_TIME - timedelta(hours=1))
        channel = _offline(bot, [missed])
        bot._caught_up = asyncio.Event()
        _run(bot._replay_missed_dms())
        assert channel.history.after == 103 and bot._caught_up.is_set()
        reply = missed.channel.send.call_args
        text = reply.args[0] if reply.args else reply.kwargs.get("content", "")
        assert "while I was offline" in text

    def test_old_ones_and_already_handled_ones_are_left(self, bot) -> None:  # noqa: F811
        _complete_onboarding(bot)
        stale = _sent_at(_fake_dm_message("help", message_id=600), FIXED_TIME - REPLAY_MAX_AGE - timedelta(hours=1))
        handled = _sent_at(_fake_dm_message("help", message_id=50), FIXED_TIME)
        _offline(bot, [handled, stale])
        _run(bot._replay_missed_dms())
        assert not stale.channel.send.called and not handled.channel.send.called

    def test_a_failure_never_blocks_the_rest(self, bot) -> None:  # noqa: F811
        _complete_onboarding(bot)
        bot.fetch_user = AsyncMock(side_effect=discord.HTTPException(Mock(status=500), "down"))
        bot._caught_up = asyncio.Event()
        _run(bot._replay_missed_dms())
        assert bot._caught_up.is_set()

    def test_one_unknown_account_does_not_stop_the_others(self, bot, core) -> None:  # noqa: F811
        _complete_onboarding(bot)
        with core.raw_connection() as conn:
            conn.execute("INSERT INTO user_channel_identities (id, user_account_id, channel, external_id, created_at) "
                         "SELECT 'x', user_account_id, 'discord', '1', created_at FROM user_channel_identities LIMIT 1")
            conn.commit()
        missed = _sent_at(_fake_dm_message("help", message_id=500), FIXED_TIME - timedelta(hours=1))
        channel = Mock()
        channel.history = History([missed])

        async def fetch(discord_id):
            if discord_id == 1:
                raise discord.NotFound(Mock(status=404), "Unknown User")
            return Mock(dm_channel=channel)

        bot.fetch_user = fetch
        _run(bot._replay_missed_dms())
        assert missed.channel.send.called

    def test_the_timed_loops_wait_for_it(self, bot) -> None:  # noqa: F811
        order = []

        async def scenario():
            bot._caught_up = asyncio.Event()

            async def loop():
                order.append("loop")

            waiting = asyncio.create_task(bot._after_catch_up(loop()))
            await asyncio.sleep(0)
            order.append("replayed")
            bot._caught_up.set()
            await waiting

        _run(scenario())
        assert order == ["replayed", "loop"]

    def test_a_check_in_answer_sent_while_offline_still_counts(self, tmp_path) -> None:
        e = Env(tmp_path)
        e.svc.tick(local(MON, 9))
        due = e.first_due()
        e.svc.tick(due)
        assert e.svc.on_user_message(U, "Did my workout and the dishes.", due + timedelta(minutes=10))      # the time it was SENT
        e.svc.tick(due + timedelta(hours=5))                                                               # handled much later
        assert e.rows()[0]["status"] == "answered"


class TestACheckInMissedWhileOff:
    def _env(self, tmp_path, *, later_slot: bool):
        e = Env(tmp_path)
        with e.core.raw_connection() as conn:
            conn.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                         "VALUES ('a', ?, ?, 0, 2, ?, 'scheduled', ?, ?)", (U, MON.isoformat(), iso(local(MON, 17, 0)), iso(local(MON, 9)), iso(local(MON, 9))))
            conn.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                         "VALUES ('b', ?, ?, 1, 2, ?, 'scheduled', ?, ?)", (U, MON.isoformat(), iso(local(MON, 21 if later_slot else 18)), iso(local(MON, 9)), iso(local(MON, 9))))
            conn.commit()
        e.svc.tick(local(MON, 19, 30))                     # the PC came on at 19:30
        return e

    def test_the_last_one_of_the_day_is_sent_late(self, tmp_path) -> None:
        e = self._env(tmp_path, later_slot=False)
        assert len(e.sender.sent) == 1 and {r["id"]: r["status"] for r in e.rows()} == {"a": "skipped", "b": "sent"}

    def test_not_when_another_one_is_still_coming_today(self, tmp_path) -> None:
        e = self._env(tmp_path, later_slot=True)
        assert e.sender.sent == [] and {r["id"]: r["status"] for r in e.rows()}["a"] == "skipped"


class TestQuestionDeadlinesMoveByTheDowntime:
    def test_an_open_question_gets_the_time_back(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        before = w.q("SELECT review_after FROM infraction_signals")[0]["review_after"]
        assert w.signals.extend_for_downtime(down_since=NOW + timedelta(hours=1), back_at=NOW + timedelta(hours=9)) == 1
        after = w.q("SELECT review_after FROM infraction_signals")[0]["review_after"]
        assert datetime.fromisoformat(after.replace("Z", "+00:00")) - datetime.fromisoformat(before.replace("Z", "+00:00")) == timedelta(hours=8)
        w.advance(NOW + GRACE + timedelta(hours=1))
        assert w.status() == [("hygiene_duration", "waiting")]

    def test_a_short_restart_changes_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        assert w.signals.extend_for_downtime(down_since=NOW, back_at=NOW + timedelta(minutes=3)) == 0

    def test_a_deadline_that_passed_before_it_went_down_stays_passed(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        assert w.signals.extend_for_downtime(down_since=NOW + GRACE * 2, back_at=NOW + GRACE * 3) == 0


def test_the_bot_waits_at_most_a_few_minutes() -> None:
    from bot.discord_bot import REPLAY_WAIT_SECONDS
    assert REPLAY_WAIT_SECONDS <= 300 and REPLAY_MAX_AGE == timedelta(days=3)


_ = timezone


class TestAfterTheComputerSlept:
    def test_a_reconnect_catches_up_again(self, bot) -> None:  # noqa: F811
        _complete_onboarding(bot)
        missed = _sent_at(_fake_dm_message("help", message_id=500), FIXED_TIME - timedelta(hours=1))
        _offline(bot, [missed])
        started = []
        bot._replay_missed_dms = lambda: started.append(1) or asyncio.sleep(0)
        _run(bot.on_ready())
        _run(bot.on_ready())
        assert len(started) == 2

    def test_a_message_is_never_answered_twice(self, bot) -> None:  # noqa: F811
        _complete_onboarding(bot)
        missed = _sent_at(_fake_dm_message("help", message_id=500), FIXED_TIME - timedelta(hours=1))
        _offline(bot, [missed])
        _run(bot._replay_missed_dms())
        _run(bot._handle_dm(missed, received_at=FIXED_TIME))
        _run(bot._replay_missed_dms())
        assert missed.channel.send.call_count == 1

    def test_question_deadlines_move_by_the_time_asleep(self) -> None:
        from infractions.wiring import detector_loop

        class Clock:
            times = iter([NOW, NOW + timedelta(hours=8)])

            def now(self):
                return next(self.times)

        moved = []
        wiring = Mock(interval_seconds=0, run=Mock(return_value=[]))
        wiring.signals.extend_for_downtime = lambda **kw: moved.append(kw) or 1

        async def scenario():
            task = asyncio.create_task(detector_loop(wiring, Clock(), initial_delay_seconds=0))
            for _ in range(20):
                await asyncio.sleep(0)
            task.cancel()

        try:
            _run(scenario())
        except (asyncio.CancelledError, StopIteration, RuntimeError):
            pass
        assert moved == [{"down_since": NOW, "back_at": NOW + timedelta(hours=8)}]
