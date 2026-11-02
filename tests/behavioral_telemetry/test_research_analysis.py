"""v0.39 Phase G3: the owner's offline analysis `python -m cli research analyze` — owner-only, the live-bot guard, the dense numeric summary, the local
model call with the analyst prompt, the hard-coded consent disclaimer, and the guarantee that the report is never stored in a database. No network."""

from __future__ import annotations

import io
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from behavioral_telemetry.analysis import ANALYST_PROMPT, DISCLAIMER, LIVE_WINDOW, bot_is_live, is_local_host, summarize
from behavioral_telemetry.store import record_in
from cli.main import main
from conversation_engine.model_types import LLMGenerationError
from infrastructure.database import Database as CoreDatabase
from infrastructure.startup_lease import acquire_system_startup_lease, release_system_startup_lease
from tenancy.admin import TenantAdmin
from tests.system.test_tenant_factory import NOW, OWNER, World

ROOT = Path(__file__).parent.parent.parent


class Model:
    def __init__(self, reply="Overview: steady. Recommendations: keep check-ins.", error=None) -> None:
        self.reply, self.error, self.requests = reply, error, []

    def generate(self, *, request) -> str:
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.reply


def fill(core: CoreDatabase, user: str, *, days=20) -> None:
    """A realistic month of derived records."""
    with core.transaction() as tx:
        n = 0
        for d in range(days):
            at = NOW - timedelta(days=d, hours=1)
            n += 1
            record_in(tx, user_id=user, occurred_at=at, event_type="daily_summary", source_key=f"d{d}", now=NOW, metrics={
                "local_date": at.date().isoformat(), "goals_assigned": 1, "goals_met": d % 3 != 0, "check_ins_due": 2, "check_ins_ok": 2 - (d % 4 == 0),
                "violations": 0, "clean": d % 3 != 0, "streak_after": d % 5, "tokens_earned": int(d % 4 == 0), "progress": 0.25, "pool_after": 0.5,
                "trust_used": 0.8, "training_inflation_factor": 1.0 + 0.025 * (d < 10), "operating_mode": "standard", "bot_mood": "neutral"})
            for k, status in enumerate(("answered", "missed" if d % 4 == 0 else "answered")):
                record_in(tx, user_id=user, occurred_at=at - timedelta(hours=k + 2), event_type="response_delay", source_key=f"c{d}-{k}", now=NOW,
                          metrics={"level": 2, "slot_index": k, "status": status, "verdict": None, "delay_seconds": None if status == "missed" else 300 + d,
                                   "window_seconds": 1800})
            for t in range(3):
                record_in(tx, user_id=user, occurred_at=at - timedelta(minutes=t * 30), event_type="llm_generation", source_key=f"g{d}-{t}", now=NOW,
                          metrics={"latency_ms": 9000 + t, "path": "structured", "outcome": "ok", "structured_status": "unparseable" if t == 2 else "ok",
                                   "trusted": True})
                record_in(tx, user_id=user, occurred_at=at - timedelta(minutes=t * 30), event_type="conversation_gap", source_key=f"gap{d}-{t}", now=NOW,
                          metrics={"seconds_since_last_reply": 600 * (t + 1)})
        record_in(tx, user_id=user, occurred_at=NOW - timedelta(days=9), event_type="inflation_resilience", source_key="i1", now=NOW, metrics={
            "outcome": "increased", "training_inflation_factor": 1.025, "factor_before": 1.0, "days_since_previous_increment": None,
            "progress_avg_7d": 0.25, "clean_days_7d": 5})
        record_in(tx, user_id=user, occurred_at=NOW - timedelta(days=4), event_type="token_burn", source_key="b1", now=NOW,
                  metrics={"amount": 1, "balance_after": 0, "hours_since_last_earn": 30.5})


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.runtime = w.factory.build(w.wearer("Alex", "2000002").id)
    w.admin = TenantAdmin(registry=w.platform.registry, halt=w.platform.halt, owner_user_id=OWNER, clock=w.clock)
    fill(w.runtime.core, w.runtime.wearer_user_id)
    w.model = Model()
    return w


def cli(world, *args, owner=OWNER, model=None):
    out = io.StringIO()
    factory_calls = []

    def factory(timeout):
        factory_calls.append(timeout)
        if isinstance(model, Exception):
            raise model
        return model or world.model

    code = main(["research", "analyze", *args, "--owner-id", owner], out=out, tenants_factory=lambda: (world.admin, world.platform),
                now=lambda: NOW, research_model_factory=factory)
    return code, out.getvalue(), factory_calls


