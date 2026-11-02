"""v0.39 Phase E6: archival of removed tenants and the final isolation audit across tenants. No network, no Discord."""

from __future__ import annotations

import asyncio
import io
import os
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from cli.main import main
from infrastructure.clock import FrozenClock
from system.tenant_loops import TenantLoopSupervisor
from tenancy.admin import TenantAdmin
from tenancy.archive import ARCHIVE_DIR_NAME, ARCHIVE_GRACE, TenantArchiver
from tenancy.models import TenantStatus
from tenancy.registry import TenantError
from tests.system.test_e5_chat_budget import ChatWorld, msg
from tests.system.test_tenant_factory import NOW, OWNER, World
from tests.system.test_tenant_gateway import G


def run(coro):
    return asyncio.run(coro)


class A:
    """One world with one built tenant and an archiver whose clock we can move."""

    def __init__(self, tmp_path) -> None:
        self.w = World(tmp_path)
        self.reg = self.w.platform.registry
        self.tenant = self.w.wearer("Alex", "2000002")
        self.runtime = self.w.factory.build(self.tenant.id)
        self.clock = FrozenClock(NOW)
        self.archiver = TenantArchiver(registry=self.reg, tenant_dir=self.w.platform.tenant_dir, clock=self.clock)

    def remove(self):
        self.reg.remove(self.tenant.id, actor="owner", now=NOW)

    def later(self, delta=ARCHIVE_GRACE + timedelta(seconds=1)):
        self.clock.set(NOW + delta)


@pytest.fixture
def a(tmp_path):
    return A(tmp_path)


class TestArchival:
    def test_a_removed_tenants_database_is_moved_to_the_read_only_archive_after_the_grace(self, a) -> None:
        live = a.runtime.db_path
        a.remove()
        assert a.archiver.archive_pending() == []                                         # still within the grace period
        a.later()
        assert a.archiver.archive_pending() == [a.tenant.id]
        t = a.reg.get(a.tenant.id)
        archived = a.w.platform.tenant_dir / ARCHIVE_DIR_NAME / t.archive_file
        assert not live.exists() and archived.exists() and t.archived_at is not None
        assert not os.stat(archived).st_mode & stat.S_IWRITE                              # read-only
        assert [e["event"] for e in a.reg.events(a.tenant.id)][0] == "archived"
        conn = sqlite3.connect(f"file:{archived}?mode=ro", uri=True)                       # the audit data is intact
        try:
            assert conn.execute("SELECT COUNT(*) FROM user_channel_identities").fetchone()[0] == 1
        finally:
            conn.close()
        os.chmod(archived, stat.S_IWRITE | stat.S_IREAD)                                  # let pytest clean up

    def test_it_happens_once_and_never_for_live_tenants_or_tenant_zero(self, a) -> None:
        bo = a.w.wearer("Bo", "3000003")
        a.w.factory.build(bo.id)
        a.reg.suspend(bo.id, actor="owner", now=NOW)
        a.remove()
        a.later()
        assert a.archiver.archive_pending() == [a.tenant.id] and a.archiver.archive_pending() == []
        assert (a.w.platform.tenant_dir / f"{bo.id}.sqlite").exists() and a.w.main_path.exists()
        archived = a.w.platform.tenant_dir / ARCHIVE_DIR_NAME / a.reg.get(a.tenant.id).archive_file
        os.chmod(archived, stat.S_IWRITE | stat.S_IREAD)

    def test_a_running_tenant_waits_for_the_next_pass(self, a) -> None:
        a.remove()
        a.later()
        assert a.archiver.archive_pending(is_running=lambda tid: True) == [] and a.runtime.db_path.exists()

    def test_a_declined_invitation_has_nothing_to_move(self, a) -> None:
        a.w.admin.invite(actor=OWNER, display_name="Never", discord_id="4000004")
        a.reg.leave(discord_id="4000004", now=NOW)
        a.later()
        declined = [t for t in a.reg.list_tenants(include_removed=True) if t.discord_id == "4000004"][0]
        assert declined.id in a.archiver.archive_pending()
        assert a.reg.get(declined.id).archived_at is not None and a.reg.get(declined.id).archive_file is None

    def test_side_files_travel_with_the_database(self, a, monkeypatch) -> None:
        # SQLite itself consumes a stale journal when the checkpoint opens the file, so the move logic is checked with the checkpoint skipped.
        checkpoints = []
        monkeypatch.setattr(TenantArchiver, "_checkpoint", staticmethod(lambda path: checkpoints.append(path)))
        side = Path(str(a.runtime.db_path) + "-journal")
        side.write_bytes(b"x")
        a.remove()
        a.later()
        a.archiver.archive_pending()
        archived = a.w.platform.tenant_dir / ARCHIVE_DIR_NAME / a.reg.get(a.tenant.id).archive_file
        assert not side.exists() and Path(str(archived) + "-journal").exists() and checkpoints == [a.runtime.db_path]
        os.chmod(archived, stat.S_IWRITE | stat.S_IREAD)

    def test_a_failed_move_is_retried_and_never_marks_the_tenant(self, a, monkeypatch) -> None:
        a.remove()
        a.later()
        real = os.replace

        def locked(src, dst):
            raise PermissionError("file in use")

        monkeypatch.setattr(os, "replace", locked)
        assert a.archiver.archive_pending() == [] and a.reg.get(a.tenant.id).archived_at is None
        monkeypatch.setattr(os, "replace", real)
        assert a.archiver.archive_pending() == [a.tenant.id]
        os.chmod(a.w.platform.tenant_dir / ARCHIVE_DIR_NAME / a.reg.get(a.tenant.id).archive_file, stat.S_IWRITE | stat.S_IREAD)

    def test_an_archived_tenant_is_never_built_or_reached_again(self, a) -> None:
        a.remove()
        a.later()
        a.archiver.archive_pending()
        with pytest.raises(TenantError):
            a.w.factory.build(a.tenant.id)
        assert not a.runtime.db_path.exists()
        again = a.w.admin.invite(actor=OWNER, display_name="Alex", discord_id="2000002").tenant          # a re-invite is a fresh tenant
        assert again.id != a.tenant.id and again.db_file != a.tenant.db_file
        os.chmod(a.w.platform.tenant_dir / ARCHIVE_DIR_NAME / a.reg.get(a.tenant.id).archive_file, stat.S_IWRITE | stat.S_IREAD)


