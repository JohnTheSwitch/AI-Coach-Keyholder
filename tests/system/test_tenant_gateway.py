"""v0.39 Phase E3: Discord routing by author, invitation and acceptance, wearer `halt` / `resume` / `leave`, isolation. No network, no Discord."""

from __future__ import annotations

import asyncio
import io

import pytest

from application.models import IncomingMessage, OutgoingMessage
from chaster_execution.control import ExecutionControl
from cli.main import main
from database.database import Database
from system.tenant_gateway import TEXTS, InvitationNotifier, TenantGateway, invitation_text
from system.tenant_loops import TenantLoopSupervisor
from tenancy.admin import TenantAdmin
from tenancy.models import TENANT_ZERO_ID, WEARER_HALT_REASON, RemovedBy, TenantStatus
from tests.system.test_tenant_factory import NOW, OWNER, World

OWNER_DISCORD = "1000001"


def run(coro):
    return asyncio.run(coro)


class G:
    """A gateway over a real registry, a real tenant factory and supervisor, and a fake tenant-zero handler."""

    def __init__(self, tmp_path, owner=OWNER) -> None:
        self.w = World(tmp_path)
        self.zero_calls: list[str] = []
        self.supervisor = TenantLoopSupervisor(registry=self.w.platform.registry, factory=self.w.factory, interval_seconds=60)
        self.gateway = TenantGateway(
            registry=self.w.platform.registry, supervisor=self.supervisor,
            admin=TenantAdmin(registry=self.w.platform.registry, halt=self.w.platform.halt, owner_user_id=owner, clock=self.w.clock),
            owner_user_id=owner, tenant_zero_handler=self._zero, tenant_zero_db=Database(self.w.main_path, core=self.w.main),
            tenant_dir=self.w.platform.tenant_dir, clock=self.w.clock)
        self.registry = self.w.platform.registry

    def _zero(self, incoming):
        self.zero_calls.append(incoming.text)
        return OutgoingMessage(text="owner reply")

    def say(self, discord_id, text):
        return run(self.gateway.handle(IncomingMessage(channel="discord", external_user_id=discord_id, text=text,
                                                       received_at=NOW, external_message_id="m")))

    def invite(self, discord_id="2000002", name="Alex"):
        return self.say(OWNER_DISCORD, f"invite {discord_id} {name}")

    def member(self, discord_id="2000002", name="Alex"):
        self.invite(discord_id, name)
        assert self.say(discord_id, "accept").text == TEXTS["accepted"]
        return self.registry.find_by_discord_id(discord_id)

    def control(self, tenant_id):
        runtime = self.supervisor.runtimes[tenant_id]
        return ExecutionControl(runtime.db_path, core=runtime.core)


@pytest.fixture
def g(tmp_path):
    return G(tmp_path)


class TestRouting:
    def test_a_stranger_is_refused_and_nothing_is_processed_or_stored(self, g) -> None:
        before = g.w.fingerprint(g.w.main_path)
        reply = g.say("9999999", "hello, who are you?")
        assert reply.text == TEXTS["stranger"] and reply.log_db is None and g.zero_calls == []
        assert g.w.fingerprint(g.w.main_path) == before and not g.w.platform.tenant_dir.exists()

    def test_the_owner_reaches_the_existing_system(self, g) -> None:
        reply = g.say(OWNER_DISCORD, "how are you")
        assert reply.text == "owner reply" and g.zero_calls == ["how are you"]
        assert reply.log_db is not None and reply.log_db.db_path == g.w.main_path

    def test_an_active_wearer_reaches_only_their_own_tenant(self, g) -> None:
        before = g.w.fingerprint(g.w.main_path)
        tenant = g.member()
        reply = g.say("2000002", "hello")
        assert "Welcome" in reply.text                                            # the tenant's own onboarding starts
        assert reply.log_db.db_path == g.supervisor.runtimes[tenant.id].db_path != g.w.main_path
        assert g.zero_calls == [] and g.w.fingerprint(g.w.main_path) == before

    def test_two_wearers_are_routed_to_two_databases(self, g) -> None:
        a, b = g.member("2000002", "Alex"), g.member("3000003", "Bo")
        ra, rb = g.say("2000002", "hi"), g.say("3000003", "hi")
        assert ra.log_db.db_path != rb.log_db.db_path
        with g.supervisor.runtimes[a.id].core.transaction() as tx:
            ids = {r["external_id"] for r in tx.fetch_all("SELECT external_id FROM user_channel_identities")}
        assert ids == {"2000002"}

    def test_a_removed_person_is_a_stranger_again(self, g) -> None:
        tenant = g.member()
        g.registry.remove(tenant.id, actor="owner", now=NOW)
        assert g.say("2000002", "hello").text == TEXTS["stranger"]

    def test_a_suspended_wearer_gets_a_notice_and_nothing_is_processed(self, g) -> None:
        tenant = g.member()
        g.registry.suspend(tenant.id, actor="owner", now=NOW)
        assert g.say("2000002", "hello").text == TEXTS["suspended"]
        with g.supervisor.runtimes[tenant.id].core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM user_preferences")["n"] == 0