def clear_leases(world):
    for core in (world.runtime.core, world.main):
        with core.transaction() as tx:
            tx.execute("UPDATE system_startup_lease SET held_by = NULL, acquired_at = NULL, expires_at = NULL")


class TestOwnerCommand:
    def test_only_the_owner_and_the_model_is_never_asked_otherwise(self, world) -> None:
        clear_leases(world)
        code, text, calls = cli(world, world.runtime.tenant_id, owner="mallory")
        assert code == 3 and "owner" in text.lower() and calls == [] and world.model.requests == []

    def test_the_report_carries_the_prompt_the_summary_and_the_disclaimer(self, world) -> None:
        clear_leases(world)
        code, text, calls = cli(world, world.runtime.tenant_id, "--days", "30", "--timeout", "900")
        assert code == 0 and calls == [900.0]
        (request,) = world.model.requests
        system, user = request.messages
        assert system.content == ANALYST_PROMPT and "Psychological Profile & Recommendations" in system.content and "consent" in system.content
        assert user.content.startswith("Numeric summary:\nPERIOD: last 30 days")
        assert "Recommendations: keep check-ins." in text and text.rstrip().endswith(DISCLAIMER)
        assert f"user {world.runtime.wearer_user_id[:8]}" in text

    def test_the_disclaimer_is_appended_whatever_the_model_says(self, world) -> None:
        clear_leases(world)
        _, text, _ = cli(world, world.runtime.tenant_id, model=Model(reply=""))
        assert text.rstrip().endswith(DISCLAIMER)
        assert DISCLAIMER == ("WARNING: Any recommended parameter changes are critical_changes and require explicit wearer consent before implementation.")

    @pytest.mark.parametrize("args,code,expected", [(("tenant_nope",), 3, "No such tenant"), (("{t}", "--days", "0"), 3, "--days"),
                                                    (("{t}", "--days", "181"), 3, "--days")])
    def test_invalid_input(self, world, args, code, expected) -> None:
        clear_leases(world)
        result = cli(world, *(a.format(t=world.runtime.tenant_id) for a in args))
        assert result[0] == code and expected in result[1] and world.model.requests == []

    def test_no_records_and_model_failures(self, world) -> None:
        clear_leases(world)
        with world.runtime.core.transaction() as tx:
            tx.execute("DELETE FROM behavioral_telemetry")
        code, text, calls = cli(world, world.runtime.tenant_id)
        assert code == 0 and "No behavioral telemetry" in text and calls == []
        fill(world.runtime.core, world.runtime.wearer_user_id)
        code, text, _ = cli(world, world.runtime.tenant_id, model=Model(error=LLMGenerationError("ollama_timeout")))
        assert code == 4 and "ollama_timeout" in text and DISCLAIMER not in text

    def test_a_remote_model_host_is_refused(self, world) -> None:
        clear_leases(world)
        code, text, _ = cli(world, world.runtime.tenant_id, model=ValueError("OLLAMA_HOST is not this machine"))
        assert code == 3 and "only uses a model on this machine" in text
        for url, local in (("http://localhost:11434", True), ("http://127.0.0.1:11434", True), ("http://[::1]:11434", True),
                           ("http://gpu.localhost:1", True), ("http://192.168.1.5:11434", False), ("https://ollama.example.com", False), ("::", False)):
            assert is_local_host(url) is local, url


class TestLiveGuard:
    def test_a_running_bot_blocks_the_analysis_unless_allowed(self, world) -> None:
        clear_leases(world)
        lease = acquire_system_startup_lease(world.main, "bot", NOW - timedelta(minutes=2), timedelta(minutes=2))
        release_system_startup_lease(world.main, lease)                  # a periodic cycle that has just finished
        code, text, calls = cli(world, world.runtime.tenant_id)
        assert code == 3 and "--allow-while-live" in text and calls == [] and world.model.requests == []
        assert cli(world, world.runtime.tenant_id, "--allow-while-live")[0] == 0

    def test_a_lease_held_now_in_the_tenant_database_blocks_too(self, world) -> None:
        clear_leases(world)
        acquire_system_startup_lease(world.runtime.core, "tenant-cycle", NOW - timedelta(hours=1), timedelta(hours=2))
        assert cli(world, world.runtime.tenant_id)[0] == 3

    def test_an_old_lease_does_not_block(self, world) -> None:
        clear_leases(world)
        lease = acquire_system_startup_lease(world.main, "bot", NOW - LIVE_WINDOW - timedelta(minutes=1), timedelta(seconds=30))
        release_system_startup_lease(world.main, lease)
        assert not bot_is_live([world.main_path, world.runtime.db_path], NOW) and cli(world, world.runtime.tenant_id)[0] == 0

    def test_release_keeps_the_last_acquisition_and_the_lease_can_be_taken_again(self, world) -> None:
        clear_leases(world)
        lease = acquire_system_startup_lease(world.main, "a", NOW, timedelta(minutes=2))
        release_system_startup_lease(world.main, lease)
        with world.main.transaction() as tx:
            row = tx.fetch_one("SELECT held_by, acquired_at, expires_at FROM system_startup_lease WHERE id = 1")
        assert row["held_by"] is None and row["expires_at"] is None and row["acquired_at"] is not None
        assert acquire_system_startup_lease(world.main, "b", NOW + timedelta(seconds=1), timedelta(minutes=2)) is not None


