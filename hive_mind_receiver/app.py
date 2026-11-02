"""
hive_mind_receiver/app.py

The standalone receiver of the opt-in research telemetry (`hive-telemetry/1`, see contract.py). FastAPI + SQLite; shares no code with the bot. Every sender
is untrusted:

- Guard (pure ASGI, BEFORE any body is read): only POST /v1/records and POST /v1/erase are accepted besides GET /healthz; a Bearer token from the configured
  set (constant-time comparison) or 401; failed authentications share one rate bucket (429 when exceeded); each sender has its own request bucket (429 with
  Retry-After); `Content-Type: application/json`; a `Content-Length` is required (411) and may not exceed the body limit (413); the body actually read may
  not exceed it either (413).
- Validation (Pydantic, `extra="forbid"`): the schema tag, 64-hex instance and subject, 32-hex ids, an event type of the contract, an hour timestamp, and
  `metrics` with EXACTLY the contract's keys: enum strings or null, finite numbers within +-1e12, booleans or null. Anything else: 422 with no echo of the
  input.
- Storage: records keyed by (sender, id), so a retransmitted record is ignored (idempotent); an erase deletes every record of that subject sent with the
  same token (another sender cannot erase or overwrite someone else's data) and is remembered by (sender, id), so its retry is harmless. Senders are stored
  as a SHA-256 prefix of their token, never the token. Records and erase receipts older than the retention period are deleted (checked at most hourly).
- No interactive documentation endpoints, no server header, no request contents in logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from hive_mind_receiver import contract
from hive_mind_receiver.clock import system_now

logger = logging.getLogger("hive_mind_receiver")

__all__ = ["Settings", "settings_from_env", "create_app", "create_app_from_env", "RateLimiter", "Store"]

_HEX32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
_HEX64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_HOUR = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:00:00Z$")]
_EVENT_TYPES = tuple(contract.METRICS)
_PATHS = ("/v1/records", "/v1/erase")
MIN_TOKEN_LENGTH = 24


# ======================================================================================================================================
# Settings
# ======================================================================================================================================

@dataclass(frozen=True)
class Settings:
    db_path: Path
    tokens: tuple[str, ...]
    max_body_bytes: int = contract.MAX_BODY_BYTES
    requests_per_minute: int = 120
    auth_failures_per_minute: int = 30
    retention_days: int = 365

    def __post_init__(self) -> None:
        if not self.tokens or any(len(t) < MIN_TOKEN_LENGTH for t in self.tokens):
            raise ValueError(f"at least one token is required and every token needs {MIN_TOKEN_LENGTH}+ characters")
        if self.max_body_bytes < 1024 or self.requests_per_minute < 1 or self.auth_failures_per_minute < 1 or self.retention_days < 1:
            raise ValueError("invalid limits")

    def __repr__(self) -> str:                                     # never print the tokens
        return f"Settings(db_path={self.db_path!s}, tokens=<{len(self.tokens)}>, max_body_bytes={self.max_body_bytes})"


def settings_from_env(env=None) -> Settings:
    """RECEIVER_DB_PATH, TELEMETRY_TOKEN and/or TELEMETRY_TOKENS (comma-separated, one per sending instance), RECEIVER_MAX_BODY_BYTES,
    RECEIVER_REQUESTS_PER_MINUTE, RECEIVER_AUTH_FAILURES_PER_MINUTE, RECEIVER_RETENTION_DAYS."""
    env = os.environ if env is None else env
    tokens = [t.strip() for t in (env.get("TELEMETRY_TOKENS") or "").split(",") if t.strip()]
    if (env.get("TELEMETRY_TOKEN") or "").strip():
        tokens.append(env["TELEMETRY_TOKEN"].strip())
    return Settings(
        db_path=Path(env.get("RECEIVER_DB_PATH") or "hive_mind.sqlite"), tokens=tuple(dict.fromkeys(tokens)),
        max_body_bytes=int(env.get("RECEIVER_MAX_BODY_BYTES") or contract.MAX_BODY_BYTES),
        requests_per_minute=int(env.get("RECEIVER_REQUESTS_PER_MINUTE") or 120),
        auth_failures_per_minute=int(env.get("RECEIVER_AUTH_FAILURES_PER_MINUTE") or 30),
        retention_days=int(env.get("RECEIVER_RETENTION_DAYS") or 365))


# ======================================================================================================================================
# Validation
# ======================================================================================================================================

class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: _HEX32
    subject: _HEX64
    event_type: Literal[_EVENT_TYPES]  # type: ignore[valid-type]
    occurred_hour: _HOUR
    metrics: dict[str, Any]

    @model_validator(mode="after")
    def _contract(self) -> "Record":
        datetime.strptime(self.occurred_hour, "%Y-%m-%dT%H:%M:%SZ")      # a real calendar hour
        contract.check_metrics(self.event_type, self.metrics)
        return self


class RecordsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_tag: Literal[contract.SCHEMA] = Field(alias="schema")       # type: ignore[valid-type]
    instance: _HEX64
    records: list[Record] = Field(min_length=1, max_length=contract.MAX_RECORDS_PER_BATCH)


class EraseItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: _HEX32
    subject: _HEX64


class EraseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_tag: Literal[contract.SCHEMA] = Field(alias="schema")       # type: ignore[valid-type]
    instance: _HEX64
    erase: EraseItem


# ======================================================================================================================================
# Rate limits (in memory, per process)
# ======================================================================================================================================

class RateLimiter:
    """A token bucket per key: `per_minute` requests, refilled continuously."""

    def __init__(self, per_minute: int, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._monotonic = monotonic
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """(allowed, seconds until the next request would be allowed)."""
        with self._lock:
            now = self._monotonic()
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return True, 0
            self._buckets[key] = (tokens, now)
            return False, max(1, math.ceil((1 - tokens) / self._rate))


# ======================================================================================================================================
# Storage
# ======================================================================================================================================

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS records (
    sender        TEXT NOT NULL,
    id            TEXT NOT NULL,
    instance      TEXT NOT NULL,
    subject       TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    occurred_hour TEXT NOT NULL,
    metrics       TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    PRIMARY KEY (sender, id)
);
CREATE INDEX IF NOT EXISTS idx_records_subject ON records(sender, subject);
CREATE INDEX IF NOT EXISTS idx_records_received ON records(received_at);
CREATE TABLE IF NOT EXISTS erasures (
    sender      TEXT NOT NULL,
    id          TEXT NOT NULL,
    subject     TEXT NOT NULL,
    erased      INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (sender, id)
);
CREATE INDEX IF NOT EXISTS idx_erasures_received ON erasures(received_at);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA_SQL)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def insert_records(self, sender: str, instance: str, records: list[Record], now: datetime) -> tuple[int, int]:
        """(stored, duplicates). One transaction for the whole batch."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            stored = 0
            for r in records:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO records (sender, id, instance, subject, event_type, occurred_hour, metrics, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (sender, r.id, instance, r.subject, r.event_type, r.occurred_hour, json.dumps(r.metrics, sort_keys=True), _iso(now)))
                stored += cur.rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return stored, len(records) - stored

    def erase(self, sender: str, erase_id: str, subject: str, now: datetime) -> tuple[int, bool]:
        """(records deleted, True when this erase id was already done)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            done = conn.execute("SELECT erased FROM erasures WHERE sender = ? AND id = ?", (sender, erase_id)).fetchone()
            if done is not None:
                conn.execute("COMMIT")
                return 0, True
            erased = conn.execute("DELETE FROM records WHERE sender = ? AND subject = ?", (sender, subject)).rowcount
            conn.execute("INSERT INTO erasures (sender, id, subject, erased, received_at) VALUES (?, ?, ?, ?, ?)",
                         (sender, erase_id, subject, erased, _iso(now)))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return erased, False

    def prune(self, now: datetime, retention_days: int) -> int:
        cutoff = _iso(now - timedelta(days=retention_days))
        conn = self._connect()
        try:
            removed = conn.execute("DELETE FROM records WHERE received_at < ?", (cutoff,)).rowcount
            conn.execute("DELETE FROM erasures WHERE received_at < ?", (cutoff,))
        finally:
            conn.close()
        return removed


# ======================================================================================================================================
# The ASGI guard
# ======================================================================================================================================

async def _reply(send, status: int, body: dict, headers: list[tuple[bytes, bytes]] = ()) -> None:
    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), *headers]})
    await send({"type": "http.response.body", "body": payload})


class Guard:
    def __init__(self, app, *, settings: Settings, monotonic: Callable[[], float]) -> None:
        self.app = app
        self._settings = settings
        self._tokens = [(t.encode("utf-8"), hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]) for t in settings.tokens]
        self._per_sender = RateLimiter(settings.requests_per_minute, monotonic=monotonic)
        self._auth_failures = RateLimiter(settings.auth_failures_per_minute, monotonic=monotonic)

    def _sender(self, header: bytes | None) -> str | None:
        if not header or not header.startswith(b"Bearer "):
            return None
        offered = header[7:].strip()
        found = None
        for token, sender in self._tokens:                       # compare against every token: no early exit timing signal
            if hmac.compare_digest(offered, token):
                found = sender
        return found

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] not in _PATHS:
            await self.app(scope, receive, send)
            return
        if scope["method"] != "POST":
            await _reply(send, 405, {"detail": "method not allowed"}, [(b"allow", b"POST")])
            return
        headers = dict(scope["headers"])
        sender = self._sender(headers.get(b"authorization"))
        if sender is None:
            allowed, _ = self._auth_failures.allow("auth")
            await _reply(send, 401 if allowed else 429, {"detail": "unauthorized" if allowed else "too many requests"},
                         [(b"www-authenticate", b"Bearer")] if allowed else [(b"retry-after", b"60")])
            return
        allowed, retry = self._per_sender.allow(sender)
        if not allowed:
            await _reply(send, 429, {"detail": "too many requests"}, [(b"retry-after", str(retry).encode())])
            return
        if headers.get(b"content-type", b"").split(b";")[0].strip().lower() != b"application/json":
            await _reply(send, 415, {"detail": "application/json required"})
            return
        declared = headers.get(b"content-length")
        if declared is None:
            await _reply(send, 411, {"detail": "content-length required"})
            return
        try:
            length = int(declared)
        except ValueError:
            await _reply(send, 400, {"detail": "invalid content-length"})
            return
        limit = self._settings.max_body_bytes
        if length < 0 or length > limit:
            await _reply(send, 413, {"detail": "payload too large"})
            return

        chunks, read = [], 0                                     # read the (small, bounded) body here, then replay it to the application
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            read += len(body)
            if read > limit or read > length:
                await _reply(send, 413, {"detail": "payload too large"})
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

        async def replay_receive():
            if replay:
                return replay.pop()
            return await receive()

        scope.setdefault("state", {})["sender"] = sender
        await self.app(scope, replay_receive, send)


# ======================================================================================================================================
# The application
# ======================================================================================================================================

def create_app(settings: Settings, *, now: Callable[[], datetime] = system_now, monotonic: Callable[[], float] = time.monotonic) -> FastAPI:
    store = Store(settings.db_path)
    app = FastAPI(title="hive_mind_receiver", docs_url=None, redoc_url=None, openapi_url=None)
    pruned = {"at": None}
    prune_lock = threading.Lock()

    def maybe_prune(at: datetime) -> None:
        with prune_lock:
            if pruned["at"] is not None and at - pruned["at"] < timedelta(hours=1):
                return
            pruned["at"] = at
        removed = store.prune(at, settings.retention_days)
        if removed:
            logger.info("Retention: removed %d record(s) older than %d days.", removed, settings.retention_days)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "invalid payload", "errors": len(exc.errors())})

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/v1/records")
    def records(body: RecordsBody, request: Request) -> dict:
        at = now()
        maybe_prune(at)
        sender = request.scope["state"]["sender"]
        stored, duplicates = store.insert_records(sender, body.instance, body.records, at)
        logger.info("Sender %s: %d record(s) stored, %d duplicate(s) ignored.", sender[:8], stored, duplicates)
        return {"stored": stored, "duplicates": duplicates}

    @app.post("/v1/erase")
    def erase(body: EraseBody, request: Request) -> dict:
        at = now()
        sender = request.scope["state"]["sender"]
        erased, already = store.erase(sender, body.erase.id, body.erase.subject, at)
        logger.info("Sender %s: erase %s (%d record(s) deleted).", sender[:8], "repeated" if already else "done", erased)
        return {"erased": erased, "already_done": already}

    app.add_middleware(Guard, settings=settings, monotonic=monotonic)
    app.state.store = store
    return app


def create_app_from_env() -> FastAPI:
    """For `uvicorn --factory hive_mind_receiver.app:create_app_from_env`."""
    return create_app(settings_from_env())