class TestInvitation:
    def test_the_owner_invites_from_a_dm(self, g) -> None:
        reply = g.invite()
        assert "Invitation created" in reply.text
        t = g.registry.find_by_discord_id("2000002")
        assert t.status is TenantStatus.INVITED and t.display_name == "Alex"

    def test_invite_usage_and_refusals(self, g) -> None:
        assert g.say(OWNER_DISCORD, "invite").text == TEXTS["invite_usage"]
        assert "Usage" in g.say(OWNER_DISCORD, "invite 2000002").text
        assert "Refused" in g.say(OWNER_DISCORD, "invite abc Alex").text
        g.invite()
        assert "already belongs" in g.invite().text

    def test_only_the_owner_can_invite(self, g) -> None:
        g.member("2000002")
        g.say("2000002", "invite 4000004 Mallory")
        assert g.registry.find_by_discord_id("4000004") is None

    def test_without_an_owner_identity_nobody_can_invite(self, tmp_path) -> None:
        gw = G(tmp_path, owner=None)
        assert "not configured" in gw.invite().text and gw.registry.find_by_discord_id("2000002") is None

    def test_an_invitee_only_sees_the_invitation_until_they_answer(self, g) -> None:
        g.invite()
        for text in ("hello", "halt", "leave", "status"):
            reply = g.say("2000002", text)
            assert reply.text == invitation_text("Alex") and reply.log_db is None
        assert "accept" in invitation_text("Alex") and "halt" in invitation_text("Alex") and "leave" in invitation_text("Alex")
        assert not g.w.platform.tenant_dir.exists()                               # nothing is created before consent

    def test_accept_activates_records_consent_and_builds_the_tenant(self, g) -> None:
        g.invite()
        assert g.say("2000002", "  ACCEPT ").text == TEXTS["accepted"]
        t = g.registry.find_by_discord_id("2000002")
        assert t.status is TenantStatus.ACTIVE and t.accepted_at is not None
        runtime = g.supervisor.runtimes[t.id]
        with runtime.core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM consent_log WHERE target_id = 'multi_wearer_keyholder'")
        assert row["action"] == "approved" and row["explanation_shown"] == invitation_text("Alex")
        assert ExecutionControl(runtime.db_path, core=runtime.core).is_halted() is True     # nothing acts until the owner enables it

    def test_decline_removes_the_invitation_as_the_wearers_own_choice(self, g) -> None:
        g.invite()
        assert g.say("2000002", "decline").text == TEXTS["declined"]
        t = g.registry.list_tenants(include_removed=True)[-1]
        assert t.status is TenantStatus.REMOVED and t.removed_by is RemovedBy.WEARER
        assert g.say("2000002", "accept").text == TEXTS["stranger"]


