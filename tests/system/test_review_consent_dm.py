"""Phase J1 slice 2: DM-native community review consent (natural trigger, overview with buttons, owner-only decisions through the I3
service) and the formatted proactive notices (embeds). No network, no real Discord connection."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.models import ActionSpec, OutgoingMessage
from application.review_consent_query import is_review_consent_query
from application.service import ApplicationService
from bot.action_buttons import HANDLER_ATTRIBUTE, ReviewConsentButton, actions_view
from chaster_execution.review_consent import NOTICE_TEXT, OWNER_ATTESTATION, ConsentDecision, ConsentState
from messaging.discord_adapter import DiscordMessagingAdapter
from peer_review.notices import health_embed, incident_embed
from peer_review.watch import PendingNotice
from system.review_consent_dm import LockConsentView, ReviewConsentDm, action_id, render_overview
from tests.application.test_lock_and_task_commands import FIXED_TIME, _complete_onboarding, _incoming, core  # noqa: F401
from tests.chaster_execution.test_extension_review import LOCK, NOW, OWNER, FakeReader, World, on_target, raw_lock
from tests.chaster_execution.test_review_consent import private, wearer

OWNER_DISCORD = "111"


def _dm(world, locks=(LOCK,)) -> ReviewConsentDm:
    return ReviewConsentDm(world.service, world.ledger, owner_user_id=OWNER, owner_discord_id=OWNER_DISCORD,
                           list_lock_ids=lambda now: list(locks), now=lambda: NOW)


class TestTrigger:
    @pytest.mark.parametrize("message", [
        "Who can see my photos?", "stop community review", "I don't want strangers to see my pictures", "Kdo vidí moje fotky?",
        "komunitní hodnocení", "turn off peer review please", "can everyone see my photos?",
    ])
    def test_matched(self, message) -> None:
        assert is_review_consent_query(message) is True

    @pytest.mark.parametrize("message", [
        "Jak se máš?", "I took a photo of the sunset", "show my progress", "How much time is left?", "who are you?",
        "send me a picture", "my review of the movie", "x" * 200, None,
    ])
    def test_not_matched(self, message) -> None:
        assert is_review_consent_query(message) is False


class TestFacade:
    def test_overview_reads_state_and_exposure(self, tmp_path) -> None:
        world = World(tmp_path, consent=False, reader=FakeReader(wearer(raw_lock())))
        view, = _dm(world).overview()
        assert (view.lock_id, view.role, view.state, view.exposed) == (LOCK, "wearer", ConsentState.NONE, False)
        assert world.transport.calls == []

    def test_the_owner_accepts_with_one_click(self, tmp_path) -> None:
        world = World(tmp_path, consent=False, reader=FakeReader(wearer(raw_lock())))
        text = _dm(world).decide(discord_user_id=OWNER_DISCORD, lock_id=LOCK, action="accept")
        assert text.startswith("Thank you") and world.ledger.state(LOCK).approved and world.transport.calls == []

    def test_a_stranger_cannot_decide(self, tmp_path) -> None:
        world = World(tmp_path, consent=False, reader=FakeReader(wearer(raw_lock())))
        assert _dm(world).decide(discord_user_id="999", lock_id=LOCK, action="accept") == "Only the keyholder can decide this."
        assert world.ledger.state(LOCK).state is ConsentState.NONE

    def test_revoking_an_exposed_lock_switches_it_back(self, tmp_path) -> None:
        exposed = wearer(on_target(raw_lock()))
        world = World(tmp_path, reader=FakeReader(exposed, exposed, private(exposed)))
        text = _dm(world).decide(discord_user_id=OWNER_DISCORD, lock_id=LOCK, action="revoke")
        assert "revoked" in text and "keyholder only again" in text and len(world.transport.calls) == 1
        assert world.ledger.state(LOCK).state is ConsentState.REVOKED

    def test_a_failed_switch_back_is_reported(self, tmp_path) -> None:
        exposed = wearer(on_target(raw_lock()))
        world = World(tmp_path, reader=FakeReader(exposed, exposed, exposed))
        text = _dm(world).decide(discord_user_id=OWNER_DISCORD, lock_id=LOCK, action="revoke")
        assert "did NOT complete (applied_unverified)" in text

    def test_a_keyholder_lock_needs_the_owners_earlier_attestation(self, tmp_path) -> None:
        world = World(tmp_path, consent=False)                                   # role keyholder
        text = _dm(world).decide(discord_user_id=OWNER_DISCORD, lock_id=LOCK, action="accept")
        assert "--wearer-is-my-account" in text and world.ledger.state(LOCK).state is ConsentState.NONE
        world.ledger.record(lock_id=LOCK, decision=ConsentDecision.APPROVED, comment=f"owner cli: approved; {OWNER_ATTESTATION}", now=NOW)
        assert world.ledger.owner_attested(LOCK)
        text = _dm(world).decide(discord_user_id=OWNER_DISCORD, lock_id=LOCK, action="revoke")
        assert "revoked" in text and world.ledger.state(LOCK).state is ConsentState.REVOKED
        with world.core.transaction() as tx:
            latest = tx.fetch_one("SELECT user_comment FROM consent_log ORDER BY created_at DESC, rowid DESC LIMIT 1")
        assert OWNER_ATTESTATION in latest["user_comment"]

    def test_an_unknown_action_records_nothing(self, tmp_path) -> None:
        world = World(tmp_path, consent=False, reader=FakeReader(wearer(raw_lock())))
        assert "nothing was recorded" in _dm(world).decide(discord_user_id=OWNER_DISCORD, lock_id=LOCK, action="delete")


class TestRender:
    def test_buttons_follow_the_state(self) -> None:
        out = render_overview([
            LockConsentView(lock_id="aaaaaaaaaaaa", role="wearer", state=ConsentState.NONE, exposed=False),
            LockConsentView(lock_id="bbbbbbbbbbbb", role="keyholder", state=ConsentState.APPROVED, exposed=True),
            LockConsentView(lock_id="cccccccccccc", role="unknown", state=None, exposed=None),
        ])
        assert [a.action_id for a in out.actions] == [action_id("accept", "aaaaaaaaaaaa"), action_id("decline", "aaaaaaaaaaaa"),
                                                      action_id("revoke", "bbbbbbbbbbbb")]
        assert out.embed.description == NOTICE_TEXT and out.embed.color == "blue"
        assert [f.value for f in out.embed.fields] == ["Consent: not decided yet; only the keyholder reviews.",
                                                        "Consent: you agreed; community review is ON.", "Could not be read right now."]
        assert NOTICE_TEXT in out.text and "trust" not in out.text.lower()

    def test_no_locked_lock(self) -> None:
        out = render_overview([])
        assert out.actions == () and "no locked lock" in out.text


class TestThroughTheService:
    def _owner(self, core, reply):
        plain = ApplicationService(core.db_path, core=core)
        _complete_onboarding(plain)
        user = plain.user_service.get_or_create_user("discord", "42", now=FIXED_TIME)
        return ApplicationService(core.db_path, core=core, owner_user_id=user.id, review_consent_reply=reply)

    def test_the_owner_gets_the_overview(self, core) -> None:
        overview = OutgoingMessage(text="overview", actions=(ActionSpec(action_id="review_consent:accept:x", label="Accept"),))
        service = self._owner(core, lambda: overview)
        assert service.handle_message(_incoming("Who can see my photos?", external_message_id="m1")) is overview

    def test_another_user_never_gets_it(self, core) -> None:
        service = self._owner(core, lambda: OutgoingMessage(text="overview"))
        _complete_onboarding(service, external_user_id="77")
        reply = service.handle_message(_incoming("Who can see my photos?", external_user_id="77", external_message_id="x1"))
        assert reply.text != "overview" and reply.actions == ()

    def test_a_failing_read_is_a_friendly_answer(self, core) -> None:
        def boom():
            raise RuntimeError("chaster down")
        service = self._owner(core, boom)
        reply = service.handle_message(_incoming("stop community review", external_message_id="m1"))
        assert "could not read your locks" in reply.text and reply.actions == ()


class TestDiscordButtons:
    def test_actions_become_persistent_dynamic_buttons_and_unknown_ones_are_dropped(self) -> None:
        async def build():
            return actions_view([ActionSpec(action_id=action_id("accept", LOCK), label="Accept · 6aca95cd", style="success"),
                                 ActionSpec(action_id="something:else", label="x")]), actions_view([ActionSpec(action_id="x", label="x")])

        view, empty = asyncio.run(build())
        item, = view.children
        assert isinstance(item, ReviewConsentButton) and (item.action, item.lock_id) == ("accept", LOCK)
        assert item.item.label == "Accept · 6aca95cd" and view.timeout is None and empty is None

    def _click(self, facade, *, clicker=OWNER_DISCORD):
        interaction = MagicMock()
        interaction.user.id = int(clicker)
        interaction.client = SimpleNamespace(**{HANDLER_ATTRIBUTE: facade})
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.message.edit = AsyncMock()

        async def click():
            await ReviewConsentButton("accept", LOCK).callback(interaction)

        asyncio.run(click())
        return interaction

    def test_the_owners_click_records_and_removes_the_buttons(self, tmp_path) -> None:
        world = World(tmp_path, consent=False, reader=FakeReader(wearer(raw_lock())))
        interaction = self._click(_dm(world))
        interaction.response.defer.assert_awaited()
        assert interaction.followup.send.await_args.args[0].startswith("Thank you")
        assert interaction.message.edit.await_args.kwargs == {"view": None} and world.ledger.state(LOCK).approved

    def test_a_stranger_gets_an_ephemeral_refusal(self, tmp_path) -> None:
        world = World(tmp_path, consent=False, reader=FakeReader(wearer(raw_lock())))
        interaction = self._click(_dm(world), clicker="999")
        assert interaction.response.send_message.await_args.kwargs == {"ephemeral": True}
        interaction.response.defer.assert_not_awaited()
        assert world.ledger.state(LOCK).state is ConsentState.NONE

    def test_without_the_facade_it_points_to_the_cli(self) -> None:
        interaction = self._click(None)
        assert "use the CLI" in interaction.response.send_message.await_args.args[0]


class TestFormattedNotices:
    def test_incident_embed(self) -> None:
        embed = incident_embed(PendingNotice(lock_id=LOCK, review_id="r", incident_id="12345678-1234-1234-1234-123456789abc", text="t",
                                             source="tasks", verified_votes=1, rejected_votes=4))
        assert embed.title == "Photo review rejected" and embed.color == "red" and "task photo" in embed.description
        assert {f.name: f.value for f in embed.fields}["Votes"] == "1 verified / 4 rejected"
        assert "Nothing was deducted" in embed.description and "trust" not in str(embed).lower()

    def test_health_embeds(self) -> None:
        assert health_embed("a", alert=True).color == "red" and health_embed("b", alert=False).color == "green"

    def test_the_adapter_sends_one_embed_message_with_the_view(self) -> None:
        user = MagicMock()
        user.send = AsyncMock()
        client = MagicMock()
        client.is_ready.return_value = True
        client.fetch_user = AsyncMock(return_value=user)
        adapter = DiscordMessagingAdapter()
        adapter.attach(client)
        embed, view = object(), object()
        assert asyncio.run(adapter.send_message("123", "fallback text", embed=embed, view=view)) is True
        assert user.send.await_count == 1 and user.send.await_args.kwargs == {"embed": embed, "view": view}

    def test_the_loop_formats_the_incident_notice(self, tmp_path) -> None:
        from peer_review.wiring import peer_review_loop
        from tests.peer_review.test_peer_review import NOW as PNOW, World as PeerWorld, task
        w = PeerWorld(tmp_path)
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(PNOW)
        w.set(tasks=[task("pv1", "rejected")])
        sent = []

        async def send(target, text, **kw):
            sent.append(kw)
            return True

        async def run():
            clock = SimpleNamespace(now=lambda: PNOW + timedelta(minutes=5))
            loop_task = asyncio.create_task(peer_review_loop(w.watch, clock, interval_seconds=3600, send=send, owner_target="1",
                                                             initial_delay_seconds=0, view_for=lambda i: "view", embed_for=lambda spec: spec))
            for _ in range(1000):
                await asyncio.sleep(0.01)
                if sent and not w.watch.pending_notifications():
                    break
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

        asyncio.run(run())
        kw, = sent
        assert kw["view"] == "view" and kw["embed"].title == "Photo review rejected"
