"""The owner's guarded Trust domain creation (TI1: consent first) and the `trust-domain` CLI. No network."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cli.main import main
from infrastructure.database import Database as CoreDatabase
from trust_manager.domain_admin import DomainAdminOutcome as O
from trust_manager.domain_admin import TrustDomainAdmin, domain_explanation
from trust_manager.repository import TrustManager

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
OWNER = "owner-1"


@pytest.fixture
def admin(tmp_path):
    core = CoreDatabase(tmp_path / "d.db")
    with core.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return TrustDomainAdmin(core.db_path, core=core, owner_user_id=OWNER)


def _rows(admin, sql):
    with admin._core.transaction() as tx:
        return [dict(r) for r in tx.fetch_all(sql)]


def test_create_records_consent_first_and_starts_at_the_defaults(admin) -> None:
    assert admin.create(actor=OWNER, domain_id="routine", display_name="Routine", description="Daily routine", confirmed=True,
                        now=NOW) is O.CREATED
    consent, = _rows(admin, "SELECT * FROM consent_log")
    domain, = _rows(admin, "SELECT * FROM trust_domains")
    assert (consent["target_type"], consent["target_id"], consent["action"]) == ("trust_algorithm", "trust_domain:routine", "approved")
    assert consent["explanation_shown"] == domain_explanation("routine", "Routine", "Daily routine")
    assert domain["created_via_consent_id"] == consent["id"]
    state = TrustManager(admin._core.db_path, core=admin._core).get_domain_state("routine")
    assert (state.score, state.confidence) == (0.6, 0.15)
    assert admin.create(actor=OWNER, domain_id="routine", display_name="R", description="d", confirmed=True, now=NOW) is O.ALREADY_EXISTS


@pytest.mark.parametrize("kw, outcome", [({"actor": "x"}, O.NOT_OWNER), ({"confirmed": False}, O.NOT_CONFIRMED),
                                         ({"domain_id": "Bad Id"}, O.INVALID_INPUT), ({"domain_id": "r"}, O.INVALID_INPUT),
                                         ({"display_name": " "}, O.INVALID_INPUT), ({"description": "x" * 201}, O.INVALID_INPUT)])
def test_refusals_write_nothing(admin, kw, outcome) -> None:
    args = {"actor": OWNER, "domain_id": "routine", "display_name": "Routine", "description": "d", "confirmed": True, "now": NOW, **kw}
    assert admin.create(**args) is outcome
    assert _rows(admin, "SELECT * FROM consent_log") == [] and _rows(admin, "SELECT * FROM trust_domains") == []


def test_an_owner_must_be_configured(admin) -> None:
    unowned = TrustDomainAdmin(admin._core.db_path, core=admin._core, owner_user_id=None)
    assert unowned.create(actor=OWNER, domain_id="routine", display_name="R", description="d", confirmed=True,
                          now=NOW) is O.OWNER_NOT_CONFIGURED


def test_cli(admin) -> None:
    def run(*args):
        out = io.StringIO()
        return main(["trust-domain", *args], out=out, domain_factory=lambda: admin, now=lambda: NOW), out.getvalue()

    assert run("list", "--owner-id", OWNER) == (0, "No Trust domains.\n")
    code, text = run("create", "routine", "--name", "Routine", "--description", "Daily routine", "--owner-id", OWNER)
    assert code == 3 and "add --yes" in text and "Create the Trust domain 'routine'" in text
    code, text = run("create", "routine", "--name", "Routine", "--description", "Daily routine", "--owner-id", OWNER, "--yes")
    assert code == 0 and "[outcome=created]" in text
    code, text = run("list", "--owner-id", OWNER)
    assert code == 0 and text.startswith("routine  Routine  active yes  score 0.6  confidence 0.15")
    assert run("list", "--owner-id", "x")[0] == 3


def test_every_outcome_has_a_text() -> None:
    from cli.main import _DOMAIN_TEXT
    assert set(_DOMAIN_TEXT) == {o.value for o in O}


def test_only_the_cli_creates_domains_with_consent() -> None:
    for name in ("system/composition.py", "bot/discord_bot.py", "system/tenant_factory.py", "application/service.py"):
        assert "domain_admin" not in (ROOT / name).read_text(encoding="utf-8")