class TestInvitationDelivery:
    def test_pending_invitations_are_sent_once_and_failures_retried(self, g) -> None:
        g.invite("2000002", "Alex")
        g.invite("3000003", "Bo")
        sent, fail = [], {"3000003"}

        async def send(discord_id, text):
            sent.append((discord_id, text))
            return discord_id not in fail

        notifier = InvitationNotifier(registry=g.registry, send=send, clock=g.w.clock)
        assert run(notifier.run_once()) == 1
        fail.clear()
        assert run(notifier.run_once()) == 1 and run(notifier.run_once()) == 0
        assert [d for d, _ in sent] == ["2000002", "3000003", "3000003"]
        assert sent[0][1] == invitation_text("Alex")

    def test_an_accepted_or_declined_invitation_is_not_sent(self, g) -> None:
        g.invite("2000002")
        g.say("2000002", "decline")
        sent = []

        async def send(discord_id, text):
            sent.append(discord_id)
            return True

        assert run(InvitationNotifier(registry=g.registry, send=send, clock=g.w.clock).run_once()) == 0 and sent == []

    def test_a_raising_sender_is_contained(self, g) -> None:
        g.invite()

        async def send(discord_id, text):
            raise RuntimeError("discord down")

        assert run(InvitationNotifier(registry=g.registry, send=send, clock=g.w.clock).run_once()) == 0
        assert len(g.registry.pending_invitations()) == 1


class TestWearerSafetyCommands:
    def test_halt_stops_only_this_tenant_and_resume_clears_only_the_wearers_halt(self, g) -> None:
        a, b = g.member("2000002", "Alex"), g.member("3000003", "Bo")
        for tid in (a.id, b.id):
            g.control(tid).set_halted(False, reason="owner enabled", now=NOW)
        assert g.say("2000002", "halt").text == TEXTS["halted"]
        assert g.control(a.id).is_halted() and g.control(a.id).reason() == WEARER_HALT_REASON
        assert g.control(b.id).is_halted() is False
        assert g.say("2000002", "resume").text == TEXTS["resumed"] and g.control(a.id).is_halted() is False

    def test_resume_cannot_clear_the_keyholders_or_the_initial_halt(self, g) -> None:
        a = g.member()
        assert g.say("2000002", "resume").text == TEXTS["resume_refused"]             # the initial halt of a new tenant
        g.control(a.id).set_halted(True, reason="keyholder pause", now=NOW)
        assert g.say("2000002", "resume").text == TEXTS["resume_refused"] and g.control(a.id).is_halted()

    def test_the_owner_cannot_clear_a_wearers_halt_from_the_cli(self, g) -> None:
        a = g.member()
        g.control(a.id).set_halted(False, reason="enabled", now=NOW)
        g.say("2000002", "halt")
        admin = TenantAdmin(registry=g.registry, halt=g.w.platform.halt, owner_user_id=OWNER, clock=g.w.clock)
        out = io.StringIO()
        code = main(["tenants", "execution", a.id, "clear", "--reason", "x", "--owner-id", OWNER], out=out,
                    tenants_factory=lambda: (admin, g.w.platform), now=lambda: NOW)
        assert code == 3 and "only the wearer" in out.getvalue() and g.control(a.id).is_halted()

    def test_halt_works_while_suspended_and_wins_over_everything(self, g) -> None:
        a = g.member()
        g.control(a.id).set_halted(False, reason="enabled", now=NOW)
        g.registry.suspend(a.id, actor="owner", now=NOW)
        assert g.say("2000002", "halt").text == TEXTS["halted"] and g.control(a.id).is_halted()

    def test_halt_after_a_restart_uses_the_existing_database(self, g) -> None:
        a = g.member()
        path = g.supervisor.runtimes[a.id].db_path
        ExecutionControl(path).set_halted(False, reason="enabled", now=NOW)
        g.supervisor.runtimes.clear()                                             # as after a restart, before the loop rebuilt it
        assert g.say("2000002", "halt").text == TEXTS["halted"]
        assert ExecutionControl(path).is_halted() and ExecutionControl(path).reason() == WEARER_HALT_REASON

    def test_leave_needs_confirmation_then_ends_everything_at_once(self, g) -> None:
        a = g.member()
        g.control(a.id).set_halted(False, reason="enabled", now=NOW)
        path = g.supervisor.runtimes[a.id].db_path
        assert g.say("2000002", "leave").text == TEXTS["leave_confirm"] and g.registry.is_managed(a.id)
        assert g.say("2000002", "leave confirm").text == TEXTS["left"]
        gone = g.registry.get(a.id)
        assert gone.status is TenantStatus.REMOVED and gone.removed_by is RemovedBy.WEARER
        assert a.id not in g.supervisor.runtimes and a.id not in g.supervisor.running
        assert ExecutionControl(path).is_halted() and path.exists()                # halted; the file is kept (owner decision)
        assert g.say("2000002", "hello").text == TEXTS["stranger"]

    def test_a_suspended_wearer_can_still_leave(self, g) -> None:
        a = g.member()
        g.registry.suspend(a.id, actor="owner", now=NOW)
        assert g.say("2000002", "leave confirm").text == TEXTS["left"] and g.registry.get(a.id).status is TenantStatus.REMOVED

    def test_the_owner_can_halt_and_resume_but_not_leave(self, g) -> None:
        zero_control = ExecutionControl(g.w.main_path, core=g.w.main)
        zero_control.set_halted(False, reason="enabled", now=NOW)
        assert g.say(OWNER_DISCORD, "halt").text == TEXTS["halted"] and zero_control.is_halted()
        assert g.say(OWNER_DISCORD, "resume").text == TEXTS["resumed"] and not zero_control.is_halted()
        assert g.say(OWNER_DISCORD, "leave confirm").text == TEXTS["owner_cannot_leave"]
        assert g.registry.get(TENANT_ZERO_ID).status is TenantStatus.ACTIVE and g.zero_calls == []

    def test_safety_commands_are_exact_and_never_reach_a_model(self, g) -> None:
        a = g.member()
        g.say("2000002", "please halt the conversation")                           # not a command: ordinary text
        assert g.control(a.id).reason() != WEARER_HALT_REASON
        before = len(g.w.model.prompts)
        g.say("2000002", "halt")
        assert len(g.w.model.prompts) == before


