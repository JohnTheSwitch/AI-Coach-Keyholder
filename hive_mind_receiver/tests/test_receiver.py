"""hive_mind_receiver: auth, rate limits, size limits, strict schema validation, idempotency, erase, retention and secrecy. FastAPI's TestClient runs the
app in process: no network. Run with the receiver's own dependencies: `python -m pytest hive_mind_receiver/tests` (see README.md)."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from hive_mind_receiver import contract
from hive_mind_receiver.app import Guard, RateLimiter, Settings, create_app, settings_from_env

TOKEN = "a" * 40
OTHER = "b" * 40
NOW = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)
SUBJECT = "1" * 64
INSTANCE = "f" * 64

SAMPLE_METRICS = {
    "response_delay": {"level": 2, "status": "answered", "verdict": "SUFFICIENT", "delay_seconds": 420, "window_seconds": 1800},
    "token_burn": {"amount": 1, "balance_after": 0, "hours_since_last_earn": 12.5},
    "inflation_resilience": {"outcome": "increased", "training_inflation_band": "1.00-1.10", "days_since_previous_increment": None,
                             "progress_avg_7d": 0.2, "clean_days_7d": 4},
    "daily_summary": {"goals_assigned": 1, "goals_met": 1, "check_ins_due": 2, "check_ins_ok": 2, "violations": 0, "clean": True, "streak_after": 3,
                      "tokens_earned": 0, "progress": 0.2857, "pool_after": 0.5, "trust_used": 0.8, "training_inflation_band": "1.10-1.20",
                      "operating_mode": "standard", "bot_mood": "approving"},
    "llm_generation": {"latency_ms": 9000, "path": "structured", "outcome": "ok", "structured_status": "unparseable", "trusted": True},
    "conversation_gap": {"seconds_since_last_reply": 596},
}


def record(i=0, *, event_type="llm_generation", subject=SUBJECT, **override):
    r = {"id": f"{i:032x}", "subject": subject, "event_type": event_type, "occurred_hour": "2026-11-03T09:00:00Z",
         "metrics": copy.deepcopy(SAMPLE_METRICS[event_type])}
    r.update(override)
    return r


def batch(records, **override):
    body = {"schema": contract.SCHEMA, "instance": INSTANCE, "records": records}
    body.update(override)
    return body


def erase_body(erase_id="e" * 32, subject=SUBJECT):
    return {"schema": contract.SCHEMA, "instance": INSTANCE, "erase": {"id": erase_id, "subject": subject}}


class Clock:
    def __init__(self) -> None:
        self.now, self.mono = NOW, 1000.0

    def advance(self, **kw) -> None:
        delta = timedelta(**kw)
        self.now += delta
        self.mono += delta.total_seconds()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(tmp_path, clock):
    settings = Settings(db_path=tmp_path / "hive.sqlite", tokens=(TOKEN, OTHER), requests_per_minute=50, auth_failures_per_minute=5)
    app = create_app(settings, now=lambda: clock.now, monotonic=lambda: clock.mono)
    with TestClient(app) as c:
        c.db_path = settings.db_path
        yield c


def post(client, path, body, token=TOKEN, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers.update(kw.pop("headers", {}))
    return client.post(path, json=body, headers=headers, **kw)


def rows(client, sql="SELECT sender, id, subject FROM records ORDER BY sender, id"):
    conn = sqlite3.connect(client.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


class TestAuth:
    @pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer wrong-token-xxxxxxxxxxxxxxxxxxxx", f"Basic {TOKEN}", f"bearer {TOKEN}", f"Bearer {TOKEN}x"])
    def test_rejected_before_anything_is_read(self, client, header) -> None:
        headers = {} if header is None else {"Authorization": header}
        response = client.post("/v1/records", json=batch([record()]), headers=headers)
        assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer" and rows(client) == []

    def test_failed_authentications_are_rate_limited(self, client, clock) -> None:
        codes = [post(client, "/v1/records", batch([record()]), token="x" * 40).status_code for _ in range(7)]
        assert codes == [401] * 5 + [429] * 2
        assert post(client, "/v1/records", batch([record()])).status_code == 200           # a valid sender is not blocked by it
        clock.advance(minutes=1)
        assert post(client, "/v1/records", batch([record()]), token="x" * 40).status_code == 401

    def test_each_sender_has_its_own_request_budget(self, client, clock) -> None:
        codes = [post(client, "/v1/records", batch([record(i)])).status_code for i in range(52)]
        assert codes[:50] == [200] * 50 and codes[50:] == [429, 429]
        limited = post(client, "/v1/records", batch([record(99)]))
        assert int(limited.headers["retry-after"]) >= 1
        assert post(client, "/v1/records", batch([record(1)]), token=OTHER).status_code == 200
        clock.advance(seconds=5)
        assert post(client, "/v1/records", batch([record(100)])).status_code == 200

    def test_health_needs_no_token_and_says_nothing(self, client) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200 and response.json() == {"status": "ok"}

    def test_no_documentation_endpoints_and_only_post(self, client) -> None:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
        assert client.get("/v1/records", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 405
        assert client.put("/v1/erase", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 405


class TestLimits:
    def test_json_only(self, client) -> None:
        response = client.post("/v1/records", content=json.dumps(batch([record()])), headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "text/plain"})
        assert response.status_code == 415

    def test_a_length_is_required(self, client) -> None:
        def chunks():
            yield json.dumps(batch([record()])).encode()

        response = client.post("/v1/records", content=chunks(), headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        assert response.status_code == 411

    def test_declared_size_over_the_limit(self, client) -> None:
        big = json.dumps(batch([record()], pad="x" * (contract.MAX_BODY_BYTES + 10)))
        response = client.post("/v1/records", content=big, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        assert response.status_code == 413 and rows(client) == []

    def test_a_body_longer_than_declared_is_refused(self, tmp_path) -> None:
        settings = Settings(db_path=tmp_path / "h.sqlite", tokens=(TOKEN,))
        reached = []

        async def app(scope, receive, send):
            reached.append(True)

        guard = Guard(app, settings=settings, monotonic=lambda: 0.0)
        sent = []
        messages = [{"type": "http.request", "body": b"x" * 600, "more_body": True}, {"type": "http.request", "body": b"y" * 600, "more_body": False}]

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "path": "/v1/records", "method": "POST",
                 "headers": [(b"authorization", f"Bearer {TOKEN}".encode()), (b"content-type", b"application/json"), (b"content-length", b"1000")]}
        asyncio.run(guard(scope, receive, send))
        assert sent[0]["status"] == 413 and reached == []

    def test_batch_size(self, client) -> None:
        assert post(client, "/v1/records", batch([record(i) for i in range(contract.MAX_RECORDS_PER_BATCH)])).status_code == 200
        assert post(client, "/v1/records", batch([record(i) for i in range(contract.MAX_RECORDS_PER_BATCH + 1)])).status_code == 422
        assert post(client, "/v1/records", batch([])).status_code == 422


class TestValidation:
    def test_every_event_type_of_the_contract_is_accepted(self, client) -> None:
        response = post(client, "/v1/records", batch([record(i, event_type=t) for i, t in enumerate(contract.METRICS)]))
        assert response.status_code == 200 and response.json() == {"stored": len(contract.METRICS), "duplicates": 0}
        assert set(SAMPLE_METRICS) == set(contract.METRICS)

    @pytest.mark.parametrize("mutate", [
        lambda b: b.update(schema="hive-telemetry/2"), lambda b: b.pop("schema"), lambda b: b.update(extra="x"), lambda b: b.update(instance="F" * 64),
        lambda b: b.update(instance="f" * 63), lambda b: b["records"][0].update(id="A" * 32), lambda b: b["records"][0].update(id="a" * 31),
        lambda b: b["records"][0].update(subject="1" * 65), lambda b: b["records"][0].update(event_type="mood_swing"),
        lambda b: b["records"][0].update(occurred_hour="2026-11-03T09:15:00Z"), lambda b: b["records"][0].update(occurred_hour="2026-13-03T09:00:00Z"),
        lambda b: b["records"][0].update(occurred_hour="2026-11-03 09:00:00"), lambda b: b["records"][0].update(note="I felt awful today"),
        lambda b: b["records"][0]["metrics"].pop("latency_ms"), lambda b: b["records"][0]["metrics"].update(extra=1),
        lambda b: b["records"][0]["metrics"].update(path="free text"), lambda b: b["records"][0]["metrics"].update(latency_ms="9000"),
        lambda b: b["records"][0]["metrics"].update(latency_ms=1e13), lambda b: b["records"][0]["metrics"].update(latency_ms=[1]),
        lambda b: b["records"][0]["metrics"].update(trusted={"a": 1}), lambda b: b["records"][0].update(metrics="x"),
        lambda b: b.update(records="x"),
    ])
    def test_anything_off_contract_is_refused_without_echo(self, client, mutate) -> None:
        body = batch([record()])
        mutate(body)
        response = post(client, "/v1/records", body)
        assert response.status_code == 422 and rows(client) == []
        assert set(response.json()) == {"detail", "errors"} and "awful" not in response.text and "free text" not in response.text

    def test_one_bad_record_refuses_the_whole_batch(self, client) -> None:
        bad = record(1)
        bad["metrics"]["outcome"] = "exploded"
        assert post(client, "/v1/records", batch([record(0), bad])).status_code == 422 and rows(client) == []

    @pytest.mark.parametrize("raw", ['{"x": NaN}', "not json", "[]", "null"])
    def test_malformed_json(self, client, raw) -> None:
        response = client.post("/v1/records", content=raw, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        assert response.status_code == 422

    def test_nan_inside_metrics_is_refused(self, client) -> None:
        text = json.dumps(batch([record()])).replace('"latency_ms": 9000', '"latency_ms": NaN')
        response = client.post("/v1/records", content=text, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        assert response.status_code == 422 and rows(client) == []

    def test_nulls_and_booleans_where_the_contract_allows_them(self, client) -> None:
        r = record(event_type="daily_summary")
        r["metrics"].update(bot_mood=None, clean=False, progress=None)
        assert post(client, "/v1/records", batch([r])).status_code == 200


class TestIdempotencyAndErase:
    def test_a_retransmitted_batch_is_ignored(self, client) -> None:
        body = batch([record(i) for i in range(3)])
        assert post(client, "/v1/records", body).json() == {"stored": 3, "duplicates": 0}
        assert post(client, "/v1/records", body).json() == {"stored": 0, "duplicates": 3}
        assert len(rows(client)) == 3
        changed = batch([record(0, occurred_hour="2026-11-03T10:00:00Z")])
        assert post(client, "/v1/records", changed).json() == {"stored": 0, "duplicates": 1}
        assert rows(client, "SELECT occurred_hour FROM records WHERE id = '" + "0" * 32 + "'") == [("2026-11-03T09:00:00Z",)]

    def test_senders_are_isolated_and_stored_only_as_a_token_hash(self, client) -> None:
        post(client, "/v1/records", batch([record(0)]))
        post(client, "/v1/records", batch([record(0)]), token=OTHER)
        senders = {r[0] for r in rows(client)}
        assert len(rows(client)) == 2 and len(senders) == 2 and all(len(s) == 16 and TOKEN not in s and OTHER not in s for s in senders)
        conn = sqlite3.connect(client.db_path)
        try:
            dump = "\n".join(conn.iterdump())
        finally:
            conn.close()
        assert TOKEN not in dump and OTHER not in dump

    def test_erase_removes_the_subject_of_this_sender_only_and_is_idempotent(self, client) -> None:
        other_subject = "2" * 64
        post(client, "/v1/records", batch([record(0), record(1), record(2, subject=other_subject)]))
        post(client, "/v1/records", batch([record(0)]), token=OTHER)
        response = post(client, "/v1/erase", erase_body())
        assert response.status_code == 200 and response.json() == {"erased": 2, "already_done": False}
        assert sorted(r[2] for r in rows(client)) == sorted([other_subject, SUBJECT])          # the other sender's copy stays
        assert post(client, "/v1/erase", erase_body()).json() == {"erased": 0, "already_done": True}
        assert post(client, "/v1/erase", erase_body(), token=OTHER).json() == {"erased": 1, "already_done": False}
        assert [r[2] for r in rows(client)] == [other_subject]

    def test_after_an_erase_new_consented_records_are_accepted(self, client) -> None:
        post(client, "/v1/records", batch([record(0)]))
        post(client, "/v1/erase", erase_body())
        assert post(client, "/v1/records", batch([record(5)])).json() == {"stored": 1, "duplicates": 0}

    @pytest.mark.parametrize("mutate", [lambda b: b["erase"].update(subject="x"), lambda b: b["erase"].update(id="E" * 32), lambda b: b.pop("erase"),
                                        lambda b: b["erase"].update(extra=1), lambda b: b.update(schema="x")])
    def test_invalid_erase(self, client, mutate) -> None:
        body = erase_body()
        mutate(body)
        assert post(client, "/v1/erase", body).status_code == 422

    def test_erase_needs_the_token(self, client) -> None:
        post(client, "/v1/records", batch([record(0)]))
        assert post(client, "/v1/erase", erase_body(), token=None).status_code == 401 and len(rows(client)) == 1


class TestRetentionSettingsAndLogs:
    def test_old_records_and_receipts_are_pruned(self, client, clock) -> None:
        post(client, "/v1/records", batch([record(0)]))
        post(client, "/v1/erase", erase_body("d" * 32, subject="3" * 64))
        clock.advance(days=366)
        post(client, "/v1/records", batch([record(1)]))
        assert [r[1] for r in rows(client)] == ["0" * 31 + "1"] and rows(client, "SELECT * FROM erasures") == []

    def test_settings(self) -> None:
        with pytest.raises(ValueError):
            Settings(db_path="x", tokens=())
        with pytest.raises(ValueError):
            Settings(db_path="x", tokens=("short",))
        s = settings_from_env({"TELEMETRY_TOKEN": TOKEN, "TELEMETRY_TOKENS": f"{OTHER}, {TOKEN}", "RECEIVER_DB_PATH": "h.sqlite"})
        assert sorted(s.tokens) == sorted([TOKEN, OTHER]) and str(s.db_path) == "h.sqlite" and s.retention_days == 365
        assert TOKEN not in repr(s) and OTHER not in repr(s)

    def test_rate_limiter_refills(self) -> None:
        t = [0.0]
        limiter = RateLimiter(2, monotonic=lambda: t[0])
        assert [limiter.allow("k")[0] for _ in range(3)] == [True, True, False]
        t[0] += 30
        assert limiter.allow("k")[0] and not limiter.allow("k")[0] and limiter.allow("other")[0]

    def test_logs_carry_counts_only(self, client, caplog) -> None:
        with caplog.at_level(logging.DEBUG):
            post(client, "/v1/records", batch([record(0)]))
            post(client, "/v1/erase", erase_body())
            post(client, "/v1/records", batch([record(1)]), token="z" * 40)
        assert TOKEN not in caplog.text and SUBJECT not in caplog.text and "structured" not in caplog.text and "record(s) stored" in caplog.text