class TestArchivalWiring:
    def test_the_supervisor_archives_after_a_wearer_leaves(self, tmp_path) -> None:
        g = G(tmp_path)
        tenant = g.member()
        clock = FrozenClock(NOW)
        g.supervisor._archiver = TenantArchiver(registry=g.registry, tenant_dir=g.w.platform.tenant_dir, clock=clock)
        path = g.supervisor.runtimes[tenant.id].db_path
        g.say("2000002", "leave confirm")
        clock.set(NOW + ARCHIVE_GRACE + timedelta(seconds=1))
        run(g.supervisor.reconcile())
        archived = g.registry.get(tenant.id)
        assert archived.archived_at is not None and not path.exists()
        os.chmod(g.w.platform.tenant_dir / ARCHIVE_DIR_NAME / archived.archive_file, stat.S_IWRITE | stat.S_IREAD)

    def test_the_cli_archives_and_lists_all(self, a) -> None:
        a.remove()
        admin = TenantAdmin(registry=a.reg, halt=a.w.platform.halt, owner_user_id=OWNER, clock=a.w.clock)

        def cli(*args):
            out = io.StringIO()
            code = main(["tenants", *args], out=out, tenants_factory=lambda: (admin, a.w.platform),
                        now=lambda: NOW + ARCHIVE_GRACE + timedelta(seconds=1))
            return code, out.getvalue()

        assert cli("archive", "--owner-id", "mallory")[0] == 3 and a.runtime.db_path.exists()
        code, text = cli("archive", "--owner-id", OWNER)
        assert code == 0 and "Archived 1" in text and not a.runtime.db_path.exists()
        assert a.tenant.id not in cli("list")[1] and "archived" in cli("list", "--all")[1]
        os.chmod(a.w.platform.tenant_dir / ARCHIVE_DIR_NAME / a.reg.get(a.tenant.id).archive_file, stat.S_IWRITE | stat.S_IREAD)


