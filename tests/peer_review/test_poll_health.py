"""Phase I4 hardening: the keyholder is alerted after 3 consecutive failed community review polls, and told once when it recovers."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from peer_review.watch import FAILURE_THRESHOLD, PollHealth, PollResult
from peer_review.wiring import peer_review_loop
from tests.peer_review.test_peer_review import NOW


class TestPollHealth:
    def test_the_third_consecutive_failure_queues_exactly_one_alert(self) -> None:
        health = PollHealth()
        assert FAILURE_THRESHOLD == 3
        health.observe(failed=True, detail="x tasks: api_error:503")
        health.observe(failed=True, detail="x tasks: api_error:503")
        assert health.pending is None
        health.observe(failed=True, detail="x tasks: api_error:503")
        assert "the last 3 polls failed (x tasks: api_error:503)" in health.pending and "NOT being seen" in health.pending
        health.delivered()
        health.observe(failed=True, detail="again")
        assert health.pending is None and health.failures == 4                  # one alert per failure streak

    def test_a_success_resets_the_streak(self) -> None:
        health = PollHealth()
        for failed in (True, True, False, True, True):
            health.observe(failed=failed, detail="d")
        assert health.pending is None and health.failures == 2

    def test_recovery_after_a_delivered_alert_is_announced_once(self) -> None:
        health = PollHealth()
        for _ in range(3):
            health.observe(failed=True, detail="reauthorization_required")
        assert "chaster connect" in health.pending
        health.delivered()
        health.observe(failed=False)
        assert health.pending == "Community review watch: polling works again after 3 failed attempts."
        health.delivered()
        health.observe(failed=False)
        assert health.pending is None

    def test_recovery_before_the_alert_was_delivered_drops_both(self) -> None:
        health = PollHealth()
        for _ in range(3):
            health.observe(failed=True, detail="d")
        health.observe(failed=False)
        assert health.pending is None and not health.alerted


class _Watch:
    """A stand-in watch whose polls fail or succeed on a script."""

    def __init__(self, script) -> None:
        self.script = list(script)

    def poll(self, now):
        step = self.script.pop(0) if self.script else "ok"
        if step == "raise":
            raise RuntimeError("network down")
        return PollResult(problems=[] if step == "ok" else ["lock tasks: api_error:503"])

    def pending_notifications(self):
        return []


def _run(watch, send, *, polls: int):
    health = PollHealth()

    async def run():
        clock = SimpleNamespace(now=lambda: NOW + timedelta(minutes=5))
        task = asyncio.create_task(peer_review_loop(watch, clock, interval_seconds=0.001, send=send, owner_target="123",
                                                    initial_delay_seconds=0, health=health))
        for _ in range(2000):
            await asyncio.sleep(0.005)
            if len(watch.script) == 0 and polls_done[0] >= polls:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    polls_done = [0]
    original = watch.poll

    def counting(now):
        polls_done[0] += 1
        return original(now)

    watch.poll = counting
    asyncio.run(run())
    return health


def test_the_loop_alerts_after_three_failures_including_exceptions_and_announces_recovery() -> None:
    sent = []

    async def send(target, text, **kw):
        sent.append(text)
        return True

    _run(_Watch(["fail", "raise", "fail", "fail", "ok"]), send, polls=6)
    alerts = [t for t in sent if "polls failed" in t]
    assert len(alerts) == 1 and "the last 3 polls failed" in alerts[0]
    assert sent.count("Community review watch: polling works again after 4 failed attempts.") == 1


def test_an_undelivered_alert_is_retried() -> None:
    attempts = []

    async def send(target, text, **kw):
        attempts.append(text)
        return len(attempts) >= 2                     # Discord refuses the first try

    health = _run(_Watch(["fail", "fail", "fail", "fail", "fail"]), send, polls=5)
    assert len(attempts) == 2 and attempts[0] == attempts[1] and health.pending is None
