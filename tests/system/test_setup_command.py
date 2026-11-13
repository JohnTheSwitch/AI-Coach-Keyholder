"""Owner, 2026-11-13: `setup check` (read-only, never prints a secret) and `setup init` (the named Trust domains, with consent)."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from cli.main import run_setup
from cli.setup import named_trust_domains, render_report, setup_report
from infrastructure.database import Database as CoreDatabase
from trust_manager.domain_admin import TrustDomainAdmin

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
SECRET = "very-secret-value-1234567890"


def migrated(tmp_path) -> CoreDatabase:
    core = CoreDatabase(tmp_path / "t.db")
    with core.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return core


def config(tmp_path, **kw):
    base = dict(discord_token=SECRET, ollama_host="http://localhost:11434", ollama_model="llama3.1", db_path=tmp_path / "t.db",
                owner_user_id=None, chaster_client_id="cid", chaster_client_secret=SECRET, chaster_token_encryption_key=SECRET,
                chaster_oauth_scopes="locks", chaster_execution_keyholder_user_id=None, release_lock_id=None, penalty_freeze_lock_id=None,
                exemption_lock_id=None, owner_chaster_account_id=None, check_in_trust_domain="routine", daily_evaluation_trust_domain="routine",
                hygiene_trust_domain="hygiene", lock_claim_trust_domain="routine", peer_review_trust_domain="routine",
                app_timezone="UTC", holiday_country="")
    base.update(kw)
    return SimpleNamespace(**base)


class TestCheck:
    def test_it_never_prints_a_secret(self, tmp_path) -> None:
        text = render_report(setup_report(config(tmp_path), migrated(tmp_path), model_ready=lambda: SimpleNamespace(reachable=True, installed=True)))
        assert SECRET not in text and "Discord bot token" in text

    def test_it_names_what_is_missing(self, tmp_path) -> None:
        text = render_report(setup_report(config(tmp_path, release_lock_id="auto"), migrated(tmp_path),
                                          model_ready=lambda: SimpleNamespace(reachable=True, installed=False)))
        for needle in ("ollama pull llama3.1", "OWNER_USER_ID", "keyholder messaging offline_access", "OWNER_CHASTER_ACCOUNT_ID",
                       "Trust domain routine", "HALTED"):
            assert needle in text, needle

    def test_a_missing_database_is_reported_not_created(self, tmp_path) -> None:
        text = render_report(setup_report(config(tmp_path), None))
        assert "does not exist yet" in text and not (tmp_path / "t.db").exists()

    def test_a_bad_time_zone(self, tmp_path) -> None:
        assert "not a valid IANA name" in render_report(setup_report(config(tmp_path, app_timezone="Mars/Base"), migrated(tmp_path)))

    def test_the_named_domains(self, tmp_path) -> None:
        assert named_trust_domains(config(tmp_path)) == ["hygiene", "routine"]


class TestInit:
    def test_it_creates_the_missing_domains_with_consent_once(self, tmp_path) -> None:
        core = migrated(tmp_path)
        cfg = config(tmp_path, owner_user_id="owner")
        admin = TrustDomainAdmin(core.db_path, core=core, owner_user_id="owner")
        args = SimpleNamespace(setup_command="init", owner_id="owner", yes=True)
        out = io.StringIO()
        assert run_setup(args, out=out, now=lambda: NOW, factory=lambda: (cfg, core, None), domain_factory=lambda: admin) == 0
        assert {r["domain_id"] for r in admin.list_domains()} == {"routine", "hygiene"}
        out2 = io.StringIO()
        run_setup(args, out=out2, now=lambda: NOW, factory=lambda: (cfg, core, None), domain_factory=lambda: admin)
        assert out2.getvalue().count("already exists") == 2
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM consent_log WHERE target_id LIKE 'trust_domain:%'")["n"] == 2

    def test_owner_only_and_yes(self, tmp_path) -> None:
        core = migrated(tmp_path)
        admin = TrustDomainAdmin(core.db_path, core=core, owner_user_id="owner")
        for args in (SimpleNamespace(setup_command="init", owner_id="x", yes=True), SimpleNamespace(setup_command="init", owner_id="owner", yes=False)):
            assert run_setup(args, out=io.StringIO(), now=lambda: NOW, factory=lambda: (config(tmp_path), core, None), domain_factory=lambda: admin) == 3
        assert admin.list_domains() == []


class TestNeutralDefaults:
    def test_a_new_installation_is_not_in_prague(self) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["app_timezone"].default == "UTC" and Config.__dataclass_fields__["holiday_country"].default == ""
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        assert "APP_TIMEZONE=UTC" in example and "RELEASE_LOCK_ID=auto" in example and "ENABLE_UPDATE_CHECK=true" in example
        assert (ROOT / "SETUP.md").exists()