class TestHardening:
    def test_a_tenant_database_can_never_be_the_owners_database(self, tmp_path) -> None:
        w = World(tmp_path)
        t = w.wearer("Alex", "2000002")
        with w.platform.core.transaction() as tx:                                           # a corrupted registry row
            tx.execute("UPDATE tenants SET db_file = ? WHERE id = ?", (w.main_path.name, t.id))
        from system.tenant_factory import TenantFactory
        factory = TenantFactory(registry=w.platform.registry, shared=w.shared, tenant_dir=w.main_path.parent)
        with pytest.raises(TenantError):
            factory.build(t.id)

    def test_the_gateway_refuses_a_runtime_of_another_tenant(self, tmp_path) -> None:
        g = G(tmp_path)
        a_t, b_t = g.member("2000002", "Alex"), g.member("3000003", "Bo")
        g.supervisor.runtimes[a_t.id] = g.supervisor.runtimes[b_t.id]                        # a wiring bug, simulated
        reply = g.say("2000002", "hello")
        assert reply.log_db is None and "cannot reach" in reply.text


class TestIsolationAudit:
    """End to end: after real use by two tenants, no identifying value of one appears in ANY table of the other, nor in the owner's database."""

    @staticmethod
    def _all_text(path: Path) -> str:
        conn = sqlite3.connect(path)
        try:
            chunks = []
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                    chunks.extend(str(v) for v in row if v is not None)
            return "\n".join(chunks)
        finally:
            conn.close()

    def test_no_value_of_one_tenant_ever_appears_in_another(self, tmp_path) -> None:
        c = ChatWorld(tmp_path)
        prompt_split = {}
        for runtime, discord, account, secret in ((c.a, "2000002", "acc-a", "ZEBRA-ALEX"), (c.b, "3000003", "acc-b", "OKAPI-BO")):
            prompt_split[discord] = len(c.w.model.prompts)
            for text in ("first", "english", "neutral", "alex", f"my secret is {secret}"):
                c.w.say(runtime, discord, text)
            c.opt_in(runtime, account)
            runtime.chat_poller.poll_once()
        c.conv.pages = {"conv-acc-a": [msg("msg-alex-1", "acc-a", "chat ZEBRA-ALEX"), msg("msg-intrusion-9", "acc-b", "OKAPI-BO intrusion")],
                        "conv-acc-b": [msg("msg-bo-1", "acc-b", "chat OKAPI-BO")]}
        b_chat_prompts_from = len(c.w.model.prompts)
        for runtime in (c.a, c.b):
            for m in runtime.chat_poller.poll_once():
                run(runtime.chat_adapter.handle_incoming_message(m))
            runtime.cycle()
        text_a, text_b, text_main = self._all_text(c.a.db_path), self._all_text(c.b.db_path), self._all_text(c.w.main_path)
        # Persisted data really is there, each in its own tenant ...
        assert "2000002" in text_a and "conv-acc-a" in text_a and "msg-alex-1" in text_a
        assert "3000003" in text_b and "conv-acc-b" in text_b and "msg-bo-1" in text_b
        # ... and never in the other tenant or in the owner's database.
        for marker in ("2000002", "conv-acc-a", "msg-alex-1", "msg-intrusion-9", "ZEBRA-ALEX", "lock-a"):
            assert marker not in text_b and marker not in text_main, marker
        for marker in ("3000003", "conv-acc-b", "msg-bo-1", "OKAPI-BO", "lock-b"):
            assert marker not in text_a and marker not in text_main, marker
        # In-memory conversation state: Bo's model calls never contain Alex's words, and the stranger's post in Alex's chat reached no model.
        bo_prompts = c.w.model.prompts[prompt_split["3000003"]:b_chat_prompts_from]
        assert bo_prompts and not any("ZEBRA-ALEX" in p for p in bo_prompts)
        assert not any("OKAPI-BO intrusion" in p for p in c.w.model.prompts)
        assert any("chat ZEBRA-ALEX" in p for p in c.w.model.prompts[b_chat_prompts_from:])            # Alex's own chat did reach the model
        assert c.a.application_service._conversation_engine is not c.b.application_service._conversation_engine
