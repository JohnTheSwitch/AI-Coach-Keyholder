"""v0.39 Phase E1: platform database, tenant registry, router, global halt, owner admin and CLI. No network, no Discord."""

from __future__ import annotations

import ast
import hashlib
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cli.main import main
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from tenancy.admin import AdminOutcome, TenantAdmin
from tenancy.halt import CombinedHalt, PlatformHalt
from tenancy.models import DEFAULT_TENANT_CAP, TENANT_ZERO_ID, RemovedBy, TenantStatus
from tenancy.platform_db import open_platform_db
from tenancy.registry import InvalidTenantInput, TenantCapReached, TenantError, TenantRegistry, resolve_db_path
from tenancy.router import RouteKind, TenantRouter
from tenancy.services import open_platform_services

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
OWNER = "owner-1"


def later(minutes):
    return NOW + timedelta(minutes=minutes)


@pytest.fixture
def platform(tmp_path):
    return open_platform_db(tmp_path / "platform.sqlite")


@pytest.fixture
def registry(platform):
    r = TenantRegistry(platform)
    r.bootstrap_tenant_zero(db_file="data/app.sqlite", discord_id="1000001", now=NOW)
    return r


class TestPlatformDatabase:
    def test_migration_is_idempotent_and_creates_only_platform_tables(self, tmp_path) -> None:
        open_platform_db(tmp_path / "p.sqlite")
        core = open_platform_db(tmp_path / "p.sqlite")
        with core.transaction() as tx:
            tables = {r["name"] for r in tx.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")}
            versions = [r["version"] for r in tx.fetch_all("SELECT version FROM schema_version")]
        assert {"tenants", "platform_halt", "tenant_events", "schema_version"} <= tables
        assert versions == [1, 2, 3, 4] and not any(t in tables for t in ("goals", "trust_domains", "chaster_connections", "token_ledger"))

    def test_the_database_enforces_status_and_removal_invariants(self, platform) -> None:
        for sql in (
            "INSERT INTO tenants (id, display_name, db_file, status, created_at, updated_at) VALUES ('a','n','f','bogus','x','x')",
            "INSERT INTO tenants (id, display_name, db_file, status, created_at, updated_at) VALUES ('a','n','f','removed','x','x')",
            "INSERT INTO tenants (id, display_name, db_file, status, is_tenant_zero, created_at, updated_at) VALUES ('a','n','f','removed',1,'x','x')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                with platform.transaction() as tx:
                    tx.execute(sql)


class TestRegistry:
    def test_tenant_zero_is_registered_unchanged_and_idempotently(self, platform) -> None:
        r = TenantRegistry(platform)
        t = r.bootstrap_tenant_zero(db_file="data/app.sqlite", now=NOW)
        assert (t.id, t.is_tenant_zero, t.status, t.db_file, t.discord_id) == (TENANT_ZERO_ID, True, TenantStatus.ACTIVE, "data/app.sqlite", None)
        again = r.bootstrap_tenant_zero(db_file="data/app.sqlite", discord_id="1000001", now=later(5))
        assert again.discord_id == "1000001" and len(r.list_tenants()) == 1
        assert r.bootstrap_tenant_zero(db_file="data/app.sqlite", discord_id="2000002", now=later(9)).discord_id == "1000001"

    def test_invite_accept_flow_and_consent_time(self, registry) -> None:
        t = registry.invite(display_name="Alex", discord_id="2000002", invited_by="owner", now=later(1))
        assert t.status is TenantStatus.INVITED and not t.is_managed and t.accepted_at is None
        assert t.db_file == f"{t.id}.sqlite" and t.id.startswith("tenant_") and t.id != TENANT_ZERO_ID
        accepted = registry.accept(discord_id="2000002", now=later(2))
        assert accepted.status is TenantStatus.ACTIVE and accepted.is_managed and accepted.accepted_at == later(2)

    def test_only_an_invited_person_can_accept(self, registry) -> None:
        assert registry.accept(discord_id="9999999", now=NOW) is None            # stranger
        assert registry.accept(discord_id="1000001", now=NOW) is None            # tenant zero is already active
        registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        assert registry.accept(discord_id="2000002", now=NOW) is not None
        assert registry.accept(discord_id="2000002", now=NOW) is None             # not twice

    def test_input_validation(self, registry) -> None:
        for kw in (dict(display_name="", discord_id="2000002"), dict(display_name="x" * 65, discord_id="2000002"),
                   dict(display_name="A", discord_id="abc"), dict(display_name="A", discord_id="12"), dict(display_name="A", discord_id="")):
            with pytest.raises(InvalidTenantInput):
                registry.invite(invited_by="o", now=NOW, **kw)

    def test_a_discord_id_belongs_to_one_live_tenant(self, registry) -> None:
        registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        with pytest.raises(TenantError):
            registry.invite(display_name="B", discord_id="2000002", invited_by="o", now=NOW)
        with pytest.raises(TenantError):
            registry.invite(display_name="Owner again", discord_id="1000001", invited_by="o", now=NOW)

    def test_the_cap_counts_non_removed_tenants_including_tenant_zero(self, registry) -> None:
        for i in range(DEFAULT_TENANT_CAP - 1):
            registry.invite(display_name=f"T{i}", discord_id=f"30000{i:02d}", invited_by="o", now=NOW)
        with pytest.raises(TenantCapReached):
            registry.invite(display_name="over", discord_id="4000001", invited_by="o", now=NOW)
        victim = registry.list_tenants()[1]
        assert registry.remove(victim.id, actor="owner", now=later(1))
        registry.invite(display_name="fits now", discord_id="4000001", invited_by="o", now=later(2))

    def test_a_custom_cap(self, platform) -> None:
        r = TenantRegistry(platform, cap=2)
        r.bootstrap_tenant_zero(db_file="a", now=NOW)
        r.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        with pytest.raises(TenantCapReached):
            r.invite(display_name="B", discord_id="2000003", invited_by="o", now=NOW)
        with pytest.raises(ValueError):
            TenantRegistry(platform, cap=0)

    def test_suspend_and_resume_switch_management_off_and_on(self, registry) -> None:
        t = registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        assert registry.suspend(t.id, actor="owner", now=NOW) is False             # only an active tenant can be suspended
        registry.accept(discord_id="2000002", now=NOW)
        assert registry.suspend(t.id, actor="owner", now=later(1)) and not registry.is_managed(t.id)
        assert t.id not in [x.id for x in registry.managed_tenants()]
        assert registry.resume(t.id, actor="owner", now=later(2)) and registry.is_managed(t.id)
        assert registry.resume(t.id, actor="owner", now=later(3)) is False

    def test_the_keyholder_removes_a_tenant_unilaterally(self, registry) -> None:
        t = registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        registry.accept(discord_id="2000002", now=NOW)
        assert registry.remove(t.id, actor="owner", now=later(1)) is True
        gone = registry.get(t.id)
        assert gone.status is TenantStatus.REMOVED and gone.removed_by is RemovedBy.KEYHOLDER and not gone.is_managed
        assert registry.remove(t.id, actor="owner", now=later(2)) is False
        assert registry.find_by_discord_id("2000002") is None and t.id not in [x.id for x in registry.managed_tenants()]

    def test_the_wearer_leaves_unilaterally_from_any_live_state(self, registry) -> None:
        for i, accept in enumerate((False, True)):
            discord = f"500000{i}"
            t = registry.invite(display_name=f"W{i}", discord_id=discord, invited_by="o", now=NOW)
            if accept:
                registry.accept(discord_id=discord, now=NOW)
                registry.suspend(t.id, actor="owner", now=NOW)
            left = registry.leave(discord_id=discord, now=later(1))
            assert left.status is TenantStatus.REMOVED and left.removed_by is RemovedBy.WEARER and not left.is_managed
        assert registry.leave(discord_id="5000000", now=later(2)) is None            # already gone

    def test_tenant_zero_cannot_be_removed_or_left(self, registry) -> None:
        assert registry.remove(TENANT_ZERO_ID, actor="owner", now=NOW) is False
        assert registry.leave(discord_id="1000001", now=NOW) is None
        assert registry.get(TENANT_ZERO_ID).status is TenantStatus.ACTIVE

    def test_a_removed_person_can_be_invited_again_as_a_fresh_tenant(self, registry) -> None:
        old = registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        registry.leave(discord_id="2000002", now=NOW)
        new = registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=later(5))
        assert new.id != old.id and new.db_file != old.db_file and registry.get(old.id).status is TenantStatus.REMOVED

    def test_pairing_a_lock_is_unique_and_validated(self, registry) -> None:
        a = registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        b = registry.invite(display_name="B", discord_id="2000003", invited_by="o", now=NOW)
        assert registry.pair_lock(a.id, lock_id="lock-a", chaster_account_id="acc-a", actor="owner", now=NOW)
        assert registry.get(a.id).lock_id == "lock-a" and registry.find_by_chaster_account("acc-a").id == a.id
        with pytest.raises(sqlite3.IntegrityError):
            registry.pair_lock(b.id, lock_id="lock-a", chaster_account_id="acc-b", actor="owner", now=NOW)
        for bad in ({"lock_id": "a b", "chaster_account_id": "x"}, {"lock_id": "x", "chaster_account_id": ""}):
            with pytest.raises(InvalidTenantInput):
                registry.pair_lock(b.id, actor="owner", now=NOW, **bad)
        registry.remove(a.id, actor="owner", now=NOW)
        assert registry.pair_lock(b.id, lock_id="lock-a", chaster_account_id="acc-b", actor="owner", now=NOW)   # freed by the removal

    def test_every_lifecycle_change_is_audited(self, registry) -> None:
        t = registry.invite(display_name="A", discord_id="2000002", invited_by="owner", now=later(1))
        registry.accept(discord_id="2000002", now=later(2))
        registry.suspend(t.id, actor="owner", now=later(3))
        registry.resume(t.id, actor="owner", now=later(4))
        registry.leave(discord_id="2000002", now=later(5))
        assert [e["event"] for e in reversed(registry.events(t.id))] == ["invited", "accepted", "suspended", "resumed", "left"]
        assert registry.events(t.id)[0]["actor"] == "wearer:2000002"

    def test_database_paths(self, registry, tmp_path) -> None:
        zero = registry.get(TENANT_ZERO_ID)
        assert resolve_db_path(zero, tmp_path / "tenants") == Path("data/app.sqlite")
        t = registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        assert resolve_db_path(t, tmp_path / "tenants") == tmp_path / "tenants" / t.db_file
        evil = type(t)(**{**t.__dict__, "db_file": "../outside.sqlite"})
        with pytest.raises(InvalidTenantInput):
            resolve_db_path(evil, tmp_path)


class TestRouter:
    def test_every_state_has_its_own_route(self, registry) -> None:
        router = TenantRouter(registry)
        assert router.route_discord("1000001").kind is RouteKind.ACTIVE and router.route_discord("1000001").tenant.id == TENANT_ZERO_ID
        t = registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        assert router.route_discord("2000002").kind is RouteKind.INVITED
        registry.accept(discord_id="2000002", now=NOW)
        assert router.route_discord("2000002").kind is RouteKind.ACTIVE
        registry.suspend(t.id, actor="owner", now=NOW)
        assert router.route_discord("2000002").kind is RouteKind.SUSPENDED
        registry.remove(t.id, actor="owner", now=NOW)
        assert router.route_discord("2000002").kind is RouteKind.UNKNOWN and router.route_discord("2000002").tenant is None

    def test_strangers_and_odd_ids_are_unknown(self, registry) -> None:
        router = TenantRouter(registry)
        for value in ("424242", "", "x' OR 1=1 --", 77):
            assert router.route_discord(value).kind is RouteKind.UNKNOWN

    def test_routing_by_chaster_account(self, registry) -> None:
        t = registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        registry.pair_lock(t.id, lock_id="L", chaster_account_id="acc", actor="o", now=NOW)
        assert TenantRouter(registry).route_chaster_account("acc").tenant.id == t.id
        assert TenantRouter(registry).route_chaster_account("other").kind is RouteKind.UNKNOWN


class TestHalt:
    def test_platform_halt_round_trip_and_audit(self, platform) -> None:
        halt = PlatformHalt(platform)
        assert halt.is_halted() is False
        halt.set_halted(True, reason="maintenance", actor="owner", now=NOW)
        assert halt.is_halted() is True and halt.reason() == "maintenance"
        halt.set_halted(False, reason="done", actor="owner", now=later(1))
        assert halt.is_halted() is False
        with platform.transaction() as tx:
            assert [r["event"] for r in tx.fetch_all("SELECT event FROM tenant_events ORDER BY rowid")] == ["platform_halt", "platform_resume"]
        with pytest.raises(ValueError):
            halt.set_halted(True, reason=" ", actor="o", now=NOW)

    def test_an_unreadable_halt_counts_as_halted(self, platform) -> None:
        with platform.transaction() as tx:
            tx.execute("DROP TABLE platform_halt")
        assert PlatformHalt(platform).is_halted() is True

    def test_combined_halt_is_either_one(self) -> None:
        class Switch:
            def __init__(self, value): self.value = value
            def is_halted(self): return self.value
        assert [CombinedHalt(Switch(a), Switch(b)).is_halted() for a, b in ((0, 0), (1, 0), (0, 1), (1, 1))] == [False, True, True, True]


class TestAdmin:
    def _admin(self, registry, platform, owner=OWNER):
        return TenantAdmin(registry=registry, halt=PlatformHalt(platform), owner_user_id=owner, clock=FrozenClock(NOW))

    def test_the_owner_can_run_the_whole_lifecycle(self, registry, platform) -> None:
        admin = self._admin(registry, platform)
        res = admin.invite(actor=OWNER, display_name="Alex", discord_id="2000002")
        assert res.done and res.tenant.status is TenantStatus.INVITED
        registry.accept(discord_id="2000002", now=NOW)
        assert admin.suspend(actor=OWNER, tenant_id=res.tenant.id).done
        assert admin.resume(actor=OWNER, tenant_id=res.tenant.id).done
        assert admin.remove(actor=OWNER, tenant_id=res.tenant.id, confirmed=True).done
        assert admin.halt_all(actor=OWNER, reason="stop").done and registry.get(TENANT_ZERO_ID) is not None
        assert admin.resume_all(actor=OWNER, reason="go").done

    @pytest.mark.parametrize("actor,owner,outcome", [("mallory", OWNER, AdminOutcome.NOT_OWNER), ("", OWNER, AdminOutcome.NOT_OWNER),
                                                      (OWNER.upper(), OWNER, AdminOutcome.NOT_OWNER), (OWNER, None, AdminOutcome.OWNER_NOT_CONFIGURED),
                                                      (OWNER, "  ", AdminOutcome.OWNER_NOT_CONFIGURED)])
    def test_every_mutation_is_owner_only_and_refusals_are_audited(self, registry, platform, actor, owner, outcome) -> None:
        admin = self._admin(registry, platform, owner)
        t = registry.invite(display_name="A", discord_id="2000002", invited_by="o", now=NOW)
        calls = [lambda: admin.invite(actor=actor, display_name="B", discord_id="2000003"),
                 lambda: admin.suspend(actor=actor, tenant_id=t.id), lambda: admin.resume(actor=actor, tenant_id=t.id),
                 lambda: admin.remove(actor=actor, tenant_id=t.id, confirmed=True), lambda: admin.halt_all(actor=actor, reason="x"),
                 lambda: admin.resume_all(actor=actor, reason="x")]
        assert [c().outcome for c in calls] == [outcome] * 6
        assert registry.get(t.id).status is TenantStatus.INVITED and PlatformHalt(platform).is_halted() is False
        assert len(registry.list_tenants()) == 2
        with platform.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM tenant_events WHERE event LIKE 'refused_%'")["n"] == 6

    def test_remove_needs_confirmation_and_known_tenant(self, registry, platform) -> None:
        admin = self._admin(registry, platform)
        t = admin.invite(actor=OWNER, display_name="A", discord_id="2000002").tenant
        assert admin.remove(actor=OWNER, tenant_id=t.id, confirmed=False).outcome is AdminOutcome.NOT_CONFIRMED
        assert admin.remove(actor=OWNER, tenant_id="nope", confirmed=True).outcome is AdminOutcome.NOT_FOUND
        assert admin.remove(actor=OWNER, tenant_id=TENANT_ZERO_ID, confirmed=True).outcome is AdminOutcome.NOT_FOUND

    def test_cap_duplicates_and_invalid_input_are_reported(self, platform) -> None:
        r = TenantRegistry(platform, cap=2)
        r.bootstrap_tenant_zero(db_file="a", discord_id="1000001", now=NOW)
        admin = TenantAdmin(registry=r, halt=PlatformHalt(platform), owner_user_id=OWNER, clock=FrozenClock(NOW))
        assert admin.invite(actor=OWNER, display_name="A", discord_id="1000001").outcome is AdminOutcome.REFUSED
        assert admin.invite(actor=OWNER, display_name="A", discord_id="zz").outcome is AdminOutcome.INVALID
        assert admin.invite(actor=OWNER, display_name="A", discord_id="2000002").done
        assert admin.invite(actor=OWNER, display_name="B", discord_id="2000003").outcome is AdminOutcome.CAP_REACHED


class TestServicesAndTenantZero:
    def _main_db(self, tmp_path):
        core = CoreDatabase(tmp_path / "app.sqlite")
        with core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES ('owner-1', ?, ?)", (NOW.isoformat(),) * 2)
            conn.execute("INSERT INTO user_channel_identities (id, user_account_id, channel, external_id, created_at) "
                         "VALUES ('i', 'owner-1', 'discord', '1000001', ?)", (NOW.isoformat(),))
            conn.commit()
        return core

    def _fingerprint(self, path):
        conn = sqlite3.connect(path)
        try:
            schema = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
            data = [(t, conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for (t, *_rest) in
                    conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()]
        finally:
            conn.close()
        return hashlib.sha256(repr((schema, data)).encode()).hexdigest()

    def test_the_existing_system_becomes_tenant_zero_without_being_touched(self, tmp_path) -> None:
        main = self._main_db(tmp_path)
        before = self._fingerprint(tmp_path / "app.sqlite")
        services = open_platform_services(platform_db_path=tmp_path / "platform.sqlite", main_db_path=tmp_path / "app.sqlite",
                                          main_core=main, owner_user_id="owner-1", clock=FrozenClock(NOW))
        zero = services.registry.get(TENANT_ZERO_ID)
        assert zero.discord_id == "1000001" and Path(zero.db_file) == tmp_path / "app.sqlite" and zero.is_managed
        assert services.router.route_discord("1000001").kind is RouteKind.ACTIVE
        assert self._fingerprint(tmp_path / "app.sqlite") == before                      # no schema or data change at all
        assert not (tmp_path / "platform.sqlite").samefile(tmp_path / "app.sqlite")

    def test_opening_twice_changes_nothing(self, tmp_path) -> None:
        main = self._main_db(tmp_path)
        kwargs = dict(platform_db_path=tmp_path / "platform.sqlite", main_db_path=tmp_path / "app.sqlite", main_core=main,
                      owner_user_id="owner-1", clock=FrozenClock(NOW))
        open_platform_services(**kwargs)
        again = open_platform_services(**kwargs)
        assert [t.id for t in again.registry.list_tenants()] == [TENANT_ZERO_ID]

    def test_without_an_owner_identity_tenant_zero_still_exists(self, tmp_path) -> None:
        main = self._main_db(tmp_path)
        services = open_platform_services(platform_db_path=tmp_path / "platform.sqlite", main_db_path=tmp_path / "app.sqlite",
                                          main_core=main, owner_user_id=None, clock=FrozenClock(NOW))
        assert services.registry.get(TENANT_ZERO_ID).discord_id is None

    def test_the_default_cap_and_flag(self) -> None:
        from core.config import Config
        f = Config.__dataclass_fields__
        assert f["enable_multi_wearer"].default is False and f["tenant_cap"].default == DEFAULT_TENANT_CAP == 5
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        assert "ENABLE_MULTI_WEARER=false" in text and "TENANT_CAP=5" in text

    def test_the_shared_keyholder_token_manager_is_held_once(self, tmp_path) -> None:
        main = self._main_db(tmp_path)
        services = open_platform_services(platform_db_path=tmp_path / "platform.sqlite", main_db_path=tmp_path / "app.sqlite",
                                          main_core=main, owner_user_id="owner-1", clock=FrozenClock(NOW))
        assert services.keyholder_token_manager is None
        marker = object()
        services.keyholder_token_manager = marker
        assert services.keyholder_token_manager is marker


class TestCli:
    def _run(self, registry, platform, *args, owner=OWNER):
        admin = TenantAdmin(registry=registry, halt=PlatformHalt(platform), owner_user_id=owner, clock=FrozenClock(NOW))
        services = type("S", (), {"registry": registry, "halt": PlatformHalt(platform)})()
        out = io.StringIO()
        code = main(["tenants", *args], out=out, tenants_factory=lambda: (admin, services))
        return code, out.getvalue()

    def test_list_is_read_only_and_shows_the_halt(self, registry, platform) -> None:
        code, text = self._run(registry, platform, "list")
        assert code == 0 and "tenant_0" in text and "Platform halt: clear" in text
        PlatformHalt(platform).set_halted(True, reason="x", actor="o", now=NOW)
        assert "HALTED" in self._run(registry, platform, "list")[1]

    def test_invite_suspend_resume_remove_through_the_cli(self, registry, platform) -> None:
        code, text = self._run(registry, platform, "invite", "--name", "Alex", "--discord-id", "2000002", "--owner-id", OWNER)
        assert code == 0 and "outcome=done" in text
        tid = registry.find_by_discord_id("2000002").id
        registry.accept(discord_id="2000002", now=NOW)
        assert self._run(registry, platform, "suspend", tid, "--owner-id", OWNER)[0] == 0
        assert self._run(registry, platform, "resume", tid, "--owner-id", OWNER)[0] == 0
        code, text = self._run(registry, platform, "remove", tid, "--owner-id", OWNER)
        assert code == 3 and "--yes" in text and registry.get(tid).status is TenantStatus.ACTIVE
        assert self._run(registry, platform, "remove", tid, "--yes", "--owner-id", OWNER)[0] == 0
        assert registry.get(tid).status is TenantStatus.REMOVED

    def test_halt_and_unhalt(self, registry, platform) -> None:
        assert self._run(registry, platform, "halt", "--reason", "stop", "--owner-id", OWNER)[0] == 0
        assert PlatformHalt(platform).is_halted() is True
        assert self._run(registry, platform, "unhalt", "--reason", "go", "--owner-id", OWNER)[0] == 0
        assert PlatformHalt(platform).is_halted() is False

    def test_a_wrong_owner_is_refused_with_exit_3(self, registry, platform) -> None:
        code, text = self._run(registry, platform, "halt", "--reason", "x", "--owner-id", "mallory")
        assert code == 3 and "not the owner" in text and PlatformHalt(platform).is_halted() is False

    def test_mutating_commands_require_an_owner_id(self, registry, platform) -> None:
        with pytest.raises(SystemExit):
            self._run(registry, platform, "halt", "--reason", "x")


class TestIsolation:
    def test_only_the_bot_the_cli_and_tests_import_tenancy(self) -> None:
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in {"tenancy", "tests", "cli", "bot", "system"} or rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                offenders += [f"{rel}: {n}" for n in names if n.split(".")[0] == "tenancy"]
        assert offenders == []

    def test_the_platform_layer_imports_no_domain_module(self) -> None:
        forbidden = {"trust_manager", "penalty_engine", "goal_management", "release_engine", "rules_engine", "conversation_engine",
                     "application", "memory_system", "chaster_execution"}
        for path in (ROOT / "tenancy").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                assert not [n for n in names if n.split(".")[0] in forbidden], (path.name, names)

    def test_the_main_database_migrations_are_unchanged_by_phase_e1(self) -> None:
        assert max(int(p.name.split("_")[0]) for p in (ROOT / "database" / "migrations").glob("*.sql")) == 46
