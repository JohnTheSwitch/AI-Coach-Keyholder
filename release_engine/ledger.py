"""
release_engine/ledger.py

The append-only token ledger (activity_authorization_technical_design.md Section 5).
Tokens are a HIDDEN mechanism (DEC-5): nothing here is ever exposed to the user or
a model. Balance is always SUM(delta) (LEDGER2). There is no UPDATE/DELETE path
(LEDGER1); a mistake is undone by a compensating REVERSAL (5.3). All writes go
through the Database transaction, which is BEGIN IMMEDIATE, so balance reads and
SPEND writes in one transaction cannot interleave with another writer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from infrastructure.database import Transaction
from infrastructure.time_format import iso as _iso

__all__ = ["LedgerEntryType", "balance", "insert_entry", "reverse_entry"]


class LedgerEntryType(StrEnum):
    EARN = "earn"
    SPEND = "spend"
    ADJUSTMENT = "adjustment"
    REVERSAL = "reversal"


def balance(tx: Transaction, user_id: str) -> int:
    row = tx.fetch_one("SELECT COALESCE(SUM(delta), 0) AS b FROM token_ledger WHERE user_id = ?", (user_id,))
    return int(row["b"])


def insert_entry(
    tx: Transaction, *, user_id: str, delta: int, entry_type: LedgerEntryType, reason_code: str, now: datetime,
    authorization_decision_id: str | None = None, source_event_id: str | None = None,
    reverses_entry_id: str | None = None,
) -> str:
    if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
        raise ValueError("delta must be a non-zero int")
    if entry_type is LedgerEntryType.EARN and delta < 0:
        raise ValueError("an EARN entry must be positive")
    if entry_type is LedgerEntryType.SPEND and delta > 0:
        raise ValueError("a SPEND entry must be negative")
    if not reason_code or not reason_code.strip():
        raise ValueError("reason_code must be non-empty")
    entry_id = str(uuid.uuid4())
    tx.execute(
        "INSERT INTO token_ledger (id, user_id, delta, entry_type, reason_code, authorization_decision_id, "
        "source_event_id, reverses_entry_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, user_id, delta, entry_type.value, reason_code, authorization_decision_id, source_event_id,
         reverses_entry_id, _iso(now)),
    )
    return entry_id


def reverse_entry(tx: Transaction, *, original_entry_id: str, reason_code: str, now: datetime) -> str | None:
    """Appends the compensating row. Returns None if the entry was already reversed
    (idempotent); raises LookupError for an unknown entry."""
    row = tx.fetch_one("SELECT * FROM token_ledger WHERE id = ?", (original_entry_id,))
    if row is None:
        raise LookupError(f"no ledger entry {original_entry_id!r}")
    if row["entry_type"] == LedgerEntryType.REVERSAL.value:
        raise ValueError("a reversal cannot itself be reversed")
    if tx.fetch_one("SELECT 1 FROM token_ledger WHERE reverses_entry_id = ?", (original_entry_id,)):
        return None
    return insert_entry(
        tx, user_id=row["user_id"], delta=-int(row["delta"]), entry_type=LedgerEntryType.REVERSAL,
        reason_code=reason_code, now=now, reverses_entry_id=original_entry_id,
    )
