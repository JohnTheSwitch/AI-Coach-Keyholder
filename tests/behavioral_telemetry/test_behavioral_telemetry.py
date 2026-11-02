"""v0.39 Phase G1: local derived Behavioral Telemetry — derivation from existing tables, idempotent watermarks, retention, the consent gate, the periodic
hook (last, isolated), the wearer's `telemetry` / `forget all`, the invitation disclosure and the guards that keep it out of the model context. No network."""

from __future__ import annotations

import ast
import itertools
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import behavioral_telemetry.collector as collector_module
from application.models import IncomingMessage
from application.service import ApplicationService
from application.user_service import UserService
from behavioral_telemetry.collector import TelemetryCollector, bot_mood
from behavioral_telemetry.store import (
    DISCLOSURE_MARKER, RETENTION_DAYS, TELEMETRY_DISCLOSURE, TelemetryStore, record_in,
)
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from system.periodic import run_periodic_cycle
from system.tenant_factory import TENANT_CONSENT_TARGET
from system.tenant_gateway import CONSENT_TARGET, invitation_text

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
U = "wearer"
_SLOTS = itertools.count(1)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def core(tmp_path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "t.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def sql(core, statement, params=()):
    with core.raw_connection() as conn:
        conn.execute(statement, params)
        conn.commit()


def rows(core, event_type=None):
    with core.transaction() as tx:
        found = tx.fetch_all("SELECT * FROM behavioral_telemetry" + (" WHERE event_type = ?" if event_type else "") + " ORDER BY occurred_at, id",
                             (event_type,) if event_type else ())
    return [dict(r, metrics=json.loads(r["metrics"])) for r in found]


def check_in(core, cid, *, status="answered", sent=NOW - timedelta(hours=2), responded=NOW - timedelta(hours=2) + timedelta(minutes=7),
             updated=None, user=U, text="SECRET-ANSWER-TEXT"):
    slot = next(_SLOTS)                                   # UNIQUE(user, local_date, slot_index)
    sql(core, "INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, prompt_text, sent_at, deadline_at, response_text, "
              "responded_at, verdict, created_at, updated_at) VALUES (?, ?, ?, ?, 2, ?, ?, 'SECRET-PROMPT', ?, ?, ?, ?, ?, ?, ?)",
        (cid, user, sent.date().isoformat(), slot, iso(sent), status, iso(sent), iso(sent + timedelta(minutes=30)),
         text if status != "missed" else None, iso(responded) if status != "missed" else None,
         "SUFFICIENT" if status == "answered" else None, iso(sent), iso(updated or (responded if status != "missed" else sent + timedelta(minutes=30)))))


def ledger(core, lid, delta, at, entry_type="earn", user=U):
    sql(core, "INSERT INTO token_ledger (id, user_id, delta, entry_type, reason_code, created_at) VALUES (?, ?, ?, ?, 'r', ?)",
        (lid, user, delta, entry_type, iso(at)))


def daily(core, day, *, clean=1, streak=1, progress="0.286", created=None, user=U):
    sql(core, "INSERT INTO daily_evaluations (user_id, local_date, goals_assigned, goals_met, check_ins_due, check_ins_ok, violations, trust_used, "
              "progress, pool_after, tokens_earned, clean, streak_after, bonus, created_at) VALUES (?, ?, 1, 1, 2, 2, 0, '0.8', ?, '0.5', 0, ?, ?, '0', ?)",
        (user, day.isoformat(), progress, clean, streak, iso(created or datetime(day.year, day.month, day.day, 23, 5, tzinfo=timezone.utc))))


def increment(core, iid, at, before="1.000", after="1.025", outcome="increased", user=U):
    sql(core, "INSERT INTO training_increments (id, user_id, authorization_id, outcome, factor_before, factor_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (iid, user, "auth-" + iid, outcome, before, after, iso(at)))


@pytest.fixture
def collector(core) -> TelemetryCollector:
    return TelemetryCollector(TelemetryStore(core.db_path, core=core))


class TestSchemaAndStore:
    def test_migration_039_creates_the_tables(self, core) -> None:
        with core.transaction() as tx:
            names = {r["name"] for r in tx.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert tx.fetch_one("SELECT MAX(version) AS v FROM schema_version")["v"] == 39
        assert {"behavioral_telemetry", "telemetry_watermarks"} <= names

    @pytest.mark.parametrize("metrics", [{"note": "the wearer said something private"}, {"x": "a" * 33}, {"Bad Key": 1}, {"x": [1]}, {"x": {"y": 1}}, "nope"])
    def test_free_text_and_structures_are_refused(self, core, metrics) -> None:
        with core.transaction() as tx, pytest.raises(ValueError):
            record_in(tx, user_id=U, occurred_at=NOW, event_type="daily_summary", metrics=metrics, source_key="k", now=NOW)

    def test_unknown_event_types_are_refused_and_a_source_row_is_stored_once(self, core) -> None:
        with core.transaction() as tx:
            with pytest.raises(ValueError):
                record_in(tx, user_id=U, occurred_at=NOW, event_type="mood_swing", metrics={}, source_key="k", now=NOW)
            first = record_in(tx, user_id=U, occurred_at=NOW, event_type="token_burn", metrics={"amount": 1, "verdict": "SUFFICIENT"}, source_key="k", now=NOW)
            again = record_in(tx, user_id=U, occurred_at=NOW, event_type="token_burn", metrics={"amount": 2}, source_key="k", now=NOW)
        assert first and not again and len(rows(core)) == 1 and rows(core)[0]["synced_at"] is None


class TestDerivation:
    def test_check_ins_become_response_delays_without_any_text(self, core, collector) -> None:
        check_in(core, "c1")
        check_in(core, "c2", status="missed", sent=NOW - timedelta(hours=5))
        check_in(core, "c3", status="scheduled")
        assert collector.run(NOW) == 2
        answered, missed = sorted(rows(core, "response_delay"), key=lambda r: r["metrics"]["status"])
        assert answered["metrics"].pop("slot_index") >= 1
        assert answered["metrics"] == {"level": 2, "status": "answered", "verdict": "SUFFICIENT", "delay_seconds": 420, "window_seconds": 1800}
        assert missed["metrics"]["delay_seconds"] is None and missed["metrics"]["status"] == "missed"
        with core.transaction() as tx:
            dump = repr([tuple(r) for r in tx.fetch_all("SELECT * FROM behavioral_telemetry")])
        assert "SECRET" not in dump

    def test_token_spends_become_token_burns(self, core, collector) -> None:
        ledger(core, "e1", 1, NOW - timedelta(hours=30))
        ledger(core, "e2", 1, NOW - timedelta(hours=20))
        ledger(core, "s1", -1, NOW - timedelta(hours=8), entry_type="spend")
        collector.run(NOW)
        (burn,) = rows(core, "token_burn")
        assert burn["metrics"] == {"amount": 1, "balance_after": 1, "hours_since_last_earn": 12.0}

    def test_increments_become_inflation_resilience(self, core, collector) -> None:
        for i in range(1, 8):
            daily(core, (NOW - timedelta(days=i)).date(), clean=int(i % 2 == 1), progress="0.2")
        increment(core, "i1", NOW - timedelta(days=10), after="1.025")
        increment(core, "i2", NOW - timedelta(hours=1), before="1.025", after="1.050")
        collector.run(NOW)
        latest = rows(core, "inflation_resilience")[-1]["metrics"]
        assert latest["training_inflation_factor"] == 1.05 and latest["factor_before"] == 1.025 and latest["outcome"] == "increased"
        assert latest["progress_avg_7d"] == 0.2 and latest["clean_days_7d"] == 4 and 9.9 < latest["days_since_previous_increment"] < 10

    def test_daily_summaries_carry_the_factor_in_force_and_a_derived_mood(self, core, collector) -> None:
        d1, d2, d3 = ((NOW - timedelta(days=k)).date() for k in (3, 2, 1))
        increment(core, "i1", datetime(d2.year, d2.month, d2.day, 12, tzinfo=timezone.utc), after="1.025")
        daily(core, d1, clean=1, streak=3)
        daily(core, d2, clean=0, streak=0)
        daily(core, d3, clean=1, streak=1)
        collector.run(NOW)
        by_date = {r["metrics"]["local_date"]: r["metrics"] for r in rows(core, "daily_summary")}
        assert by_date[d1.isoformat()]["training_inflation_factor"] == 1.0 and by_date[d1.isoformat()]["bot_mood"] == "approving"
        assert by_date[d2.isoformat()]["training_inflation_factor"] == 1.025 and by_date[d2.isoformat()]["bot_mood"] == "demanding"
        assert by_date[d3.isoformat()]["bot_mood"] == "neutral" and by_date[d3.isoformat()]["operating_mode"] == "standard"

    def test_bot_mood_rules(self) -> None:
        assert bot_mood(strict=True, clean=True, streak=9) == "strict"
        assert bot_mood(strict=False, clean=False, streak=0) == "demanding"
        assert bot_mood(strict=False, clean=True, streak=3) == "approving"
        assert bot_mood(strict=False, clean=True, streak=2) == "neutral"


class TestWatermarksAndRetention:
    def test_reruns_restarts_and_a_second_instance_add_nothing(self, core, collector) -> None:
        check_in(core, "c1")
        ledger(core, "s1", -1, NOW - timedelta(hours=1), entry_type="spend")
        assert collector.run(NOW) == 2
        assert collector.run(NOW) == 0
        assert TelemetryCollector(TelemetryStore(core.db_path, core=core)).run(NOW + timedelta(minutes=1)) == 0
        check_in(core, "c2", updated=NOW + timedelta(minutes=2))
        assert collector.run(NOW + timedelta(minutes=3)) == 1 and len(rows(core)) == 3

    def test_large_backlogs_are_read_in_batches(self, core, collector, monkeypatch) -> None:
        monkeypatch.setattr(collector_module, "BATCH", 3)
        same = NOW - timedelta(hours=1)
        for i in range(8):
            check_in(core, f"c{i}", updated=same)                     # identical timestamps: the id breaks the tie
        counts = [collector.run(NOW) for _ in range(4)]
        assert counts == [3, 3, 2, 0] and len(rows(core)) == 8

    def test_facts_older_than_the_retention_are_never_derived_and_old_rows_are_pruned(self, core, collector) -> None:
        old = NOW - timedelta(days=RETENTION_DAYS + 5)
        check_in(core, "old", sent=old, responded=old + timedelta(minutes=1))
        check_in(core, "new")
        assert collector.run(NOW) == 1
        later = NOW + timedelta(days=RETENTION_DAYS + 1)
        collector.run(later)
        assert rows(core) == []

    def test_a_forgotten_user_is_not_derived_again(self, core, collector) -> None:
        check_in(core, "c1")
        collector.run(NOW)
        assert TelemetryStore(core.db_path, core=core).forget_all(U) == 1
        collector.run(NOW + timedelta(minutes=1))
        assert rows(core) == []


class TestConsent:
    def _consent(self, core, text, action="approved"):
        sql(core, "INSERT INTO consent_log (id, created_at, target_type, target_id, action, explanation_shown) VALUES (?, ?, 'integration', ?, ?, ?)",
            (text[:8] + action, iso(NOW), CONSENT_TARGET, action, text))

    def test_a_tenant_is_recorded_only_after_accepting_the_disclosing_invitation(self, core) -> None:
        store = TelemetryStore(core.db_path, core=core, consent_target=TENANT_CONSENT_TARGET)
        tenant = TelemetryCollector(store)
        check_in(core, "c1")
        assert tenant.run(NOW) == 0 and not store.is_recording()
        self._consent(core, "an older invitation without the disclosure")
        assert tenant.run(NOW) == 0
        self._consent(core, invitation_text("Alex"), action="rejected")
        assert tenant.run(NOW) == 0
        self._consent(core, invitation_text("Alex"))
        assert store.is_recording() and tenant.run(NOW) == 1

    def test_tenant_zero_needs_no_invitation(self, core) -> None:
        assert TelemetryStore(core.db_path, core=core).is_recording()

    def test_the_constants_and_the_invitation_agree(self) -> None:
        text = invitation_text("Alex")
        assert TENANT_CONSENT_TARGET == CONSENT_TARGET and TELEMETRY_DISCLOSURE in text and DISCLOSURE_MARKER in text
        for part in ("`telemetry`", "`forget all`", f"{RETENTION_DAYS} days", "chat response times", "nothing is sent anywhere unless you separately opt in",
                     "Progressive Conditioning", "`accept`", "`halt`", "`leave`"):
            assert part in text, part


class TestPeriodicHook:
    class Probe:
        def __init__(self, order, name, fail=False):
            self.order, self.name, self.fail = order, name, fail

        def run(self, now):
            self.order.append(self.name)
            if self.fail:
                raise RuntimeError("boom")

        def evaluate_pending(self, *, now):
            self.run(now)

        def record_trigger_in_transaction(self, tx, event):
            pass

    def test_telemetry_runs_last_and_even_after_an_earlier_step_failed(self, core) -> None:
        order = []
        clock = FrozenClock(NOW)
        assert run_periodic_cycle(core, "p", clock, rules_engine=self.Probe(order, "rules"), executor=None, daily=self.Probe(order, "daily"),
                                  telemetry=self.Probe(order, "telemetry"))
        assert order == ["rules", "daily", "telemetry"]
        order.clear()
        assert run_periodic_cycle(core, "p", clock, rules_engine=self.Probe(order, "rules", fail=True), executor=None,
                                  telemetry=self.Probe(order, "telemetry"))
        assert order == ["rules", "telemetry"]

    def test_a_failing_collector_never_breaks_the_cycle_or_the_lease(self, core, caplog) -> None:
        order = []
        clock = FrozenClock(NOW)
        assert run_periodic_cycle(core, "p", clock, rules_engine=None, executor=None, telemetry=self.Probe(order, "telemetry", fail=True))
        assert run_periodic_cycle(core, "other-process", clock, rules_engine=None, executor=None, telemetry=self.Probe(order, "telemetry"))
        assert order == ["telemetry", "telemetry"] and "telemetry collection failed" in caplog.text

    def test_the_composition_always_builds_it_and_the_tenant_factory_gates_it(self, core) -> None:
        from system.composition import build_rules_and_execution
        config = SimpleNamespace(db_path=core.db_path, enable_rules_engine=False, enable_supervision=False, enable_chaster_execution=False)
        wiring = build_rules_and_execution(config, core)
        assert isinstance(wiring.telemetry, TelemetryCollector) and wiring.telemetry_store.is_recording()
        text = (ROOT / "system" / "tenant_factory.py").read_text(encoding="utf-8")
        assert "telemetry_consent_target=TENANT_CONSENT_TARGET" in text and "telemetry=w.telemetry" in text and "telemetry=wiring.telemetry_store" in text
        bot = (ROOT / "bot" / "discord_bot.py").read_text(encoding="utf-8")
        assert "wiring.telemetry)" in bot and "telemetry=telemetry" in bot and "telemetry=wiring.telemetry_store" in bot


class TestWearerCommands:
    def _service(self, core, *, telemetry=True, memory=True):
        from coaching_memory.service import CoachingMemoryService
        from tests.application.test_chaster_llm_tool_integration import _onboard
        store = TelemetryStore(core.db_path, core=core) if telemetry else None
        mem = CoachingMemoryService(core.db_path, core=core) if memory else None
        service = ApplicationService(core.db_path, core=core, coaching_memory=mem, telemetry=store)
        _onboard(service)
        user = UserService(core.db_path, core=core).get_or_create_user("discord", "42", now=NOW)
        return service, user.id, mem

    def say(self, service, text, mid):
        return service.handle_message(IncomingMessage(channel="discord", external_user_id="42", text=text, received_at=NOW,
                                                      external_message_id=mid)).text

    def test_the_view_shows_counts_but_never_a_value(self, core) -> None:
        service, uid, _ = self._service(core)
        assert "There are no records" in self.say(service, "telemetry", "a")
        increment(core, "i1", NOW - timedelta(hours=1), before="1.475", after="1.500", user=uid)
        daily(core, (NOW - timedelta(days=1)).date(), user=uid)
        TelemetryCollector(TelemetryStore(core.db_path, core=core)).run(NOW)
        text = self.say(service, "telemetry", "b")
        assert "recorded for you" in text and "daily summary 1" in text and "inflation resilience 1" in text and "`forget all`" in text
        assert not re.search(r"1\.[0-9]{2,3}", text) and "factor" not in text.lower()

    def test_forget_all_erases_memory_and_telemetry(self, core) -> None:
        service, uid, mem = self._service(core)
        mem.record(user_id=uid, category="stated_goal", content="Wants to run 5k by December", user_message="ok", now=NOW)
        daily(core, (NOW - timedelta(days=1)).date(), user=uid)
        daily(core, (NOW - timedelta(days=1)).date(), user="someone-else")
        TelemetryCollector(TelemetryStore(core.db_path, core=core)).run(NOW)
        reply = self.say(service, "forget all", "c")
        assert "Erased 1 entry" in reply and "Erased 1 behavioral telemetry record." in reply
        assert mem.entries(uid) == [] and [r["user_id"] for r in rows(core)] == ["someone-else"]

    def test_forget_all_works_without_coaching_memory_and_the_view_says_when_it_is_off(self, core) -> None:
        service, _, _ = self._service(core, memory=False)
        assert "Erased 0 behavioral telemetry records." in self.say(service, "forget all", "d")
        bare, _, _ = self._service(core, telemetry=False, memory=False)
        assert "not available" in self.say(bare, "telemetry", "e")

    def test_a_tenant_that_has_not_consented_is_told_so(self, core) -> None:
        from tests.application.test_chaster_llm_tool_integration import _onboard
        service = ApplicationService(core.db_path, core=core, telemetry=TelemetryStore(core.db_path, core=core, consent_target=CONSENT_TARGET))
        _onboard(service)
        assert "not recorded for you" in self.say(service, "telemetry", "f")


class TestNeverInTheModelContext:
    ALLOWED = {"behavioral_telemetry", "system", "tests"}

    def test_only_the_package_the_system_wiring_and_tests_import_it(self) -> None:
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in self.ALLOWED or rel.parts[0].startswith(".") or "__pycache__" in rel.parts or "venv" in rel.parts[0]:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                if any(n.split(".")[0] == "behavioral_telemetry" for n in names):
                    offenders.append(str(rel))
        assert offenders == []

    def test_nothing_in_the_model_context_path_reads_the_table(self) -> None:
        for package in ("conversation_engine", "coaching_memory", "memory_system", "application", "idea_box"):
            for path in (ROOT / package).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                assert not re.search(r"(?<![a-z_.])behavioral_telemetry(?![a-z_.])", text) and "telemetry_watermarks" not in text, path

    def test_the_new_package_uses_the_clock_and_reads_sources_only(self) -> None:
        for path in (ROOT / "behavioral_telemetry").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "datetime.now" not in text and "verify=False" not in text
            written = set(re.findall(r"(?:INSERT(?: OR IGNORE)? INTO|UPDATE|DELETE FROM)\s+([a-z_]+)", text))
            assert written <= {"behavioral_telemetry", "telemetry_watermarks"}, written
