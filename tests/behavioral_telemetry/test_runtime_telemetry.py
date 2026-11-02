"""v0.39 Phase G2: the Tier C runtime telemetry buffer — O(1) in-memory samples from the live reply path, the engine hook, exception immunity, the batched
drain in the periodic cycle, consent and `forget all`, and the three guards (no DB imports in conversation_engine, no disk access from the sink, a failing sink
never changes a reply). No network."""

from __future__ import annotations

import ast
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import behavioral_telemetry.collector as collector_module
from behavioral_telemetry.collector import TelemetryCollector
from behavioral_telemetry.runtime import MAX_TRACKED_SUBJECTS, RuntimeTelemetryBuffer
from behavioral_telemetry.store import TelemetryStore
from conversation_engine.engine import ConversationEngine
from conversation_engine.model_types import LLMGenerationError
from conversation_engine.subject_queue import SubjectConversationQueue
from infrastructure.database import Database as CoreDatabase
from memory_system.working_memory import InMemoryWorkingMemory
from system.tenant_gateway import CONSENT_TARGET, invitation_text

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def core(tmp_path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "t.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


class Model:
    """Plain and structured generation; `structured` is the raw JSON text (or None = unusable)."""

    def __init__(self, *, structured="ok", error=None) -> None:
        self.structured, self.error = structured, error

    def generate(self, *, request) -> str:
        if self.error:
            raise self.error
        return "plain reply"

    def generate_structured(self, *, request, schema) -> str:
        if self.structured == "ok":
            return json.dumps({"reply_to_user": "structured reply", "save_to_memory": None, "submit_suggestion": None})
        if self.structured == "side_dropped":
            return json.dumps({"reply_to_user": "structured reply", "save_to_memory": {"category": "weakness", "note": "x"}, "submit_suggestion": None})
        return "not json at all"


class Ticks:
    """A fake monotonic clock: each call advances by `step` seconds."""

    def __init__(self, step=0.25) -> None:
        self.t, self.step = 100.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def engine(model, *, sink=None, structured=False, ticks=None):
    wm = InMemoryWorkingMemory(max_exchanges_per_subject=5, max_characters_per_subject=5000)
    return ConversationEngine(model=model, working_memory_reader=wm, working_memory_writer=wm, queue=SubjectConversationQueue(),
                              structured_output=structured, runtime_sink=sink, monotonic=ticks or Ticks())


def turn(eng, subject="s1", now=NOW, text="hi"):
    return eng.generate_response(subject_key=subject, current_user_message=text, language="en", identity_id="alex", now=now).text


def rows(core):
    with core.transaction() as tx:
        found = tx.fetch_all("SELECT * FROM behavioral_telemetry ORDER BY occurred_at, event_type")
    return [dict(r, metrics=json.loads(r["metrics"])) for r in found]


class TestBuffer:
    def test_bounded_oldest_dropped_and_drained_in_order(self) -> None:
        buf = RuntimeTelemetryBuffer(maxlen=3)
        for i in range(5):
            buf.record(f"s{i}", "x", {"i": i}, NOW)
        assert len(buf) == 3 and [s[3]["i"] for s in buf.take()] == [2, 3, 4] and buf.take() == []

    def test_conversation_gap_is_measured_from_the_end_of_the_previous_reply(self) -> None:
        buf = RuntimeTelemetryBuffer()
        buf.record("s1", "llm_generation", {"latency_ms": 4000}, NOW)
        buf.record("s1", "llm_generation", {"latency_ms": 100}, NOW + timedelta(minutes=10))
        buf.record("s2", "llm_generation", {"latency_ms": 100}, NOW)
        gaps = [s for s in buf.take() if s[2] == "conversation_gap"]
        assert [(s[1], s[3]) for s in gaps] == [("s1", {"seconds_since_last_reply": 596})]

    def test_record_never_raises_and_tracking_is_bounded(self) -> None:
        buf = RuntimeTelemetryBuffer()
        buf.record("s", "llm_generation", None, NOW)                       # bad metrics: swallowed
        buf.record("s", "llm_generation", {"latency_ms": "x"}, "not a time")
        for i in range(MAX_TRACKED_SUBJECTS + 5):
            buf.record(f"s{i}", "llm_generation", {"latency_ms": 1}, NOW)
        assert len(buf._last_reply_at) <= MAX_TRACKED_SUBJECTS

    def test_forget_marks_earlier_samples_only(self) -> None:
        buf = RuntimeTelemetryBuffer()
        buf.record("s1", "x", {}, NOW)
        buf.forget("s1")
        buf.record("s1", "x", {}, NOW)
        first, second = buf.take()
        assert buf.is_forgotten("s1", first[0]) and not buf.is_forgotten("s1", second[0]) and not buf.is_forgotten("s2", 1)


class TestEngineHook:
    def test_plain_turn_records_latency_path_and_outcome(self) -> None:
        samples = []
        assert turn(engine(Model(), sink=lambda *a: samples.append(a))) == "plain reply"
        (subject, kind, metrics, at), = samples
        assert (subject, kind, at) == ("s1", "llm_generation", NOW)
        assert metrics == {"latency_ms": 250, "path": "plain", "outcome": "ok", "structured_status": None, "trusted": True}

    @pytest.mark.parametrize("raw,status,reply", [("ok", "ok", "structured reply"), ("side_dropped", "side_dropped", "structured reply"),
                                                  ("bad", "unparseable", "plain reply")])
    def test_structured_status_including_parse_failures(self, raw, status, reply) -> None:
        samples = []
        assert turn(engine(Model(structured=raw), sink=lambda *a: samples.append(a), structured=True)) == reply
        assert samples[0][2]["structured_status"] == status and samples[0][2]["path"] == "structured"

    def test_a_generation_failure_is_recorded_too(self) -> None:
        samples = []
        eng = engine(Model(error=LLMGenerationError("ollama_timeout")), sink=lambda *a: samples.append(a))
        turn(eng)
        assert samples[0][2]["outcome"] == "generation_failed"

    def test_untrusted_turns_are_marked(self) -> None:
        samples = []
        eng = engine(Model(), sink=lambda *a: samples.append(a))
        eng.generate_response(subject_key="s1", current_user_message="hi", language="en", identity_id="alex", now=NOW, allow_side_data=False)
        assert samples[0][2]["trusted"] is False


class TestGuards:
    DB_MODULES = ("sqlite3", "infrastructure.database", "infrastructure.outbox", "database", "behavioral_telemetry")

    def test_conversation_engine_imports_no_database_module(self) -> None:
        offenders = []
        for path in (ROOT / "conversation_engine").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                for name in names:
                    if any(name == m or name.startswith(m + ".") for m in self.DB_MODULES):
                        offenders.append(f"{path.relative_to(ROOT)}: {name}")
                    if path.name == "engine.py" and name.endswith(".repository"):
                        offenders.append(f"{path.relative_to(ROOT)}: {name}")
        assert offenders == []

    def test_the_sink_never_touches_the_disk(self, monkeypatch) -> None:
        def refuse(*_a, **_k):
            raise AssertionError("the runtime sink opened a database")

        monkeypatch.setattr(sqlite3, "connect", refuse)
        monkeypatch.setattr(CoreDatabase, "transaction", refuse)
        monkeypatch.setattr(CoreDatabase, "raw_connection", refuse)
        monkeypatch.setattr("builtins.open", refuse)
        buf = RuntimeTelemetryBuffer()
        assert turn(engine(Model(), sink=buf.record)) == "plain reply"
        assert turn(engine(Model(), sink=buf.record), now=NOW + timedelta(minutes=1)) == "plain reply"
        assert {s[2] for s in buf.take()} == {"llm_generation", "conversation_gap"}

    def test_a_failing_sink_never_changes_the_reply(self, caplog) -> None:
        def broken(*_a):
            raise RuntimeError("telemetry down")

        for structured, raw in ((False, "ok"), (True, "ok"), (True, "bad")):
            clean = turn(engine(Model(structured=raw), structured=structured))
            eng = engine(Model(structured=raw), sink=broken, structured=structured)
            with caplog.at_level(logging.WARNING):
                assert turn(eng) == clean and turn(eng, now=NOW + timedelta(minutes=1)) == clean
        assert caplog.text.count("Runtime telemetry sink failed") == 3      # once per engine, never per turn

    def test_a_failing_monotonic_clock_never_changes_the_reply_either(self) -> None:
        calls = {"n": 0}

        def clock():
            calls["n"] += 1
            raise OSError("clock")

        samples = []
        assert turn(engine(Model(), sink=lambda *a: samples.append(a), ticks=clock)) == "plain reply" and calls["n"] == 1 and samples == []


class TestDrain:
    def test_one_batched_transaction_into_the_database(self, core, monkeypatch) -> None:
        buf = RuntimeTelemetryBuffer()
        store = TelemetryStore(core.db_path, core=core, buffer=buf)
        eng = engine(Model(), sink=buf.record)
        for minute in range(3):
            turn(eng, now=NOW + timedelta(minutes=minute))
        calls = []
        real = collector_module.apply_transition
        monkeypatch.setattr(collector_module, "apply_transition", lambda *a, **k: calls.append(1) or real(*a, **k))
        collector = TelemetryCollector(store, buffer=buf)
        assert collector._drain(NOW) == 5 and len(calls) == 1 and len(buf) == 0
        kinds = [r["event_type"] for r in rows(core)]
        assert kinds.count("llm_generation") == 3 and kinds.count("conversation_gap") == 2
        assert all(r["source_key"].startswith("runtime:") and r["user_id"] == "s1" for r in rows(core))

    def test_without_consent_samples_are_discarded(self, core) -> None:
        buf = RuntimeTelemetryBuffer()
        store = TelemetryStore(core.db_path, core=core, consent_target=CONSENT_TARGET, buffer=buf)
        turn(engine(Model(), sink=buf.record))
        assert TelemetryCollector(store, buffer=buf).run(NOW) == 0 and len(buf) == 0 and rows(core) == []

    def test_forget_all_drops_undrained_samples(self, core) -> None:
        buf = RuntimeTelemetryBuffer()
        store = TelemetryStore(core.db_path, core=core, buffer=buf)
        eng = engine(Model(), sink=buf.record)
        turn(eng)
        turn(eng, subject="other")
        store.forget_all("s1")
        turn(eng, now=NOW + timedelta(minutes=5))              # after the erase: kept, and no gap back to the erased turn
        TelemetryCollector(store, buffer=buf).run(NOW + timedelta(minutes=6))
        assert sorted((r["user_id"], r["event_type"]) for r in rows(core)) == [("other", "llm_generation"), ("s1", "llm_generation")]

    def test_a_refused_sample_is_skipped_with_a_warning(self, core, caplog) -> None:
        buf = RuntimeTelemetryBuffer()
        buf.record("s1", "llm_generation", {"note": "free text from somewhere"}, NOW)
        buf.record("s1", "unknown_kind", {}, NOW)
        with caplog.at_level(logging.WARNING):
            assert TelemetryCollector(TelemetryStore(core.db_path, core=core), buffer=buf).run(NOW) == 0
        assert caplog.text.count("runtime telemetry sample was refused") == 2


class TestWiring:
    def test_the_composition_shares_one_buffer(self, core) -> None:
        from system.composition import build_rules_and_execution
        wiring = build_rules_and_execution(SimpleNamespace(db_path=core.db_path, enable_rules_engine=False, enable_supervision=False,
                                                           enable_chaster_execution=False), core)
        assert isinstance(wiring.telemetry_buffer, RuntimeTelemetryBuffer)
        assert wiring.telemetry._buffer is wiring.telemetry_buffer and wiring.telemetry_store._buffer is wiring.telemetry_buffer
        for rel in ("system/tenant_factory.py", "bot/discord_bot.py"):
            assert "runtime_sink=wiring.telemetry_buffer.record" in (ROOT / rel).read_text(encoding="utf-8"), rel

    def test_a_tenant_turn_reaches_its_own_database_through_the_periodic_cycle(self, tmp_path) -> None:
        from database.database import Database
        from database.models import ConsentAction, ConsentRecord, ConsentTargetType
        from tests.system.test_tenant_factory import World

        w = World(tmp_path)
        tenant = w.wearer("Alex", "2000002")
        runtime = w.factory.build(tenant.id)
        Database(runtime.db_path, core=runtime.core).save_consent_record(ConsentRecord(
            created_at=w.clock.now(), target_type=ConsentTargetType.INTEGRATION, target_id=CONSENT_TARGET, action=ConsentAction.APPROVED,
            explanation_shown=invitation_text("Alex"), user_comment="accept"))
        for i, text in enumerate(("anything", "english", "neutral", "alex", "how am I doing?")):
            w.say(runtime, "2000002", text, f"m{i}")
        assert runtime.cycle()
        with runtime.core.transaction() as tx:
            found = tx.fetch_all("SELECT user_id, event_type FROM behavioral_telemetry")
        assert [(r["user_id"], r["event_type"]) for r in found] == [(runtime.wearer_user_id, "llm_generation")]
        with w.main.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM behavioral_telemetry")["n"] == 0