class TestSummary:
    def test_dense_numeric_and_no_raw_rows(self, world) -> None:
        (summary,) = summarize(world.runtime.db_path, days=30, now=NOW)
        text = summary.text
        assert summary.records > 150 and len(text) < 2500
        for section in ("PERIOD:", "DAILY (20 evaluated days)", "CHECK-INS (40 concluded)", "miss rate", "TOKENS: 1 spent", "CONDITIONING: factor 1.000 -> 1.025",
                        "CHAT: 60 model turns", "'unparseable': 20", "reply gap hours", "WEEKLY:"):
            assert section in text, section
        assert '{"' not in text and "source_key" not in text and world.runtime.wearer_user_id not in text

    def test_the_window_and_users_are_respected(self, world) -> None:
        with world.runtime.core.transaction() as tx:
            record_in(tx, user_id="second-user", occurred_at=NOW - timedelta(days=2), event_type="token_burn", source_key="x", now=NOW,
                      metrics={"amount": 1, "balance_after": 2, "hours_since_last_earn": 1.0})
        summaries = summarize(world.runtime.db_path, days=3, now=NOW)
        assert sorted(s.user_id for s in summaries) == sorted([world.runtime.wearer_user_id, "second-user"])
        assert "DAILY (3 evaluated days)" in next(s.text for s in summaries if s.user_id == world.runtime.wearer_user_id)

    def test_the_connection_is_read_only(self, world) -> None:
        from behavioral_telemetry.analysis import _connect_read_only
        conn = _connect_read_only(world.runtime.db_path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM behavioral_telemetry")
        finally:
            conn.close()


class TestNeverStored:
    def test_no_database_changes_and_a_plain_file_only(self, world, tmp_path) -> None:
        clear_leases(world)
        before = (world.fingerprint(world.runtime.db_path), world.fingerprint(world.main_path))
        target = tmp_path / "out" / "report.txt"
        target.parent.mkdir()
        code, text, _ = cli(world, world.runtime.tenant_id, "--output", str(target))
        assert code == 0 and "Report written" in text and target.read_text(encoding="utf-8").rstrip().endswith(DISCLAIMER)
        assert (world.fingerprint(world.runtime.db_path), world.fingerprint(world.main_path)) == before
        for core in (world.runtime.core, world.main):
            with core.transaction() as tx:
                assert tx.fetch_one("SELECT COUNT(*) AS n FROM ai_suggestions")["n"] == 0
                assert tx.fetch_one("SELECT COUNT(*) AS n FROM wearer_dossier")["n"] == 0

    @pytest.mark.parametrize("name", ["report.sqlite", "report.db", "x.sqlite3"])
    def test_database_files_are_refused_as_targets(self, world, tmp_path, name) -> None:
        clear_leases(world)
        code, text, _ = cli(world, world.runtime.tenant_id, "--output", str(tmp_path / name))
        assert code == 3 and not (tmp_path / name).exists() and world.model.requests == []

    def test_the_tenant_folder_is_refused_as_a_target(self, world) -> None:
        clear_leases(world)
        target = Path(world.platform.tenant_dir) / "report.txt"
        assert cli(world, world.runtime.tenant_id, "--output", str(target))[0] == 3 and not target.exists()

    def test_the_analysis_code_writes_nothing_and_feeds_nothing_back(self) -> None:
        text = (ROOT / "behavioral_telemetry" / "analysis.py").read_text(encoding="utf-8")
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", text) and "datetime.now" not in text
        for banned in ("coaching_memory", "idea_box", "working_memory", "apply_transition", "record_in"):
            assert banned not in text, banned
        cli_text = (ROOT / "cli" / "main.py").read_text(encoding="utf-8").split("def _research_analyze", 1)[1].split("\ndef ", 1)[0]
        assert "write_text" in cli_text and ".execute(" not in cli_text and "transaction" not in cli_text

    def test_config_and_env_example(self) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["research_ollama_model"].default is None
        assert "RESEARCH_OLLAMA_MODEL=" in (ROOT / ".env.example").read_text(encoding="utf-8")
