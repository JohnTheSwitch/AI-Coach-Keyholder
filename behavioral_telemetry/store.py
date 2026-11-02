"""
behavioral_telemetry/store.py

The local Behavioral Telemetry table (v0.39 Phase G1; tables `behavioral_telemetry`, `telemetry_watermarks`, migration 039). See
docs/architecture/phase_g_proposal.md.

Rows are DERIVED facts (written only by `behavioral_telemetry.collector`): numbers, booleans and short enum strings, never free text. `record_in` enforces that in
code, so a check-in answer, a message or a prompt can never be stored here. Rows live in this database only; the opt-in export (G4) is not built yet.

Consent (philosophy 2.5): in a tenant database the collector records only when the accepted invitation disclosed it (`disclosed_in`); tenant zero is the owner's
own database. The wearer sees what exists with `telemetry` (`summary`) and erases it with `forget all` (`forget_all`). Nothing that builds model context may import
this package; tests guard that. The conditioning factor stored in `metrics` is for the owner only: `summary` never returns metric values.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

__all__ = ["TelemetryStore", "TelemetrySummary", "EVENT_TYPES", "RETENTION_DAYS", "DISCLOSURE_MARKER", "TELEMETRY_DISCLOSURE", "record_in",
           "disclosed_in"]

EVENT_TYPES = ("response_delay", "token_burn", "inflation_resilience", "daily_summary",
               "llm_generation", "conversation_gap")          # the last two come from the runtime buffer (Phase G2)
RETENTION_DAYS = 180

# The invitation text (system/tenant_gateway.py) includes TELEMETRY_DISCLOSURE; a tenant database is recorded only when its accepted consent text contains the marker.
DISCLOSURE_MARKER = "behavioral telemetry"
TELEMETRY_DISCLOSURE = (
    f"It also derives {DISCLOSURE_MARKER} from your records (check-in and chat response times, token use, daily results) for your keyholder's private analysis; "
    f"it stays in your own records for {RETENTION_DAYS} days, you can see it with `telemetry` and erase it with `forget all`, "
    "and nothing is sent anywhere unless you separately opt in.")

_ENUM_TEXT = re.compile(r"[A-Za-z0-9_.:-]{0,32}")


def _valid_metrics(metrics: dict) -> bool:
    if not isinstance(metrics, dict) or len(metrics) > 24:
        return False
    for key, value in metrics.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", key):
            return False
        if value is None or isinstance(value, (bool, int, float)):
            continue
        if not isinstance(value, str) or not _ENUM_TEXT.fullmatch(value):
            return False
    return True


def record_in(tx: Transaction, *, user_id: str, occurred_at: datetime, event_type: str, metrics: dict, source_key: str, now: datetime) -> bool:
    """Stores one derived fact; False when this source row was already recorded. Raises ValueError for an unknown type or anything that is not a short
    number / boolean / enum value (free text can never be stored)."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown telemetry event type {event_type!r}")
    if not _valid_metrics(metrics):
        raise ValueError("telemetry metrics must be numbers, booleans or short enum strings")
    cursor = tx.execute(
        "INSERT OR IGNORE INTO behavioral_telemetry (id, user_id, occurred_at, event_type, metrics, source_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, _iso(occurred_at), event_type, json.dumps(metrics, sort_keys=True), source_key, _iso(now)))
    return cursor.rowcount == 1


def disclosed_in(tx: Transaction, consent_target: str) -> bool:
    """True when an approved consent record for `consent_target` showed the telemetry disclosure."""
    rows = tx.fetch_all("SELECT explanation_shown FROM consent_log WHERE target_id = ? AND action = 'approved'", (consent_target,))
    return any(DISCLOSURE_MARKER in (r["explanation_shown"] or "") for r in rows)


@dataclass(frozen=True)
class TelemetrySummary:
    recording: bool
    counts: dict[str, int]
    oldest: datetime | None


class TelemetryStore:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, consent_target: str | None = None, buffer=None) -> None:
        """`consent_target`: in a tenant database, the consent_log target of the accepted invitation (recording needs its disclosure); None in tenant zero.
        `buffer`: the runtime buffer (Phase G2) of this database's conversation engine; `forget_all` also drops its undrained samples."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._consent_target = consent_target
        self._buffer = buffer

    @property
    def core(self) -> CoreDatabase:
        return self._core

    def is_recording(self) -> bool:
        if self._consent_target is None:
            return True
        with self._core.transaction() as tx:
            return disclosed_in(tx, self._consent_target)

    def recording_in(self, tx: Transaction) -> bool:
        return self._consent_target is None or disclosed_in(tx, self._consent_target)

    def summary(self, user_id: str) -> TelemetrySummary:
        """What the wearer may see: categories, counts and the oldest record. Never a metric value."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT event_type, COUNT(*) AS n, MIN(occurred_at) AS oldest FROM behavioral_telemetry WHERE user_id = ? GROUP BY event_type",
                                (user_id,))
            recording = self.recording_in(tx)
        oldest = min((r["oldest"] for r in rows), default=None)
        return TelemetrySummary(recording=recording, counts={r["event_type"]: int(r["n"]) for r in rows},
                                oldest=_parse_iso(oldest) if oldest else None)

    def forget_all(self, user_id: str) -> int:
        if self._buffer is not None:
            self._buffer.forget(user_id)                    # first, so a drain running concurrently drops this user's older samples

        def write(tx: Transaction, _s: object) -> int:
            return tx.execute("DELETE FROM behavioral_telemetry WHERE user_id = ?", (user_id,)).rowcount

        return apply_transition(self._core, write=write)

    def prune(self, now: datetime) -> int:
        """Deletes records older than the retention period."""
        cutoff = _iso(now - timedelta(days=RETENTION_DAYS))

        def write(tx: Transaction, _s: object) -> int:
            return tx.execute("DELETE FROM behavioral_telemetry WHERE occurred_at < ?", (cutoff,)).rowcount

        return apply_transition(self._core, write=write)
