"""
behavioral_telemetry/research.py

The opt-in research export (v0.39 Phase G4; tables `research_preferences`, `research_erase_requests`, migration 040). See
docs/architecture/phase_g_proposal.md section 6 (G4).

Consent (philosophy 2.5): a record is exported only while BOTH the owner's ENABLE_RESEARCH_TELEMETRY and the wearer's own `research on` are set, and only
records that occurred after that `research on` (consent is not retroactive). `research off`, and `forget all` (which also switches research off), queue an erase
request for the receiver. Erase requests are sent whenever the link is configured, even with ENABLE_RESEARCH_TELEMETRY off, so a revocation is never stranded.

Payload (`build_record`): an allowlist per event type. Every string must be one of a fixed set of enum values and every number finite, so no free text can
leave. The person is the subject pseudonym HMAC-SHA256(TELEMETRY_HMAC_KEY, "<tenant id>:<user id>"); the record id is an HMAC of the local row id (stable, so a
retry is idempotent at the receiver, and unlinkable to the local database); timestamps are rounded down to the hour; the conditioning factor leaves only as a
0.10 band. No tenant id, user id, local date or row id is ever sent.

Sending (`TelemetryLink`): HTTPS only (TLS verification is never disabled), a bearer token, batches of at most MAX_BATCH records and MAX_PAYLOAD_BYTES, connect
and read timeouts, HTTP 200 as the only success (only then `synced_at` is set or the erase marked sent), exponential backoff after a failure, one warning when
the link goes down and one info line when it recovers. It runs in its own background task (`research_export_loop`), never in the reply path or the periodic
cycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

from behavioral_telemetry.store import RETENTION_DAYS
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

logger = logging.getLogger("ai_coach_keyholder.behavioral_telemetry.research")

__all__ = ["ResearchPreferences", "ResearchExporter", "TelemetryLink", "build_record", "factor_band", "subject_pseudonym", "instance_pseudonym",
           "research_export_loop", "valid_endpoint", "SCHEMA", "MAX_BATCH", "MAX_PAYLOAD_BYTES", "BACKOFF_START", "BACKOFF_MAX", "RESEARCH_CONSENT_TARGET",
           "RESEARCH_EXPLANATION"]

SCHEMA = "hive-telemetry/1"
MAX_BATCH = 200
MAX_PAYLOAD_BYTES = 256 * 1024
BACKOFF_START = timedelta(minutes=1)
BACKOFF_MAX = timedelta(hours=1)
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 20.0
RESEARCH_CONSENT_TARGET = "research_telemetry"

RESEARCH_EXPLANATION = (
    "Research sharing sends pseudonymized numeric telemetry to the bot owner's research server, to study patterns across community instances: check-in "
    "and chat response times, token use, daily results, and Progressive Conditioning only as a coarse band. You are identified only by a keyed pseudonym; "
    "no names, ids, messages or other text are sent, and times are rounded to the hour. Only records made after you switch it on are sent. "
    "`research off` stops it at once and asks the server to erase everything sent under your pseudonym; `forget all` does the same.")

_BANDS = ("1.00-1.10", "1.10-1.20", "1.20-1.30", "1.30-1.40", "1.40-1.50")

# key -> allowed enum values (strings) or None for numbers / booleans only
_STATUS = frozenset({"answered", "answered_uncertain", "insufficient", "missed"})
_VERDICT = frozenset({"SUFFICIENT", "INSUFFICIENT", "UNCERTAIN", "NOT_EVALUATED"})
ALLOWLIST: dict[str, dict[str, frozenset | None]] = {
    "response_delay": {"level": None, "status": _STATUS, "verdict": _VERDICT, "delay_seconds": None, "window_seconds": None},
    "token_burn": {"amount": None, "balance_after": None, "hours_since_last_earn": None},
    "inflation_resilience": {"outcome": frozenset({"increased", "capped", "not_token", "wearer_off", "owner_off"}), "training_inflation_band": frozenset(_BANDS),
                             "days_since_previous_increment": None, "progress_avg_7d": None, "clean_days_7d": None},
    "daily_summary": {"goals_assigned": None, "goals_met": None, "check_ins_due": None, "check_ins_ok": None, "violations": None, "clean": None,
                      "streak_after": None, "tokens_earned": None, "progress": None, "pool_after": None, "trust_used": None,
                      "training_inflation_band": frozenset(_BANDS), "operating_mode": frozenset({"standard", "advanced"}),
                      "bot_mood": frozenset({"strict", "demanding", "approving", "neutral"})},
    "llm_generation": {"latency_ms": None, "path": frozenset({"plain", "structured", "tools", "context"}),
                       "outcome": frozenset({"ok", "generation_failed", "validation_failed"}),
                       "structured_status": frozenset({"ok", "side_dropped", "unparseable"}), "trusted": None},
    "conversation_gap": {"seconds_since_last_reply": None},
}


def _hmac_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def subject_pseudonym(key: str, tenant_id: str, user_id: str) -> str:
    return _hmac_hex(key, f"subject:{tenant_id}:{user_id}")


def instance_pseudonym(key: str) -> str:
    return _hmac_hex(key, "instance")


def factor_band(value) -> str | None:
    """1.000 .. 1.500 -> "1.00-1.10" .. "1.40-1.50" (the cap 1.50 belongs to the last band). None for anything else."""
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    if not d.is_finite() or d < 1 or d > Decimal("1.5"):
        return None
    return _BANDS[min(int((d - 1) / Decimal("0.10")), len(_BANDS) - 1)]


def _hour(at: datetime) -> str:
    return _iso(at.replace(minute=0, second=0, microsecond=0))


def _clean_value(value, allowed: frozenset | None):
    if allowed is not None:
        return value if isinstance(value, str) and value in allowed else None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return round(value, 4)
    return None


def build_record(*, key: str, tenant_id: str, row_id: str, user_id: str, occurred_at: datetime, event_type: str, metrics: dict) -> dict | None:
    """The exported form of one local record, or None when its type is not exportable. Only allowlisted keys; anything else is dropped."""
    allowed = ALLOWLIST.get(event_type)
    if allowed is None or not isinstance(metrics, dict):
        return None
    source = dict(metrics)
    if "training_inflation_factor" in source:
        source["training_inflation_band"] = factor_band(source.pop("training_inflation_factor"))
    clean = {k: _clean_value(source.get(k), spec) for k, spec in allowed.items()}
    return {"id": _hmac_hex(key, f"record:{tenant_id}:{row_id}")[:32], "subject": subject_pseudonym(key, tenant_id, user_id), "event_type": event_type,
            "occurred_hour": _hour(occurred_at), "metrics": clean}


def valid_endpoint(url: str | None) -> bool:
    """HTTPS with a host and no credentials in the URL."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None


