"""v0.39 Phase G4: the opt-in research export — the wearer's `research on|off|status`, the allowlist payload (HMAC pseudonyms, hour rounding, factor band, no
free text or raw ids), the per-database queue, the store-and-forward link (HTTPS, bearer token, batches, timeouts, HTTP 200 only, backoff, outage logging on
state change), the erase queue and the wiring. No network: the HTTP call is injected."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import behavioral_telemetry.research as research_module
from application.models import IncomingMessage
from application.service import ApplicationService
from application.user_service import UserService
from behavioral_telemetry.research import (
    ALLOWLIST, BACKOFF_MAX, MAX_BATCH, RESEARCH_CONSENT_TARGET, RESEARCH_EXPLANATION, ResearchExporter, ResearchPreferences, TelemetryLink, build_record,
    factor_band, instance_pseudonym, research_export_loop, subject_pseudonym, valid_endpoint,
)
from behavioral_telemetry.store import TelemetryStore, record_in
from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 2, 12, 34, 56, tzinfo=timezone.utc)
KEY = "k" * 40
TENANT = "tenant_abc123"
U = "user-0001-secret-id"
URL = "https://hive.example.org"


@pytest.fixture
def core(tmp_path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "t.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def add(core, n, *, user=U, at=NOW + timedelta(minutes=1), event_type="llm_generation", metrics=None, prefix="r"):
    with core.transaction() as tx:
        for i in range(n):
            record_in(tx, user_id=user, occurred_at=at + timedelta(seconds=i), event_type=event_type, source_key=f"{prefix}{user}{i}", now=NOW,
                      metrics=metrics or {"latency_ms": 900 + i, "path": "plain", "outcome": "ok", "structured_status": None, "trusted": True})


def synced(core):
    with core.transaction() as tx:
        return tx.fetch_one("SELECT COUNT(*) AS n FROM behavioral_telemetry WHERE synced_at IS NOT NULL")["n"]


class Post:
    """The injected HTTP call: records every request; `statuses` is consumed in order (default 200); an Exception instance is raised."""

    def __init__(self, *statuses) -> None:
        self.statuses, self.calls = list(statuses), []

    def __call__(self, url, *, json_body, headers, timeout):
        self.calls.append(SimpleNamespace(url=url, body=json.loads(json.dumps(json_body)), headers=headers, timeout=timeout))
        status = self.statuses.pop(0) if self.statuses else 200
        if isinstance(status, Exception):
            raise status
        return status


def link(post):
    return TelemetryLink(endpoint=URL, token="tok-secret", key=KEY, post=post)


class TestPayload:
    @pytest.mark.parametrize("value,band", [(1.0, "1.00-1.10"), ("1.075", "1.00-1.10"), (1.1, "1.10-1.20"), (1.125, "1.10-1.20"), (1.425, "1.40-1.50"),
                                            (1.5, "1.40-1.50"), (0.9, None), (1.6, None), ("x", None), (float("nan"), None), (None, None)])
    def test_factor_band(self, value, band) -> None:
        assert factor_band(value) == band

    def test_only_allowlisted_keys_with_a_band_and_pseudonyms(self) -> None:
        metrics = {"local_date": "2026-11-01", "goals_assigned": 1, "goals_met": 1, "check_ins_due": 2, "check_ins_ok": 2, "violations": 0, "clean": True,
                   "streak_after": 3, "tokens_earned": 0, "progress": 0.2857142, "pool_after": 0.5, "trust_used": 0.8, "training_inflation_factor": 1.125,
                   "operating_mode": "standard", "bot_mood": "approving", "note": "free text"}
        record = build_record(key=KEY, tenant_id=TENANT, row_id="row-1", user_id=U, occurred_at=NOW, event_type="daily_summary", metrics=metrics)
        assert set(record["metrics"]) == set(ALLOWLIST["daily_summary"])
        assert record["metrics"]["training_inflation_band"] == "1.10-1.20" and record["metrics"]["progress"] == 0.2857
        assert record["occurred_hour"] == "2026-11-02T12:00:00Z" and re.fullmatch(r"[0-9a-f]{32}", record["id"]) and re.fullmatch(r"[0-9a-f]{64}", record["subject"])
        dump = json.dumps(record)
        for secret in (U, TENANT, "row-1", "2026-11-01", "free text", "1.125", "training_inflation_factor"):
            assert secret not in dump, secret
        again = build_record(key=KEY, tenant_id=TENANT, row_id="row-1", user_id=U, occurred_at=NOW, event_type="daily_summary", metrics=metrics)
        assert again == record                                        # stable id: a retry is idempotent at the receiver
        other = build_record(key=KEY, tenant_id="tenant_other", row_id="row-1", user_id=U, occurred_at=NOW, event_type="daily_summary", metrics=metrics)
        assert other["subject"] != record["subject"] and other["id"] != record["id"]
        assert subject_pseudonym("another-key" * 4, TENANT, U) != record["subject"]

    @pytest.mark.parametrize("event_type,metrics,key", [
        ("response_delay", {"status": "I was busy, sorry"}, "status"), ("response_delay", {"verdict": "free"}, "verdict"),
        ("llm_generation", {"path": "x" * 10}, "path"), ("daily_summary", {"bot_mood": "furious"}, "bot_mood"),
        ("token_burn", {"amount": float("inf")}, "amount"), ("token_burn", {"amount": "3"}, "amount"), ("conversation_gap", {"seconds_since_last_reply": [1]}, "seconds_since_last_reply")])
    def test_strings_outside_the_enums_and_odd_values_become_null(self, event_type, metrics, key) -> None:
        record = build_record(key=KEY, tenant_id=TENANT, row_id="r", user_id=U, occurred_at=NOW, event_type=event_type, metrics=metrics)
        assert record["metrics"][key] is None

    def test_unknown_types_are_not_exported(self) -> None:
        assert build_record(key=KEY, tenant_id=TENANT, row_id="r", user_id=U, occurred_at=NOW, event_type="mystery", metrics={}) is None

    def test_every_allowlisted_string_is_an_enum(self) -> None:
        for event_type, keys in ALLOWLIST.items():
            for key, allowed in keys.items():
                assert allowed is None or all(re.fullmatch(r"[A-Za-z0-9_.-]{1,24}", v) for v in allowed), (event_type, key)

    def test_endpoint_rules(self) -> None:
        assert valid_endpoint("https://hive.example.org") and valid_endpoint("https://hive.example.org/base")
        for bad in (None, "", "http://hive.example.org", "ftp://x", "https://user:pw@hive.example.org", "hive.example.org", "https://"):
            assert not valid_endpoint(bad), bad
        with pytest.raises(ValueError):
            TelemetryLink(endpoint="http://hive.example.org", token="t", key=KEY)
        with pytest.raises(ValueError):
            TelemetryLink(endpoint=URL, token="", key=KEY)


class TestPreferences:
    def test_default_off_on_and_revoke_with_consent_audit(self, core) -> None:
        prefs = ResearchPreferences(core.db_path, core=core)
        assert not prefs.is_enabled(U) and prefs.revoke(U, now=NOW) is False
        assert prefs.enable(U, now=NOW) and prefs.is_enabled(U) and not prefs.enable(U, now=NOW)
        assert prefs.revoke(U, now=NOW + timedelta(hours=1)) and not prefs.is_enabled(U) and not prefs.revoke(U, now=NOW)
        with core.transaction() as tx:
            consents = tx.fetch_all("SELECT action, explanation_shown, user_comment FROM consent_log WHERE target_id = ? ORDER BY created_at",
                                    (RESEARCH_CONSENT_TARGET,))
            erases = tx.fetch_all("SELECT user_id, sent_at FROM research_erase_requests")
        assert [(c["action"], c["user_comment"]) for c in consents] == [("approved", "research on"), ("revoked", "research off")]
        assert all(c["explanation_shown"] == RESEARCH_EXPLANATION for c in consents)
        assert [(e["user_id"], e["sent_at"]) for e in erases] == [(U, None)]

    def test_the_explanation_says_what_is_and_is_not_sent(self) -> None:
        for part in ("pseudonym", "no names, ids, messages or other text", "rounded to the hour", "coarse band", "after you switch it on", "`research off`",
                     "erase", "`forget all`"):
            assert part in RESEARCH_EXPLANATION, part


class TestQueue:
    def test_only_consenting_users_and_only_after_their_consent(self, core) -> None:
        prefs = ResearchPreferences(core.db_path, core=core)
        add(core, 3, at=NOW - timedelta(hours=2), prefix="before")
        prefs.enable(U, now=NOW)
        add(core, 2)
        add(core, 4, user="not-consenting")
        exporter = ResearchExporter(core, tenant_id=TENANT, key=KEY, export_records=True)
        pairs = exporter.pending_batch(NOW + timedelta(hours=1))
        assert len(pairs) == 2 and all(r["subject"] == subject_pseudonym(KEY, TENANT, U) for _, r in pairs)
        assert ResearchExporter(core, tenant_id=TENANT, key=KEY, export_records=False).pending_batch(NOW + timedelta(hours=1)) == []

    def test_after_research_off_nothing_more_is_queued_and_re_consent_is_not_retroactive(self, core) -> None:
        prefs = ResearchPreferences(core.db_path, core=core)
        prefs.enable(U, now=NOW)
        prefs.revoke(U, now=NOW + timedelta(minutes=5))
        add(core, 2, at=NOW + timedelta(minutes=10))
        exporter = ResearchExporter(core, tenant_id=TENANT, key=KEY, export_records=True)
        assert exporter.pending_batch(NOW + timedelta(hours=1)) == []
        prefs.enable(U, now=NOW + timedelta(minutes=20))
        assert exporter.pending_batch(NOW + timedelta(hours=1)) == []
        assert len(exporter.pending_erases()) == 1 and exporter.pending_erases()[0][1]["subject"] == subject_pseudonym(KEY, TENANT, U)


class TestLink:
    def _setup(self, core, n=3):
        ResearchPreferences(core.db_path, core=core).enable(U, now=NOW)
        add(core, n)
        return ResearchExporter(core, tenant_id=TENANT, key=KEY, export_records=True)

    def test_a_successful_push(self, core) -> None:
        exporter, post = self._setup(core), Post()
        assert link(post).push([exporter], NOW + timedelta(hours=1))
        (call,) = post.calls
        assert call.url == f"{URL}/v1/records" and call.headers == {"Authorization": "Bearer tok-secret"} and call.timeout == (5.0, 20.0)
        assert call.body["schema"] == "hive-telemetry/1" and call.body["instance"] == instance_pseudonym(KEY) and len(call.body["records"]) == 3
        assert synced(core) == 3 and link(Post()).push([exporter], NOW + timedelta(hours=2)) and synced(core) == 3

    def test_only_http_200_marks_rows_synced(self, core) -> None:
        exporter = self._setup(core)
        for status in (201, 204, 400, 401, 500, ConnectionError("refused")):
            assert not link(Post(status)).push([exporter], NOW + timedelta(hours=1))
            assert synced(core) == 0

    def test_batches_respect_the_count_and_byte_limits(self, core, monkeypatch) -> None:
        exporter, post = self._setup(core, n=MAX_BATCH * 2 + 50), Post()
        assert link(post).push([exporter], NOW + timedelta(hours=1))
        assert [len(c.body["records"]) for c in post.calls] == [MAX_BATCH, MAX_BATCH, 50] and synced(core) == MAX_BATCH * 2 + 50
        with core.transaction() as tx:
            tx.execute("UPDATE behavioral_telemetry SET synced_at = NULL")
        monkeypatch.setattr(research_module, "MAX_PAYLOAD_BYTES", 5000)
        post = Post()
        assert link(post).push([exporter], NOW + timedelta(hours=1))
        sent = [r["id"] for c in post.calls for r in c.body["records"]]
        assert all(len(json.dumps(c.body["records"])) <= 5000 for c in post.calls) and len(sent) == len(set(sent)) == MAX_BATCH * 2 + 50

    def test_a_failure_mid_way_never_marks_unsent_rows(self, core, monkeypatch) -> None:
        exporter = self._setup(core, n=MAX_BATCH + 10)
        post = Post(200, 503)
        assert not link(post).push([exporter], NOW + timedelta(hours=1))
        assert synced(core) == MAX_BATCH and len(post.calls) == 2

    def test_backoff_and_outage_logging_on_state_change_only(self, core, caplog) -> None:
        exporter = self._setup(core)
        post = Post(503, ConnectionError("x"), TimeoutError("y"), 500)
        lk = link(post)
        t = NOW + timedelta(hours=1)
        with caplog.at_level(logging.INFO, logger="ai_coach_keyholder.behavioral_telemetry.research"):
            assert not lk.push([exporter], t) and lk.next_attempt_at == t + timedelta(minutes=1)
            assert not lk.push([exporter], t + timedelta(seconds=30)) and len(post.calls) == 1          # inside the backoff: no attempt
            delays = []
            for _ in range(3):
                t = lk.next_attempt_at
                lk.push([exporter], t)
                delays.append(lk.next_attempt_at - t)
            assert delays == [timedelta(minutes=2), timedelta(minutes=4), timedelta(minutes=8)]
            assert lk.push([exporter], lk.next_attempt_at) and synced(core) == 3
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(warnings) == 1 and "down (HTTP 503)" in warnings[0].getMessage()
        assert len(infos) == 1 and "recovered after 4 failed attempt(s)" in infos[0].getMessage()
        assert "tok-secret" not in caplog.text and KEY not in caplog.text

    def test_backoff_is_capped(self, core) -> None:
        exporter = self._setup(core)
        lk = link(Post(*([500] * 30)))
        t = NOW
        for _ in range(20):
            lk.push([exporter], t)
            t = lk.next_attempt_at
        assert lk.failures == 20
        before = lk.next_attempt_at
        lk.push([exporter], before)
        assert lk.next_attempt_at - before == BACKOFF_MAX

    def test_erasures_go_first_even_with_the_export_switched_off(self, core) -> None:
        prefs = ResearchPreferences(core.db_path, core=core)
        prefs.enable(U, now=NOW)
        prefs.revoke(U, now=NOW + timedelta(minutes=1))
        post = Post()
        exporter = ResearchExporter(core, tenant_id=TENANT, key=KEY, export_records=False)
        assert link(post).push([exporter], NOW + timedelta(hours=1))
        (call,) = post.calls
        assert call.url == f"{URL}/v1/erase" and call.body["erase"]["subject"] == subject_pseudonym(KEY, TENANT, U)
        assert exporter.pending_erases() == [] and link(Post()).push([exporter], NOW + timedelta(hours=2))

    def test_a_failed_erasure_stays_queued_and_blocks_nothing_else_forever(self, core) -> None:
        prefs = ResearchPreferences(core.db_path, core=core)
        prefs.enable(U, now=NOW)
        prefs.revoke(U, now=NOW)
        exporter = ResearchExporter(core, tenant_id=TENANT, key=KEY, export_records=True)
        lk = link(Post(500))
        assert not lk.push([exporter], NOW) and len(exporter.pending_erases()) == 1
        assert lk.push([exporter], lk.next_attempt_at) and exporter.pending_erases() == []

    def test_the_real_http_call_keeps_tls_verification_and_no_redirects(self, monkeypatch) -> None:
        import requests
        seen = {}

        class Response:
            status_code = 200

            def close(self):
                seen["closed"] = True

        monkeypatch.setattr(requests, "post", lambda url, **kw: seen.update(url=url, **kw) or Response())
        assert research_module._requests_post(f"{URL}/v1/records", json_body={"a": 1}, headers={"h": "v"}, timeout=(5.0, 20.0)) == 200
        assert "verify" not in seen and seen["allow_redirects"] is False and seen["timeout"] == (5.0, 20.0) and seen["closed"]
        text = (ROOT / "behavioral_telemetry" / "research.py").read_text(encoding="utf-8")
        assert "verify=False" not in text and "verify =" not in text


class TestEndToEndPrivacy:
    def test_a_check_in_answer_never_leaves(self, core) -> None:
        from behavioral_telemetry.collector import TelemetryCollector
        ResearchPreferences(core.db_path, core=core).enable(U, now=NOW - timedelta(days=1))
        with core.raw_connection() as conn:
            conn.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, prompt_text, sent_at, deadline_at, response_text, "
                         "responded_at, verdict, created_at, updated_at) VALUES ('ci-1', ?, '2026-11-02', 1, 2, ?, 'answered', 'PROMPT-SECRET', ?, ?, "
                         "'ANSWER-SECRET', ?, 'SUFFICIENT', ?, ?)",
                         (U, "2026-11-02T10:00:00Z", "2026-11-02T10:00:00Z", "2026-11-02T10:30:00Z", "2026-11-02T10:07:00Z", "2026-11-02T10:00:00Z",
                          "2026-11-02T10:07:00Z"))
            conn.commit()
        TelemetryCollector(TelemetryStore(core.db_path, core=core)).run(NOW)
        post = Post()
        assert link(post).push([ResearchExporter(core, tenant_id=TENANT, key=KEY, export_records=True)], NOW)
        dump = json.dumps([c.body for c in post.calls])
        assert post.calls and "delay_seconds" in dump
        for secret in ("SECRET", U, TENANT, "ci-1", "2026-11-02T10:07", "slot_index"):
            assert secret not in dump, secret


class TestWearerCommands:
    def _service(self, core, *, active=True):
        from coaching_memory.service import CoachingMemoryService
        from tests.application.test_chaster_llm_tool_integration import _onboard
        prefs = ResearchPreferences(core.db_path, core=core, export_active=active)
        service = ApplicationService(core.db_path, core=core, coaching_memory=CoachingMemoryService(core.db_path, core=core),
                                     telemetry=TelemetryStore(core.db_path, core=core), research=prefs)
        _onboard(service)
        return service, UserService(core.db_path, core=core).get_or_create_user("discord", "42", now=NOW).id, prefs

    def say(self, service, text, mid):
        return service.handle_message(IncomingMessage(channel="discord", external_user_id="42", text=text, received_at=NOW, external_message_id=mid)).text

    def test_status_on_off(self, core) -> None:
        service, uid, prefs = self._service(core)
        status = self.say(service, "research status", "a")
        assert "Research sharing is off for you." in status and RESEARCH_EXPLANATION in status and "`research on`" in status
        assert "Research sharing is on." in self.say(service, "research on", "b") and prefs.is_enabled(uid)
        assert "already on" in self.say(service, "research on", "c")
        off = self.say(service, "research off", "d")
        assert "Research sharing is off" in off and "erase" in off and not prefs.is_enabled(uid)
        assert "already off" in self.say(service, "research off", "e")
        assert "Research sharing is off for you." in self.say(service, "research", "f")

    def test_the_owner_switch_is_shown(self, core) -> None:
        service, _, _ = self._service(core, active=False)
        assert "has not enabled the research export" in self.say(service, "research status", "a")
        assert "nothing is sent until they do" in self.say(service, "research on", "b")

    def test_forget_all_revokes_and_queues_the_erase(self, core) -> None:
        service, uid, prefs = self._service(core)
        self.say(service, "research on", "a")
        reply = self.say(service, "forget all", "b")
        assert "Research sharing is off" in reply and "erase" in reply and not prefs.is_enabled(uid)
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM research_erase_requests WHERE user_id = ?", (uid,))["n"] == 1
            assert tx.fetch_one("SELECT user_comment FROM consent_log WHERE action = 'revoked'")["user_comment"] == "forget all"
        assert "Research sharing" not in self.say(service, "forget all", "c")              # nothing more to revoke

    def test_unavailable_without_injection(self, core) -> None:
        from tests.application.test_chaster_llm_tool_integration import _onboard
        service = ApplicationService(core.db_path, core=core)
        _onboard(service)
        assert "not available" in self.say(service, "research on", "a")


class TestWiring:
    def _config(self, core, **kw):
        base = dict(db_path=core.db_path, enable_rules_engine=False, enable_supervision=False, enable_chaster_execution=False,
                    enable_research_telemetry=True, telemetry_endpoint=URL, telemetry_token="tok", telemetry_hmac_key=KEY)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_composition(self, core) -> None:
        from system.composition import build_rules_and_execution, research_link_configured
        wiring = build_rules_and_execution(self._config(core), core, telemetry_tenant_id=TENANT)
        assert wiring.research_exporter.tenant_id == TENANT and wiring.research_preferences.export_active
        for bad in (dict(telemetry_endpoint="http://x.org"), dict(telemetry_token=None), dict(telemetry_hmac_key="short"), dict(telemetry_endpoint=None)):
            config = self._config(core, **bad)
            assert not research_link_configured(config)
            wiring = build_rules_and_execution(config, core)
            assert wiring.research_exporter is None and not wiring.research_preferences.export_active
            assert any("research export is NOT active" in n for n in wiring.notes)
        off = build_rules_and_execution(self._config(core, enable_research_telemetry=False), core)
        assert off.research_exporter is not None and not off.research_preferences.export_active and not any("research" in n for n in off.notes)

    def test_the_loop_is_built_only_when_configured_and_runs_in_a_thread(self, core) -> None:
        from system.composition import research_export_loop_for
        assert research_export_loop_for(self._config(core, telemetry_token=None), lambda: [], FrozenClock(NOW)) is None

        class FakeLink:
            def __init__(self):
                self.calls = []

            def push(self, exporters, now):
                self.calls.append((exporters, now))
                raise RuntimeError("one failure is logged and the loop continues")

        async def scenario():
            fake = FakeLink()
            task = asyncio.create_task(research_export_loop(fake, lambda: ["e"], FrozenClock(NOW), interval_seconds=0.01))
            while len(fake.calls) < 2:
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return fake.calls

        calls = asyncio.run(scenario())
        assert calls[0] == (["e"], NOW)
        coroutine = research_export_loop_for(self._config(core), lambda: [], FrozenClock(NOW))
        assert asyncio.iscoroutine(coroutine)
        coroutine.close()

    def test_tenants_bot_and_config(self) -> None:
        from core.config import Config
        factory = (ROOT / "system" / "tenant_factory.py").read_text(encoding="utf-8")
        assert "telemetry_tenant_id=tenant.id" in factory and "research=wiring.research_preferences" in factory
        bot = (ROOT / "bot" / "discord_bot.py").read_text(encoding="utf-8")
        assert "research_export_loop_for(config, research_exporters, clock)" in bot and 'name="research-export"' in bot
        assert "r.wiring.research_exporter" in bot and "research=wiring.research_preferences" in bot
        fields = Config.__dataclass_fields__
        assert fields["enable_research_telemetry"].default is False and fields["telemetry_endpoint"].default is None
        assert not fields["telemetry_token"].repr and not fields["telemetry_hmac_key"].repr
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        for line in ("ENABLE_RESEARCH_TELEMETRY=false", "TELEMETRY_ENDPOINT=", "TELEMETRY_TOKEN=", "TELEMETRY_HMAC_KEY="):
            assert line in env, line
        config = Config(discord_token="t", telemetry_token="tok-secret", telemetry_hmac_key=KEY)
        assert "tok-secret" not in repr(config) and KEY not in repr(config)