class TestBotWiring:
    """The Discord adapter's gateway path: replies go back to the DM and the exchange is stored only in the right database."""

    def _bot(self, g):
        from unittest.mock import AsyncMock, Mock

        import discord
        from application.service import ApplicationService
        from bot.discord_bot import build_bot
        from core.config import Config
        bot = build_bot(Config(discord_token="t", db_path=g.w.main_path), Database(g.w.main_path, core=g.w.main), g.w.clock,
                        ApplicationService(g.w.main_path, core=g.w.main))
        bot.gateway = g.gateway

        def message(author, text, mid=1):
            m = Mock()
            m.author = Mock()
            m.author.id = int(author)
            m.content = text
            m.id = mid
            m.channel = Mock(spec=discord.DMChannel)
            m.channel.id = 999
            m.channel.send = AsyncMock(return_value=Mock(id=mid + 1000))
            return m

        return bot, message

    def _logged(self, core):
        with core.transaction() as tx:
            return [r["content"] for r in tx.fetch_all("SELECT content FROM conversation_messages ORDER BY rowid")]

    def test_a_stranger_gets_the_refusal_and_nothing_is_logged_anywhere(self, g) -> None:
        bot, message = self._bot(g)
        m = message("9999999", "my secret text")
        run(bot.on_message(m))
        m.channel.send.assert_awaited_once_with(TEXTS["stranger"])
        assert self._logged(g.w.main) == []

    def test_a_wearers_exchange_is_logged_only_in_their_tenant_database(self, g) -> None:
        tenant = g.member()
        bot, message = self._bot(g)
        run(bot.on_message(message("2000002", "wearer text")))
        runtime = g.supervisor.runtimes[tenant.id]
        assert self._logged(runtime.core)[0] == "wearer text" and len(self._logged(runtime.core)) == 2
        assert "wearer text" not in self._logged(g.w.main)

    def test_the_owners_exchange_is_logged_in_the_main_database(self, g) -> None:
        bot, message = self._bot(g)
        run(bot.on_message(message(OWNER_DISCORD, "owner text")))
        assert self._logged(g.w.main) == ["owner text", "owner reply"]

    def test_without_a_gateway_the_bot_behaves_exactly_as_before(self, g) -> None:
        bot, message = self._bot(g)
        bot.gateway = None
        m = message("9999999", "first message")
        run(bot.on_message(m))
        assert "Welcome" in m.channel.send.await_args.args[0] and self._logged(g.w.main)[0] == "first message"