# =====================================================================================================================================
# The wearer's switch
# =====================================================================================================================================

class ResearchPreferences:
    """`research on|off|status`. No row = off. Every change is recorded in consent_log with the text that was shown."""

    explanation = RESEARCH_EXPLANATION

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, export_active: bool = False) -> None:
        """`export_active`: the owner's ENABLE_RESEARCH_TELEMETRY is on and the link is configured (shown to the wearer, changes nothing here)."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self.export_active = export_active

    def is_enabled(self, user_id: str) -> bool:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT enabled FROM research_preferences WHERE user_id = ?", (user_id,))
        return row is not None and bool(row["enabled"])

    def enable(self, user_id: str, *, now: datetime) -> bool:
        """False when it was already on."""
        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT enabled FROM research_preferences WHERE user_id = ?", (user_id,))
            if row is not None and row["enabled"]:
                return False
            tx.execute("INSERT INTO research_preferences (user_id, enabled, enabled_at, updated_at) VALUES (?, 1, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET enabled = 1, enabled_at = excluded.enabled_at, updated_at = excluded.updated_at",
                       (user_id, _iso(now), _iso(now)))
            self._consent(tx, "approved", "research on", now)
            return True

        return apply_transition(self._core, write=write)

    def revoke(self, user_id: str, *, now: datetime, reason: str = "research off") -> bool:
        """Switches research off and queues an erase request. False (nothing queued) when the user never had it on or it is already off."""
        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT enabled FROM research_preferences WHERE user_id = ?", (user_id,))
            if row is None or not row["enabled"]:
                return False
            tx.execute("UPDATE research_preferences SET enabled = 0, updated_at = ? WHERE user_id = ?", (_iso(now), user_id))
            tx.execute("INSERT INTO research_erase_requests (id, user_id, created_at) VALUES (?, ?, ?)", (str(uuid.uuid4()), user_id, _iso(now)))
            self._consent(tx, "revoked", reason, now)
            return True

        return apply_transition(self._core, write=write)

    @staticmethod
    def _consent(tx: Transaction, action: str, comment: str, now: datetime) -> None:
        tx.execute("INSERT INTO consent_log (id, created_at, target_type, target_id, action, explanation_shown, user_comment) VALUES (?, ?, 'integration', ?, ?, ?, ?)",
                   (str(uuid.uuid4()), _iso(now), RESEARCH_CONSENT_TARGET, action, RESEARCH_EXPLANATION, comment))


# =====================================================================================================================================
# One database's queue
# =====================================================================================================================================

class ResearchExporter:
    def __init__(self, core: CoreDatabase, *, tenant_id: str, key: str, export_records: bool) -> None:
        """`export_records` = ENABLE_RESEARCH_TELEMETRY; erase requests are sent regardless."""
        self._core = core
        self.tenant_id = tenant_id
        self._key = key
        self._export_records = export_records

    def pending_erases(self) -> list[tuple[str, dict]]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT id, user_id FROM research_erase_requests WHERE sent_at IS NULL ORDER BY created_at, id")
        return [(r["id"], {"id": _hmac_hex(self._key, f"erase:{r['id']}")[:32], "subject": subject_pseudonym(self._key, self.tenant_id, r["user_id"])})
                for r in rows]

    def mark_erase_sent(self, erase_id: str, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE research_erase_requests SET sent_at = ? WHERE id = ? AND sent_at IS NULL", (_iso(now), erase_id))

        apply_transition(self._core, write=write)

    def pending_batch(self, now: datetime, limit: int = MAX_BATCH) -> list[tuple[str, dict | None]]:
        """(local row id, exported record or None when not exportable): unsynced records of users whose research is on, made after they switched it on,
        within retention, oldest first."""
        if not self._export_records:
            return []
        floor = _iso(now - timedelta(days=RETENTION_DAYS))
        with self._core.transaction() as tx:
            rows = tx.fetch_all(
                "SELECT t.id, t.user_id, t.occurred_at, t.event_type, t.metrics FROM behavioral_telemetry t "
                "JOIN research_preferences p ON p.user_id = t.user_id AND p.enabled = 1 "
                "WHERE t.synced_at IS NULL AND t.occurred_at >= p.enabled_at AND t.occurred_at >= ? ORDER BY t.occurred_at, t.id LIMIT ?",
                (floor, limit))
        out = []
        for r in rows:
            try:
                record = build_record(key=self._key, tenant_id=self.tenant_id, row_id=r["id"], user_id=r["user_id"], occurred_at=_parse_iso(r["occurred_at"]),
                                      event_type=r["event_type"], metrics=json.loads(r["metrics"]))
            except (ValueError, TypeError):
                record = None
            out.append((r["id"], record))          # a non-exportable row is marked synced too, so it never blocks the queue
        return out

    def mark_synced(self, row_ids: list[str], now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.executemany("UPDATE behavioral_telemetry SET synced_at = ? WHERE id = ? AND synced_at IS NULL", [(_iso(now), i) for i in row_ids])

        if row_ids:
            apply_transition(self._core, write=write)


# =====================================================================================================================================
# The link (one per bot: one endpoint, one backoff, one outage state)
# =====================================================================================================================================

def _requests_post(url: str, *, json_body: dict, headers: dict, timeout: tuple[float, float]) -> int:
    import requests

    # TLS verification is the library default and is never turned off here.
    response = requests.post(url, json=json_body, headers=headers, timeout=timeout, allow_redirects=False)
    response.close()
    return response.status_code


class TelemetryLink:
    def __init__(self, *, endpoint: str, token: str, key: str, post: Callable[..., int] = _requests_post) -> None:
        if not valid_endpoint(endpoint):
            raise ValueError("TELEMETRY_ENDPOINT must be an https:// URL")
        if not token or not key:
            raise ValueError("TELEMETRY_TOKEN and TELEMETRY_HMAC_KEY are required")
        self._base = endpoint.rstrip("/")
        self._token = token
        self._instance = instance_pseudonym(key)
        self._post = post
        self.down_since: datetime | None = None
        self.failures = 0
        self.next_attempt_at: datetime | None = None

    def push(self, exporters: Iterable[ResearchExporter], now: datetime) -> bool:
        """One pass: every database's erase requests first, then its records in batches. Stops at the first failure (backoff). False when skipped or failed."""
        if self.next_attempt_at is not None and now < self.next_attempt_at:
            return False
        for exporter in exporters:
            for erase_id, erase in exporter.pending_erases():
                if not self._send("erase", {"schema": SCHEMA, "instance": self._instance, "erase": erase}, now):
                    return False
                exporter.mark_erase_sent(erase_id, now)
            while True:
                fetched = exporter.pending_batch(now)
                if not fetched:
                    break
                pairs = self._fit(fetched)
                records = [r for _, r in pairs if r is not None]
                if records and not self._send("records", {"schema": SCHEMA, "instance": self._instance, "records": records}, now):
                    return False
                exporter.mark_synced([i for i, _ in pairs], now)       # only what was actually sent (or was not exportable)
                if len(fetched) < MAX_BATCH and len(pairs) == len(fetched):
                    break
        return True

    @staticmethod
    def _fit(pairs: list[tuple[str, dict | None]]) -> list[tuple[str, dict | None]]:
        """Halves the batch until the JSON body fits MAX_PAYLOAD_BYTES (the rest goes in the next request)."""
        while len(pairs) > 1 and len(json.dumps([r for _, r in pairs if r is not None])) > MAX_PAYLOAD_BYTES:
            pairs = pairs[: len(pairs) // 2]
        return pairs

    def _send(self, path: str, body: dict, now: datetime) -> bool:
        try:
            status = self._post(f"{self._base}/v1/{path}", json_body=body, headers={"Authorization": f"Bearer {self._token}"},
                                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS))
            reason = None if status == 200 else f"HTTP {status}"
        except Exception as exc:                                          # connection refused, TLS failure, timeout ...
            reason = type(exc).__name__
        if reason is None:
            if self.down_since is not None:
                logger.info("Research telemetry link recovered after %d failed attempt(s).", self.failures)
            self.down_since, self.failures, self.next_attempt_at = None, 0, None
            return True
        if self.down_since is None:
            self.down_since = now
            logger.warning("Research telemetry link is down (%s); retrying with backoff. Nothing is lost.", reason)
        self.failures += 1
        delay = min(BACKOFF_MAX, BACKOFF_START * (2 ** min(self.failures - 1, 16)))
        self.next_attempt_at = now + delay
        return False


async def research_export_loop(link: TelemetryLink, exporters: Callable[[], list[ResearchExporter]], clock, *, interval_seconds: float = 60.0) -> None:
    """The background task: one push per interval in a worker thread (never on the event loop, never in the reply path)."""
    while True:
        try:
            await asyncio.to_thread(link.push, exporters(), clock.now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Research telemetry export pass failed; will retry.")
        await asyncio.sleep(interval_seconds)
