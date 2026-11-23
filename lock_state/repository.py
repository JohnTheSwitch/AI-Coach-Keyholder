"""
lock_state/repository.py

docs/architecture/lock_state_technical_design.md (draft, not approved
for implementation as a whole -- see lock_state/README.md for the
exact boundary this module implements).

Two structurally separate public classes, the same split
task_catalog/advanced_mode already established:

- `LockState` -- read-only (`get_current_report`, `get_current_knowledge_state`).
  No write method exists on this class.
- `LockStateAdministration` -- governed write API (`report_status`).
  Requires a non-empty `reported_via_consent_id`, the same audit-trail
  discipline every other governed write in this project already uses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.decision_log import decision
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from lock_state.models import LockKnowledgeState, LockReport, LockReportStatus

__all__ = ["LockState", "LockStateAdministration"]


def _new_id() -> str:
    return str(uuid.uuid4())


def _require_consent_id(consent_id: str) -> None:
    if not consent_id or not consent_id.strip():
        raise ValueError("reported_via_consent_id must be a non-empty string.")


def _require_user_id(user_id: str) -> None:
    if not user_id or not user_id.strip():
        raise ValueError("user_id must be a non-empty string.")


def _row_to_report(row) -> LockReport:
    return LockReport(
        id=row["id"], user_id=row["user_id"], status=LockReportStatus(row["status"]),
        sequence_number=row["sequence_number"], reported_at=_parse_iso(row["reported_at"]),
        reported_via_consent_id=row["reported_via_consent_id"],
    )


class LockState:
    """Read-only. No write method exists on this class at all."""

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def get_current_report(self, user_id: str) -> LockReport | None:
        """The most recent report for this user, ordered deterministically
        by `sequence_number` -- never relying on timestamp precision or
        SQLite's own implicit row order. `None` if no report exists yet
        (never a fabricated "unknown" row)."""
        _require_user_id(user_id)
        with self._core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT * FROM lock_reports WHERE user_id = ? ORDER BY sequence_number DESC LIMIT 1",
                (user_id,),
            )
        return _row_to_report(row) if row is not None else None

    def get_current_knowledge_state(self, user_id: str) -> LockKnowledgeState:
        """The primary read method for callers that only need the
        three-value epistemic state, not the full report record.
        Absence of any report maps to UNKNOWN -- never to
        UNLOCKED_USER_REPORTED."""
        report = self.get_current_report(user_id)
        if report is None:
            return LockKnowledgeState.UNKNOWN
        return LockKnowledgeState(report.status.value)


class LockStateAdministration:
    """Governed write API. `report_status()` is the only way a row is
    ever written to `lock_reports` -- always an INSERT, never an
    UPDATE or DELETE (append-only, migration 019's own invariant)."""

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def report_status(
        self, *, user_id: str, status: LockReportStatus, reported_via_consent_id: str, now: datetime,
    ) -> LockReport:
        """A single new report row -- the whole exchange rolls back
        atomically (`apply_transition`'s own transactional guarantee)
        if anything inside `write` raises, so a failed call never
        leaves a partial or inconsistent row behind."""
        _require_user_id(user_id)
        _require_consent_id(reported_via_consent_id)

        def write(tx: Transaction, _state: object) -> LockReport:
            existing_max = tx.fetch_one(
                "SELECT COALESCE(MAX(sequence_number), 0) AS max_sequence FROM lock_reports WHERE user_id = ?",
                (user_id,),
            )
            next_sequence = existing_max["max_sequence"] + 1

            report = LockReport(
                id=_new_id(), user_id=user_id, status=status, sequence_number=next_sequence,
                reported_at=now, reported_via_consent_id=reported_via_consent_id,
            )
            tx.execute(
                """
                INSERT INTO lock_reports
                    (id, user_id, status, sequence_number, reported_at, reported_via_consent_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report.id, report.user_id, report.status.value, report.sequence_number,
                    _iso(report.reported_at), report.reported_via_consent_id,
                ),
            )
            return report

        report = apply_transition(self._core, write=write)
        decision("lock_state", "The wearer reported his lock state.", status=report.status.value, sequence=report.sequence_number)
        return report
