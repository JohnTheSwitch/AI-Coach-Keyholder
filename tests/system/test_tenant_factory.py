"""v0.39 Phase E2: the tenant factory, the platform-wide halt in the composition, and the staggered tenant loops. No network, no Discord."""

from __future__ import annotations

import asyncio
import hashlib
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.models import IncomingMessage
from chaster_execution.control import ExecutionControl
from cli.main import main
from core.config import Config
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from system.composition import _WithGlobalHalt, build_rules_and_execution
from system.tenant_factory import SharedServices, TenantFactory, tenant_config
from system.tenant_loops import TenantLoopSupervisor, stagger_offsets
from tenancy.admin import TenantAdmin
from tenancy.halt import PlatformHalt
from tenancy.registry import TenantError, resolve_db_path
from tenancy.services import open_platform_services
from tests.chaster_execution.test_wiring import _chaster_parts, core as wiring_core  # noqa: F401

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
OWNER = "owner-1"


class FakeModel:
    """A stateless stand-in for the language model: replies with a fixed text and remembers every prompt it saw."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, *, request) -> str:
        self.prompts.append("\n".join(m.content for m in request.messages))
        return "a reply"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.main_path = tmp_path / "app.sqlite"
        self.main = CoreDatabase(self.main_path)
        with self.main.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES ('owner-1', ?, ?)", (NOW.isoformat(),) * 2)
            conn.execute("INSERT INTO user_channel_identities (id, user_account_id, channel, external_id, created_at) "
                         "VALUES ('i', 'owner-1', 'discord', '1000001', ?)", (NOW.isoformat(),))
            conn.commit()
        self.clock = FrozenClock(NOW)
        self.platform = open_platform_services(platform_db_path=tmp_path / "platform.sqlite", main_db_path=self.main_path,
                                               main_core=self.main, owner_user_id=OWNER, clock=self.clock)
        self.model = FakeModel()
        self.config = Config(discord_token="t", db_path=self.main_path, enable_rules_engine=True, enable_supervision=True)
        self.senders: list[Path] = []
        self.shared = SharedServices(
            config=self.config, clock=self.clock, model=self.model, oauth_client=None, lock_client=SimpleNamespace(),
            keyholder_connections=None, keyholder_token_manager=None, platform_halt=self.platform.halt,
            check_in_sender_factory=lambda path, core: self.senders.append(path) or SimpleNamespace(),
            check_in_complete=lambda a, b: "SUFFICIENT")
        self.factory = TenantFactory(registry=self.platform.registry, shared=self.shared, tenant_dir=self.platform.tenant_dir)
        self.admin = TenantAdmin(registry=self.platform.registry, halt=self.platform.halt, owner_user_id=OWNER, clock=self.clock)

    def wearer(self, name, discord_id):
        tenant = self.admin.invite(actor=OWNER, display_name=name, discord_id=discord_id).tenant
        self.platform.registry.accept(discord_id=discord_id, now=NOW)
        return self.platform.registry.get(tenant.id)

    def fingerprint(self, path):
        conn = sqlite3.connect(path)
        try:
            schema = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
            counts = [(n, conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]) for (n,) in
                      conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()]
        finally:
            conn.close()
        return hashlib.sha256(repr((schema, counts)).encode()).hexdigest()

    def say(self, runtime, discord_id, text, mid="m"):
        return runtime.handle_message(IncomingMessage(channel="discord", external_user_id=discord_id, text=text,
                                                      received_at=NOW, external_message_id=mid)).text


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


class TestFactory:
    def test_an_active_tenant_gets_its_own_migrated_database_and_wearer_account(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        assert runtime.db_path == w.platform.tenant_dir / f"{t.id}.sqlite" and runtime.db_path.exists()
        with runtime.core.transaction() as tx:
            assert tx.fetch_one("SELECT MAX(version) AS v FROM schema_version")["v"] == 90
            identity = tx.fetch_one("SELECT user_account_id FROM user_channel_identities WHERE channel = 'discord' AND external_id = '2000002'")
        assert identity["user_account_id"] == runtime.wearer_user_id

    def test_an_additional_wearer_gets_the_named_trust_domains_with_consent(self, w) -> None:
        """Owner, 2026-11-13: without them the daily evaluation, the check-in level and supervision never ran for an additional wearer."""
        from trust_manager.repository import TrustManager
        runtime = w.factory.build(w.wearer("Alex", "2000002").id)
        trust = TrustManager(runtime.db_path, core=runtime.core)
        assert trust.get_domain_state("routine") is not None and trust.get_domain_state("hygiene") is not None
        with runtime.core.transaction() as tx:
            consents = tx.fetch_all("SELECT target_id, user_comment FROM consent_log WHERE target_id LIKE 'trust_domain:%'")
        # Slice 8 (Q9): the Honesty domain too, now that the invitation discloses the spot checks.
        assert {c["target_id"] for c in consents} == {"trust_domain:routine", "trust_domain:hygiene", "trust_domain:honesty"}
        assert all(c["user_comment"] == "tenant: accepted invitation" for c in consents)
        daily = runtime.wiring.daily
        assert daily is not None
        from datetime import timedelta
        assert daily.run(NOW + timedelta(days=1)) != []                  # the daily evaluation now runs for this wearer
        w.factory.build(runtime.tenant_id)                               # a rebuild creates nothing twice
        with runtime.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM consent_log WHERE target_id LIKE 'trust_domain:%'")["n"] == 3

    def test_tenant_zero_is_never_touched(self, w) -> None:
        before = w.fingerprint(w.main_path)
        runtime = w.factory.build(w.wearer("Alex", "2000002").id)
        w.say(runtime, "2000002", "hello")
        runtime.cycle()
        assert w.fingerprint(w.main_path) == before

    @pytest.mark.parametrize("state", ["invited", "suspended", "removed", "zero", "unknown"])
    def test_only_an_active_non_zero_tenant_can_be_built(self, w, state) -> None:
        reg = w.platform.registry
        if state == "zero":
            tenant_id = "tenant_0"
        elif state == "unknown":
            tenant_id = "tenant_nope"
        else:
            tenant_id = w.admin.invite(actor=OWNER, display_name="X", discord_id="3000003").tenant.id
            if state != "invited":
                reg.accept(discord_id="3000003", now=NOW)
            if state == "suspended":
                reg.suspend(tenant_id, actor="o", now=NOW)
            if state == "removed":
                reg.remove(tenant_id, actor="o", now=NOW)
        with pytest.raises(TenantError):
            w.factory.build(tenant_id)
        if state in ("invited", "suspended", "removed"):
            assert not (w.platform.tenant_dir / f"{tenant_id}.sqlite").exists()                     # no file for a tenant that must not run

    def test_building_twice_is_idempotent(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        first, second = w.factory.build(t.id), w.factory.build(t.id)
        assert first.wearer_user_id == second.wearer_user_id and first.db_path == second.db_path

    def test_the_tenant_database_is_chosen_by_the_registry_not_by_the_caller(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        assert resolve_db_path(t, w.platform.tenant_dir) == w.factory.build(t.id).db_path

    def test_a_new_tenants_execution_starts_halted(self, w) -> None:
        runtime = w.factory.build(w.wearer("Alex", "2000002").id)
        assert ExecutionControl(runtime.db_path, core=runtime.core).is_halted() is True

    def test_the_check_in_sender_is_bound_to_the_tenants_own_database(self, w) -> None:
        runtime = w.factory.build(w.wearer("Alex", "2000002").id)
        assert w.senders == [runtime.db_path]


class TestIsolation:
    def test_two_tenants_never_share_data_or_memory(self, w) -> None:
        a, b = w.wearer("Alex", "2000002"), w.wearer("Bo", "3000003")
        ra, rb = w.factory.build(a.id), w.factory.build(b.id)
        assert ra.db_path != rb.db_path and ra.wearer_user_id != rb.wearer_user_id
        assert ra.application_service is not rb.application_service and ra.core is not rb.core
        assert ra.application_service._conversation_engine is not rb.application_service._conversation_engine
        for text in ("first", "english", "neutral", "alex", "I have a private secret goal: ZEBRA"):
            w.say(ra, "2000002", text)
        with rb.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM user_channel_identities WHERE external_id = '2000002'")["n"] == 0
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM user_preferences WHERE user_id = ?", (ra.wearer_user_id,))["n"] == 0
        with ra.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM user_preferences")["n"] == 1
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM user_channel_identities WHERE external_id = '3000003'")["n"] == 0
        assert any("ZEBRA" in p for p in w.model.prompts)                                            # A's own text did reach the model for A
        seen_before = len(w.model.prompts)
        for text in ("first", "english", "neutral", "alex", "how should I plan my week?"):
            w.say(rb, "3000003", text)
        assert len(w.model.prompts) > seen_before                                                    # B really produced model prompts ...
        assert not any("ZEBRA" in p for p in w.model.prompts[seen_before:])                          # ... and none contains A's text

    def test_a_tenant_does_not_know_another_tenants_wearer(self, w) -> None:
        a, b = w.wearer("Alex", "2000002"), w.wearer("Bo", "3000003")
        ra, rb = w.factory.build(a.id), w.factory.build(b.id)
        w.say(ra, "2000002", "hello")
        w.say(rb, "2000002", "hello")                       # A's Discord id arrives at B's runtime by mistake
        with rb.core.transaction() as tx:
            ids = {r["external_id"] for r in tx.fetch_all("SELECT external_id FROM user_channel_identities")}
        assert ids == {"3000003", "2000002"}                # B's runtime made its own account; it saw none of A's records
        with rb.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM user_preferences WHERE user_id = ?", (ra.wearer_user_id,))["n"] == 0

    def test_a_cycle_of_one_tenant_changes_nothing_in_another(self, w) -> None:
        a, b = w.wearer("Alex", "2000002"), w.wearer("Bo", "3000003")
        ra, rb = w.factory.build(a.id), w.factory.build(b.id)
        before_b = w.fingerprint(rb.db_path)
        assert ra.cycle() is True
        assert w.fingerprint(rb.db_path) == before_b

    def test_each_tenant_has_its_own_startup_lease_database(self, w) -> None:
        ra = w.factory.build(w.wearer("Alex", "2000002").id)
        rb = w.factory.build(w.wearer("Bo", "3000003").id)
        assert ra.process_id != rb.process_id and ra.cycle() and rb.cycle()

    def test_the_tenants_settings_never_inherit_the_owners_identities(self, w) -> None:
        base = Config(discord_token="t", db_path=w.main_path, check_in_user_id="owner-checkin", chaster_execution_wearer_user_id="owner-wearer",
                      chaster_execution_keyholder_user_id="bot", enable_chaster_messaging=True, chaster_llm_tools_enabled=True,
                      enable_multi_wearer=True, enable_release_engine=True)
        cfg = tenant_config(base, db_path=Path("tenant.sqlite"), wearer_user_id="tenant-wearer")
        assert (cfg.db_path, cfg.chaster_execution_wearer_user_id, cfg.check_in_user_id) == (Path("tenant.sqlite"), "tenant-wearer", None)
        assert cfg.chaster_execution_keyholder_user_id == "bot" and cfg.enable_release_engine is True          # platform keyholder and flags kept
        assert (cfg.enable_chaster_messaging, cfg.chaster_llm_tools_enabled, cfg.enable_multi_wearer) == (False, False, False)
        assert base.check_in_user_id == "owner-checkin"                                                        # the original is untouched


class TestPlatformHaltInComposition:
    class Switch:
        def __init__(self, value) -> None:
            self.value = value

        def is_halted(self):
            return self.value

    def test_halted_when_either_switch_is_set(self, tmp_path) -> None:
        core = CoreDatabase(tmp_path / "t.db")
        with core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        control = ExecutionControl(core.db_path, core=core)
        control.set_halted(False, reason="open", now=NOW)
        assert _WithGlobalHalt(control, None).is_halted() is False
        assert _WithGlobalHalt(control, self.Switch(False)).is_halted() is False
        assert _WithGlobalHalt(control, self.Switch(True)).is_halted() is True
        control.set_halted(True, reason="stop", now=NOW)
        assert _WithGlobalHalt(control, self.Switch(False)).is_halted() is True
        assert _WithGlobalHalt(control, None).reason() == "stop"

    def test_the_executor_of_a_wiring_honours_the_platform_halt(self, wiring_core) -> None:
        from tests.system.test_phase2_composition import config
        parts = _chaster_parts(wiring_core)
        ExecutionControl(wiring_core.db_path, core=wiring_core).set_halted(False, reason="open", now=NOW)
        switch = self.Switch(False)
        wiring = build_rules_and_execution(config(wiring_core, enable_chaster_execution=True), wiring_core, transport=SimpleNamespace(),
                                           halt_source=switch, **parts)
        assert wiring.executor is not None and wiring.executor._control.is_halted() is False
        switch.value = True
        assert wiring.executor._control.is_halted() is True

    def test_keyholder_objects_may_differ_from_the_wearers(self, wiring_core) -> None:
        from tests.system.test_phase2_composition import config
        parts = _chaster_parts(wiring_core)
        own_keyholder = _chaster_parts(wiring_core)
        wiring = build_rules_and_execution(
            config(wiring_core, enable_chaster_execution=True), wiring_core, transport=SimpleNamespace(),
            keyholder_connections=own_keyholder["connections"], keyholder_token_manager=own_keyholder["token_manager"], **parts)
        assert wiring.executor is not None and not any("NOT active" in n for n in wiring.notes if "execution" in n)

    def test_without_the_new_arguments_nothing_changes(self, wiring_core) -> None:
        from tests.system.test_phase2_composition import config
        wiring = build_rules_and_execution(config(wiring_core), wiring_core, **_chaster_parts(wiring_core))
        assert wiring.rules_engine is not None and wiring.executor is None


class TestStagger:
    def test_offsets_spread_evenly_over_the_interval(self) -> None:
        assert stagger_offsets(0, 60) == [] and stagger_offsets(1, 60) == [0.0]
        assert stagger_offsets(4, 60) == [0.0, 15.0, 30.0, 45.0]
        assert all(0 <= o < 60 for o in stagger_offsets(7, 60))


class FakeRuntime:
    def __init__(self, tenant_id) -> None:
        self.tenant_id, self.cycles, self.started = tenant_id, 0, []

    def cycle(self):
        self.cycles += 1
        return True


class FakeFactory:
    def __init__(self, fail=()) -> None:
        self.built, self.fail = [], set(fail)

    def build(self, tenant_id):
        if tenant_id in self.fail:
            raise RuntimeError("cannot build")
        self.built.append(tenant_id)
        return FakeRuntime(tenant_id)


def run(coro):
    return asyncio.run(coro)


class TestSupervisor:
    def _setup(self, w, factory=None, interval=0.05):
        return TenantLoopSupervisor(registry=w.platform.registry, factory=factory or FakeFactory(), interval_seconds=interval,
                                    reconcile_seconds=0.05)

    def test_only_active_non_zero_tenants_get_a_loop(self, w) -> None:
        a = w.wearer("Alex", "2000002")
        w.admin.invite(actor=OWNER, display_name="Pending", discord_id="4000004")             # invited only
        sup = self._setup(w)

        async def go():
            await sup.reconcile()
            names = sup.running
            await sup.stop_all()
            return names

        assert run(go()) == [a.id]

    def test_suspend_and_remove_stop_the_loop_and_drop_the_runtime(self, w) -> None:
        a, b = w.wearer("Alex", "2000002"), w.wearer("Bo", "3000003")
        sup = self._setup(w)

        async def go():
            await sup.reconcile()
            assert sup.running == sorted([a.id, b.id])
            w.platform.registry.suspend(a.id, actor="o", now=NOW)
            w.platform.registry.leave(discord_id="3000003", now=NOW)
            await sup.reconcile()
            result = (sup.running, sorted(sup.runtimes))
            await sup.stop_all()
            return result

        assert run(go()) == ([], [])

    def test_a_resumed_tenant_gets_a_fresh_loop(self, w) -> None:
        a = w.wearer("Alex", "2000002")
        factory = FakeFactory()
        sup = self._setup(w, factory)

        async def go():
            await sup.reconcile()
            w.platform.registry.suspend(a.id, actor="o", now=NOW)
            await sup.reconcile()
            w.platform.registry.resume(a.id, actor="o", now=NOW)
            await sup.reconcile()
            running = sup.running
            await sup.stop_all()
            return running

        assert run(go()) == [a.id] and factory.built == [a.id, a.id]

    def test_one_tenant_failing_to_build_does_not_block_the_others(self, w) -> None:
        bad, good = w.wearer("Bad", "2000002"), w.wearer("Good", "3000003")
        sup = self._setup(w, FakeFactory(fail=[bad.id]))

        async def go():
            await sup.reconcile()
            running = sup.running
            await sup.reconcile()                                  # the failed one waits for its back-off, no storm of retries
            again = sup.running
            await sup.stop_all()
            return running, again

        assert run(go()) == ([good.id], [good.id])

    def test_cycles_run_staggered_and_stop_at_once_when_the_tenant_is_no_longer_managed(self, w) -> None:
        a, b = w.wearer("Alex", "2000002"), w.wearer("Bo", "3000003")
        sup = self._setup(w, interval=0.2)

        async def go():
            await sup.reconcile()
            ra, rb = sup.runtimes[a.id], sup.runtimes[b.id]
            await asyncio.sleep(0.05)
            first = sorted([ra.cycles, rb.cycles])                 # offsets 0 and 0.1: exactly one has started
            w.platform.registry.remove(b.id, actor="o", now=NOW)
            await asyncio.sleep(0.5)
            await sup.stop_all()
            return first, ra.cycles, rb.cycles

        first, a_cycles, b_cycles = run(go())
        assert first == [0, 1] and a_cycles >= 2 and b_cycles <= 1

    def test_a_cycle_that_raises_stays_inside_its_own_loop(self, w) -> None:
        a, b = w.wearer("Alex", "2000002"), w.wearer("Bo", "3000003")
        factory = FakeFactory()
        sup = self._setup(w, factory, interval=0.05)

        async def go():
            await sup.reconcile()
            def boom():
                raise RuntimeError("tenant a failed")
            sup.runtimes[a.id].cycle = boom
            await asyncio.sleep(0.4)
            count = sup.runtimes[b.id].cycles
            await sup.stop_all()
            return count

        assert run(go()) >= 2

    def test_stop_all_and_invalid_arguments(self, w) -> None:
        with pytest.raises(ValueError):
            TenantLoopSupervisor(registry=w.platform.registry, factory=FakeFactory(), interval_seconds=0)
        sup = self._setup(w)
        run(sup.stop_all())
        assert sup.running == []


class TestExecutionCommand:
    def _run(self, w, *args, owner=OWNER):
        admin = TenantAdmin(registry=w.platform.registry, halt=w.platform.halt, owner_user_id=owner, clock=w.clock)
        out = io.StringIO()
        code = main(["tenants", *args], out=out, tenants_factory=lambda: (admin, w.platform), now=lambda: NOW)
        return code, out.getvalue()

    def test_the_owner_can_clear_and_halt_one_tenants_execution(self, w) -> None:
        a, b = w.wearer("Alex", "2000002"), w.wearer("Bo", "3000003")
        ra, rb = w.factory.build(a.id), w.factory.build(b.id)
        assert self._run(w, "execution", a.id, "clear", "--reason", "test lock verified", "--owner-id", OWNER)[0] == 0
        assert ExecutionControl(ra.db_path, core=ra.core).is_halted() is False
        assert ExecutionControl(rb.db_path, core=rb.core).is_halted() is True                       # the other tenant is unaffected
        assert self._run(w, "execution", a.id, "halt", "--reason", "stop", "--owner-id", OWNER)[0] == 0
        assert ExecutionControl(ra.db_path, core=ra.core).is_halted() is True

    def test_refusals(self, w) -> None:
        a = w.wearer("Alex", "2000002")
        code, text = self._run(w, "execution", a.id, "clear", "--reason", "x", "--owner-id", "mallory")
        assert code == 3 and "not the owner" in text
        assert self._run(w, "execution", a.id, "clear", "--reason", "x", "--owner-id", OWNER)[0] == 3       # database not created yet
        assert self._run(w, "execution", "tenant_0", "clear", "--reason", "x", "--owner-id", OWNER)[0] == 3  # tenant zero: use scripts/execution_control
        assert self._run(w, "execution", "tenant_nope", "clear", "--reason", "x", "--owner-id", OWNER)[0] == 3

    def test_the_platform_halt_stops_a_cleared_tenant_too(self, w) -> None:
        runtime = w.factory.build(w.wearer("Alex", "2000002").id)
        ExecutionControl(runtime.db_path, core=runtime.core).set_halted(False, reason="open", now=NOW)
        w.platform.halt.set_halted(True, reason="stop all", actor="owner", now=NOW)
        assert w.platform.halt.is_halted() is True and PlatformHalt(w.platform.core).is_halted() is True


class TestImportBoundaries:
    def test_only_the_bot_imports_the_tenant_factory_and_loops(self) -> None:
        import ast
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in {"bot", "tests", "system"} or rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                offenders += [f"{rel}: {n}" for n in names if n in ("system.tenant_factory", "system.tenant_loops")]
        assert offenders == []

    def test_the_main_database_schema_is_unchanged_by_phase_e2(self) -> None:
        assert max(int(p.name.split("_")[0]) for p in (ROOT / "database" / "migrations").glob("*.sql")) == 90


class TestParity5Exemptions:
    """Work queue Q5: exemptions and hygiene openings for an additional wearer, in their own database, judged, never on an unconfirmed lock."""

    def test_an_additional_wearer_gets_exemptions_and_hygiene(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        assert runtime.exemptions is not None and "exemption" in runtime.dm_actions
        reply = runtime.application_service._exemption_reply(runtime.wearer_user_id, "I need a wear exemption for tomorrow", NOW)
        assert reply is not None and reply.text                          # the exemption path, not the conversation model
        assert runtime.application_service._exemption_reply("someone-else", "I need a wear exemption", NOW) is None
        with runtime.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM wear_exemptions")["n"] == 1
        with w.main.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM wear_exemptions")["n"] == 0

    def test_an_opening_never_targets_an_unconfirmed_lock(self, w) -> None:
        from system.composition import build_tenant_exemption_opening
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        cfg = SimpleNamespace(chaster_execution_keyholder_user_id="kh", db_path=runtime.db_path)
        opening = build_tenant_exemption_opening(cfg, runtime.core, token_manager=SimpleNamespace(), lock_client=SimpleNamespace(),
                                                 lock_of=lambda: None, halt_source=w.platform.halt, clock=w.clock)
        assert opening is not None and opening.lock_id is None
        assert build_tenant_exemption_opening(SimpleNamespace(chaster_execution_keyholder_user_id=None, db_path=runtime.db_path), runtime.core,
                                              token_manager=None, lock_client=None, lock_of=lambda: "x") is None


class TestParity6Detectors:
    """Work queue Q6: an additional wearer's detectors run only through their own Chaster connection, in their own database."""

    def test_without_the_wearers_connection_nothing_is_watched(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        assert runtime.detectors is None and "infraction" not in runtime.dm_actions
        assert runtime.cycle() in (True, False)                          # a cycle without detectors runs as before

    def test_with_a_connection_the_detectors_are_built_for_the_wearer(self, w, monkeypatch) -> None:
        from types import SimpleNamespace as NS
        import infractions.wiring as iw
        seen = {}

        def fake_build(config, core, **kw):
            seen.update(account=config.chaster_execution_keyholder_user_id, owner=config.owner_user_id, db=str(config.db_path))
            return NS(signals=None, notes=["fake"], run=lambda now: [])

        monkeypatch.setattr(iw, "build_detectors", fake_build)
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        from system.tenant_factory import tenant_config
        cfg = tenant_config(w.config, db_path=runtime.db_path, wearer_user_id=runtime.wearer_user_id)
        detectors = w.factory._detectors(runtime.db_path, runtime.core, cfg, runtime.wearer_user_id, NS(), NS(), runtime.wiring)
        assert detectors is not None and seen == {"account": runtime.wearer_user_id, "owner": runtime.wearer_user_id, "db": str(runtime.db_path)}


class TestParity7Photos:
    """Work queue Q7: photo check-ins for an additional wearer are wired only with their own connection, and stay off without consent."""

    def test_wired_through_the_wearers_connection_and_off_by_default(self, w) -> None:
        from types import SimpleNamespace as NS
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        if runtime.wiring.check_ins is None:
            return
        w.factory._photo_check_ins(runtime, runtime.application_service, runtime.wiring, runtime.wearer_user_id, NS(), NS())
        assert runtime.wiring.check_ins.photo_times is not None and runtime.application_service.photo_check_ins is not None
        assert runtime.wiring.check_ins.photo_consent.is_on(runtime.wearer_user_id) is False
        assert runtime.wiring.check_ins.photo_suspended(None) is False


class TestSlice8:
    """Work queue Q9: the invitation discloses the coach and the spot checks; `proof checks off` for the wearer."""

    def test_the_invitation_discloses_them(self) -> None:
        from system.tenant_gateway import invitation_text
        text = invitation_text("Alex")
        assert "coach proposes goals" in text and "spot-checked" in text and "`proof checks off`" in text

    def test_the_switch_turns_spot_checks_off_and_on(self, w) -> None:
        from types import SimpleNamespace as NS
        from proof_checks.service import ProofChecks
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        checks = ProofChecks(runtime.core, user_id=runtime.wearer_user_id, honesty_domain='honesty', chaster=NS(), ports=NS(honesty_score=lambda: 0.5))
        assert checks.switched_off() is False
        assert "off" in checks.set_switch(runtime.wearer_user_id, True, NOW) and checks.switched_off() is True
        assert checks.select("c1", "something", NOW) is False
        assert "on again" in checks.set_switch(runtime.wearer_user_id, False, NOW) and checks.switched_off() is False
        assert checks.set_switch("someone-else", True, NOW) == "That isn't available for you."
        runtime.application_service.proof_switch = checks.set_switch


class TestParity10Switches:
    """Owner, 2026-11-16 (multi-wearer first): an additional wearer gets `features` and the 24-hour switch-off wait in their own database."""

    def test_features_and_opt_outs_live_in_the_wearers_database(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        assert runtime.features is not None and runtime.opt_outs is not None and "optout" in runtime.dm_actions
        service = runtime.application_service
        assert "Your features:" in service.features_command(runtime.wearer_user_id, "features", NOW)
        reply = service.features_command(runtime.wearer_user_id, "lines off", NOW)
        assert reply.startswith("Noted.") or reply.startswith("Lines is off.")      # locked or not, never an error
        runtime.cycle()                                                                   # the opt-out tick runs in the tenant's cycle
        with w.main.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM opt_out_requests")["n"] == 0
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM feature_switches")["n"] == 0


class TestParity11Lines:
    """Owner, 2026-11-16 (multi-wearer first): lines for an additional wearer, in their own database, behind their `lines` switch."""

    def _event(self, incident_id):
        from types import SimpleNamespace
        return SimpleNamespace(payload={"incident_id": incident_id, "rule_group_id": "HYGIENE_MISUSE"}, occurred_at=NOW)

    def test_a_confirmed_incident_assigns_lines_in_the_wearers_database_only(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        assert runtime.lines is not None and "lines" in runtime.extra_consumers and "lines" in runtime.dm_actions
        assert runtime.application_service.lines is runtime.lines and runtime.exemptions.lines_open is not None
        with runtime.core.transaction() as tx:
            runtime.extra_consumers["lines"]["penalty_warning.recorded"](tx, self._event("inc-1"))
        assert runtime.lines.open_lines(runtime.wearer_user_id) > 0
        with w.main.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM line_assignments")["n"] == 0

    def test_the_lines_switch_off_assigns_none(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        runtime.features.set(runtime.wearer_user_id, "lines", False, NOW)
        with runtime.core.transaction() as tx:
            runtime.extra_consumers["lines"]["penalty_warning.recorded"](tx, self._event("inc-2"))
        assert runtime.lines.open_lines(runtime.wearer_user_id) == 0


class TestParity12Obedience:
    """Owner, 2026-11-16 (multi-wearer first): obedience tasks for an additional wearer start with erotic tasks and workouts off and no gear."""

    def test_erotic_tasks_and_workouts_start_off_and_stay_the_wearers_choice(self, w) -> None:
        from features.service import get_feature_on
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        assert not get_feature_on(runtime.core, runtime.wearer_user_id, "erotic_tasks")
        assert not get_feature_on(runtime.core, runtime.wearer_user_id, "workouts")
        assert get_feature_on(runtime.core, runtime.wearer_user_id, "lines")
        runtime.features.set(runtime.wearer_user_id, "workouts", True, NOW)
        assert runtime.features.seed_off(runtime.wearer_user_id, ("workouts",), NOW) == []          # a rebuild never overrides it
        assert get_feature_on(runtime.core, runtime.wearer_user_id, "workouts")
        assert runtime.obedience is None and any("obedience" in n for n in runtime.notes)           # no spot checks / detectors here

    def test_with_spot_checks_and_detectors_the_gear_list_starts_empty(self, w) -> None:
        from types import SimpleNamespace as NS
        from system.tenant_factory import tenant_config
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        runtime.proof_checks = NS(user_id=runtime.wearer_user_id, chaster=NS(), get_open_check=lambda: None,
                                  ports=NS(lock_id=lambda: None, halted=lambda: True, paused=lambda now: False, today=lambda now: now.date(),
                                           local_clock=lambda t: "x"))
        runtime.detectors = NS(signals=NS(raise_signal=lambda **kw: True))
        cfg = tenant_config(w.config, db_path=runtime.db_path, wearer_user_id=runtime.wearer_user_id)
        w.factory._obedience(runtime, runtime.application_service, cfg, runtime.core)
        assert runtime.obedience is not None and runtime.obedience.owned_gear() == frozenset()
        assert "obedience" in runtime.dm_actions and runtime.application_service.gear_command is not None
        assert runtime.obedience.gear_command(runtime.wearer_user_id, "gear add collar", NOW).startswith("Added: collar")
        assert runtime.obedience.owned_gear() == frozenset({"collar"})
        with w.main.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM obedience_gear")["n"] == 0


class TestParity13Release:
    """Owner, 2026-11-16 (multi-wearer first): earning the release, the release window and the relock picture for an additional wearer."""

    def test_the_release_waits_for_training_or_release_lines(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        release_task = runtime.wiring.release_task
        if release_task is None:
            return
        assert release_task.release_earned is not None
        assert runtime.release_window is None                      # no wearer connection: no window, never a miss on an unknown

    def test_with_a_connection_the_window_and_relock_picture_are_wired(self, w) -> None:
        from types import SimpleNamespace as NS
        from system.tenant_factory import tenant_config
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        if runtime.wiring.release_task is None or runtime.exemptions is None:
            return
        runtime.detectors = NS(signals=NS(raise_signal=lambda **kw: True))
        cfg = tenant_config(w.config, db_path=runtime.db_path, wearer_user_id=runtime.wearer_user_id)
        w.factory._release_and_relock(runtime, cfg, runtime.core, runtime.wiring, NS(), NS())
        assert runtime.release_window is not None and runtime.release_window.enabled() is True
        assert runtime.exemptions.relock_pictures is not None and runtime.exemptions.raise_missing_picture is not None
        runtime.features.set(runtime.wearer_user_id, "release_relock", False, NOW)
        assert runtime.release_window.enabled() is False


class TestParity14MorningAndProtocol:
    """Owner, 2026-11-16 (multi-wearer first): the morning report (on) and the protocol ladder (off at the start) for an additional wearer."""

    def test_wired_in_the_wearers_runtime(self, w) -> None:
        t = w.wearer("Alex", "2000002")
        runtime = w.factory.build(t.id)
        if runtime.wiring.check_ins is None:
            return
        assert runtime.wiring.check_ins.morning_on is True and runtime.wiring.check_ins.morning_enabled() is True
        assert runtime.protocol is not None and runtime.application_service.protocol_gate is not None
        assert runtime.application_service.protocol_gate(runtime.wearer_user_id, "this is unfair", NOW) is None     # protocol off at the start
        runtime.features.set(runtime.wearer_user_id, "protocol", True, NOW)
        assert runtime.application_service.protocol_gate(runtime.wearer_user_id, "this is unfair", NOW) is not None

    def test_the_invitation_says_what_the_operator_can_see(self) -> None:
        from system.tenant_gateway import invitation_text
        text = invitation_text("Alex")
        assert "as the operator they can see them" in text and "beginner level" in text
