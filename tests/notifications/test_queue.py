"""The notification engine: durable queue, retry, quiet hours, channel choice, and every proactive sender going through it. No network."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.models import ActionSpec, EmbedField, EmbedSpec
from infrastructure.database import Database as CoreDatabase
from messaging.discord_adapter import DiscordMessagingAdapter, DmOutcome, send_dm
from notifications.delivery import NotificationDelivery
from notifications.dispatcher import NotificationDispatcher
from notifications.queue import MAX_ATTEMPTS, RETRY_DELAYS, STALE_CLAIM, DeliveryResult, NotificationQueue, enqueue_in_transaction

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
USER = "user-1"


@pytest.fixture
def core(tmp_path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "n.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


@pytest.fixture
def queue(core) -> NotificationQueue:
    return NotificationQueue(core=core)


def _add(queue, key="k", **kwargs):
    return queue.enqueue(user_id=USER, kind="test", dedupe_key=key, text="hello", now=kwargs.pop("now", NOW), **kwargs)


def _rows(core):
    with core.transaction() as tx:
        return [dict(r) for r in tx.fetch_all("SELECT * FROM notifications ORDER BY created_at, rowid")]


class Port:
    def __init__(self, *results) -> None:
        self.results, self.sent = list(results), []

    async def __call__(self, notification):
        self.sent.append(notification)
        result = self.results.pop(0) if self.results else DeliveryResult.DELIVERED
        if isinstance(result, Exception):
            raise result
        return result


def _pass(queue, port, at=NOW, quiet=False):
    return asyncio.run(NotificationDispatcher(queue, deliver=port, is_quiet=lambda _n: quiet).run_once(at))


class TestQueue:
    def test_an_enqueue_is_idempotent(self, queue, core) -> None:
        assert _add(queue) is True and _add(queue) is False
        assert len(_rows(core)) == 1

    def test_a_notice_written_in_a_rolled_back_transaction_does_not_exist(self, core) -> None:
        """The outbox guarantee: the fact and the notice about it commit together or not at all."""
        with pytest.raises(RuntimeError):
            with core.transaction() as tx:
                enqueue_in_transaction(tx, user_id=USER, kind="t", dedupe_key="k", text="x", now=NOW)
                raise RuntimeError("the state change failed")
        assert _rows(core) == []

    def test_a_card_survives_the_queue_exactly(self, queue) -> None:
        embed = EmbedSpec(title="T", description="D", color="red", fields=(EmbedField(name="a", value="b"),))
        actions = (ActionSpec(action_id="x:y:z", label="Go", style="danger"),)
        _add(queue, embed=embed, actions=actions)
        claimed = queue.claim_due(NOW, quiet=False)[0]
        assert claimed.embed == embed and claimed.actions == actions

    def test_a_claimed_notice_is_not_claimed_twice(self, queue) -> None:
        _add(queue)
        assert len(queue.claim_due(NOW, quiet=False)) == 1 and queue.claim_due(NOW, quiet=False) == []

    def test_a_crash_while_sending_puts_the_notice_back(self, queue, core) -> None:
        _add(queue)
        queue.claim_due(NOW, quiet=False)
        assert queue.claim_due(NOW + STALE_CLAIM - timedelta(seconds=1), quiet=False) == []
        assert len(queue.claim_due(NOW + STALE_CLAIM, quiet=False)) == 1


class TestDelivery:
    def test_a_delivered_notice_is_done(self, queue, core) -> None:
        _add(queue)
        assert _pass(queue, Port()) == 1
        row = _rows(core)[0]
        assert row["status"] == "delivered" and row["attempts"] == 1 and row["delivered_at"] is not None
        assert _pass(queue, Port(), NOW + timedelta(days=1)) == 0

    @pytest.mark.parametrize("result", [DeliveryResult.UNDELIVERABLE, DeliveryResult.UNKNOWN, RuntimeError("boom")])
    def test_a_failed_delivery_is_retried_after_the_delay(self, queue, core, result) -> None:
        _add(queue)
        assert _pass(queue, Port(result)) == 0
        row = _rows(core)[0]
        assert row["status"] == "pending" and row["attempts"] == 1
        assert _pass(queue, Port(), NOW + RETRY_DELAYS[0] - timedelta(seconds=1)) == 0            # not yet
        assert _pass(queue, Port(), NOW + RETRY_DELAYS[0]) == 1
        assert _rows(core)[0]["status"] == "delivered"

    def test_the_delays_rise(self, queue, core) -> None:
        _add(queue)
        at, gaps = NOW, []
        for _ in range(7):
            _pass(queue, Port(DeliveryResult.UNKNOWN), at)
            following = datetime.fromisoformat(_rows(core)[0]["next_attempt_at"].replace("Z", "+00:00"))
            gaps.append(following - at)
            at = following
        assert gaps == sorted(gaps) and gaps[0] == RETRY_DELAYS[0] and gaps[-1] == timedelta(hours=1)

    def test_a_dead_channel_gives_up_but_keeps_the_row(self, queue, core) -> None:
        _add(queue)
        at = NOW
        for _ in range(MAX_ATTEMPTS):
            _pass(queue, Port(DeliveryResult.UNDELIVERABLE), at)
            at += timedelta(days=1)
        row = _rows(core)[0]
        assert row["status"] == "failed" and row["attempts"] == MAX_ATTEMPTS

    def test_one_bad_notice_does_not_stop_the_next(self, queue) -> None:
        _add(queue, key="a")
        _add(queue, key="b")
        port = Port(RuntimeError("boom"))
        assert _pass(queue, port) == 1 and len(port.sent) == 2


class TestQuietHours:
    def test_a_routine_notice_waits_and_loses_no_attempt(self, queue, core) -> None:
        _add(queue)
        port = Port()
        assert _pass(queue, port, quiet=True) == 0 and port.sent == []
        assert _rows(core)[0]["attempts"] == 0
        assert _pass(queue, port) == 1

    def test_an_immediate_notice_goes_out_at_night(self, queue) -> None:
        _add(queue, immediate=True)
        assert _pass(queue, Port(), quiet=True) == 1

    def test_an_unreadable_schedule_holds_routine_notices_back(self, queue) -> None:
        _add(queue)

        def broken(_now):
            raise ValueError("bad timezone")

        assert asyncio.run(NotificationDispatcher(queue, deliver=Port(), is_quiet=broken).run_once(NOW)) == 0


class Discord:
    def __init__(self, outcome=DmOutcome.DELIVERED) -> None:
        self.outcome, self.calls = outcome, []

    async def send_outcome(self, target, text, *, view=None, embed=None):
        self.calls.append((target, text, view, embed))
        return self.outcome


class Router:
    def __init__(self, sent=True) -> None:
        self.sent, self.calls = sent, []

    async def send_to_user(self, user_id, text):
        self.calls.append((user_id, text))
        return self.sent


def _note(embed=None, actions=()):
    from notifications.queue import Notification
    return Notification(id="1", user_id=USER, kind="t", text="hi", embed=embed, actions=actions, immediate=False, attempts=0)


class TestChannelChoice:
    def _delivery(self, discord, router=None, discord_id="42"):
        return NotificationDelivery(discord=discord, discord_id_for=lambda _u: discord_id, router=router, embed_for=lambda s: ("embed", s.title),
                                    view_for=lambda a: ("view", len(a)))

    def test_a_plain_notice_follows_the_stored_channel_preference(self) -> None:
        discord, router = Discord(), Router()
        assert asyncio.run(self._delivery(discord, router)(_note())) is DeliveryResult.DELIVERED
        assert router.calls == [(USER, "hi")] and discord.calls == []

    def test_a_failed_routed_notice_is_retried(self) -> None:
        assert asyncio.run(self._delivery(Discord(), Router(sent=False))(_note())) is DeliveryResult.UNKNOWN

    def test_a_card_with_buttons_always_goes_to_discord(self) -> None:
        """Chaster chat cannot show a button, and a decision nobody can press is worse than a later one."""
        discord, router = Discord(), Router()
        asyncio.run(self._delivery(discord, router)(_note(actions=(ActionSpec(action_id="a", label="b"),))))
        assert router.calls == [] and discord.calls == [("42", "hi", ("view", 1), None)]

    def test_no_discord_identity_is_undeliverable(self) -> None:
        assert asyncio.run(self._delivery(Discord(), discord_id=None)(_note(embed=EmbedSpec(title="x")))) is DeliveryResult.UNDELIVERABLE


class Forbidden(Exception):
    pass


class Unavailable(Exception):
    pass


class TestOneDmPath:
    def _client(self, exc=None):
        async def send(*_a, **_k):
            if exc:
                raise exc

        async def fetch_user(_id):
            return SimpleNamespace(send=send)

        return SimpleNamespace(fetch_user=fetch_user, is_ready=lambda: True)

    def test_outcomes(self) -> None:
        assert asyncio.run(send_dm(self._client(), 1, "x")) is DmOutcome.DELIVERED
        assert asyncio.run(send_dm(self._client(Forbidden()), 1, "x")) is DmOutcome.UNDELIVERABLE
        assert asyncio.run(send_dm(self._client(Unavailable()), 1, "x")) is DmOutcome.UNKNOWN

    def test_a_client_that_is_not_ready_yet_is_only_a_delay(self) -> None:
        adapter = DiscordMessagingAdapter()
        adapter.attach(SimpleNamespace(is_ready=lambda: False))
        assert asyncio.run(adapter.send_outcome("1", "x")) is DmOutcome.UNKNOWN

    def test_only_send_dm_opens_a_dm(self) -> None:
        """The duplicate send code is gone, and it stays gone: `fetch_user` appears once in everything that runs in production."""
        found = []
        for path in ROOT.rglob("*.py"):
            parts = path.relative_to(ROOT).parts
            if parts[0] in ("tests", "hive_mind_receiver", "scripts") or ".venv" in parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found += [str(path.relative_to(ROOT)) for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "fetch_user"]
        assert found == [str(Path("messaging/discord_adapter.py"))]


class TestSendersUseTheQueue:
    def test_the_release_notice_skips_quiet_hours_and_a_retry_is_one_message(self, queue, core) -> None:
        from infrastructure.clock import FrozenClock
        from system.composition import _release_sender
        send = _release_sender(queue, None, USER, FrozenClock(NOW))
        send("Releasing you now.")
        send("Releasing you now.")
        rows = _rows(core)
        assert len(rows) == 1 and rows[0]["immediate"] == 1 and rows[0]["kind"] == "release"

    def test_the_check_in_warning_is_handed_over_durably(self, queue, core) -> None:
        from infrastructure.clock import FrozenClock
        from system.composition import _warning_notifier
        assert _warning_notifier(queue, None, USER, FrozenClock(NOW))("Warning") is True
        assert _rows(core)[0]["kind"] == "check_in_warning"

    def test_a_tenant_without_a_queue_still_sends_directly(self) -> None:
        from supervision.checkins import SendOutcome
        from system.composition import _warning_notifier
        sender = SimpleNamespace(send=lambda **_k: SendOutcome.DELIVERED)
        assert _warning_notifier(None, sender, USER, None)("Warning") is True

    def test_the_peer_review_notice_keeps_its_buttons_in_the_queue(self, queue, core) -> None:
        from infrastructure.clock import FrozenClock
        from peer_review.discord_buttons import decision_actions
        from peer_review.wiring import queued_send
        send = queued_send(queue, user_id=USER, clock=FrozenClock(NOW))
        incident = "12345678-1234-1234-1234-123456789012"
        assert asyncio.run(send("ignored", "rejected", view=decision_actions(incident), embed=EmbedSpec(title="x"))) is True
        claimed = queue.claim_due(NOW, quiet=False)[0]
        assert [a.action_id for a in claimed.actions] == [f"peer_review:confirm:{incident}", f"peer_review:dismiss:{incident}"]

    def test_every_queued_button_has_a_registered_handler(self) -> None:
        """A card that waits in the queue must still be pressable when it arrives."""
        from bot.action_buttons import REGISTERED_TEMPLATES
        ident = "12345678-1234-1234-1234-123456789012"
        for action_id in (f"peer_review:confirm:{ident}", f"exemption:approve:{ident}", f"exemption:cat_work_school:{ident}", f"exemption:h168:{ident}",
                          f"infraction:health:{ident}", f"hygiene:planned:{ident}", f"lock_report:locked:{ident}"):
            assert any(pattern.fullmatch(action_id) for pattern, _ in REGISTERED_TEMPLATES), action_id
