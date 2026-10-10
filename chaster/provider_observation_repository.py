"""
chaster/provider_observation_repository.py

docs/architecture/chaster_integration_technical_design.md, Section
25a Milestone D / Section 26 (CHASTER-01B Increment 2). Two
structurally separate public classes, the same read/write split
`lock_state/repository.py` already established for the same reason:

- `ChasterProviderObservations` -- read-only (`get_latest`). No write
  method exists on this class.
- `ChasterProviderObservationRecorder` -- the only way a row is ever
  written to `chaster_provider_observations` -- always an INSERT,
  never an UPDATE or DELETE (append-only, migration 023's own
  invariant, mirroring migration 019's `lock_reports`).

Deliberately NOT a general public write API a Discord command could
call directly -- CHASTER-01B's own on-demand fetch service (a later
increment) is the only intended caller of the recorder, since a
"provider observation" is a machine-fetched fact, not a user-supplied
one (no consent-id concept applies here the way it does for
`lock_state`'s own governed write).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from chaster.models import ChasterProviderObservation, ChasterProviderStatus
from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

__all__ = ["ChasterProviderObservations", "ChasterProviderObservationRecorder"]


def _new_id() -> str:
    return str(uuid.uuid4())


def _require_connection_id(connection_id: str) -> None:
    if not connection_id or not connection_id.strip():
        raise ValueError("connection_id must be a non-empty string.")


def _row_to_observation(row) -> ChasterProviderObservation:
    return ChasterProviderObservation(
        id=row["id"], connection_id=row["connection_id"], provider=row["provider"],
        chaster_lock_id=row["chaster_lock_id"], status=ChasterProviderStatus(row["status"]),
        fetched_at=_parse_iso(row["fetched_at"]), created_at=_parse_iso(row["created_at"]),
        is_frozen=None if row["is_frozen"] is None else bool(row["is_frozen"]),
        has_keyholder=None if row["has_keyholder"] is None else bool(row["has_keyholder"]),
        end_date=_parse_iso(row["end_date"]) if row["end_date"] else None,
        frozen_at=_parse_iso(row["frozen_at"]) if row["frozen_at"] else None,
    )


class ChasterProviderObservations:
    """Read-only. No write method exists on this class at all."""

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def get_latest(self, connection_id: str) -> ChasterProviderObservation | None:
        """The most recent observation for this connection, ordered by
        `fetched_at` deterministically -- `rowid DESC` is used as an
        explicit tiebreaker for the (rare, e.g. under a frozen test
        clock) case of two observations sharing the same `fetched_at`
        value, the same reason `lock_state`'s own
        `LockState.get_current_report()` orders by an explicit
        monotonic column rather than trusting timestamp precision
        alone. `None` if no observation exists yet (never a
        fabricated "no locks" row) -- this is the provider-observation
        equivalent of `LockKnowledgeState.UNKNOWN`'s own "no
        trustworthy report exists" framing (chaster_integration_technical_design.md
        Section 14, "No observation")."""
        _require_connection_id(connection_id)
        with self._core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT * FROM chaster_provider_observations "
                "WHERE connection_id = ? ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
                (connection_id,),
            )
        return _row_to_observation(row) if row is not None else None


class ChasterProviderObservationRecorder:
    """The only way a row is ever written to
    `chaster_provider_observations`. Always an INSERT -- append-only,
    migration 023's own invariant. Not a general public write API --
    see this module's own header docstring."""

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def record(
        self, *, connection_id: str, provider: str, chaster_lock_id: str | None,
        status: ChasterProviderStatus, fetched_at: datetime, now: datetime,
        is_frozen: bool | None = None, has_keyholder: bool | None = None, end_date: datetime | None = None,
        frozen_at: datetime | None = None,
    ) -> ChasterProviderObservation:
        """Inserts one new observation row. `fetched_at` is when the
        real Chaster API call actually completed; `now` (the injected
        Clock's own value, per this project's own established
        convention) is this row's own `created_at` -- the two are
        conceptually distinct even though they will typically be
        equal or near-equal in practice, matching the same
        `reported_at` vs. row-creation-time distinction
        `lock_state.repository.LockStateAdministration.report_status()`
        already preserves for user reports."""
        _require_connection_id(connection_id)
        if not provider or not provider.strip():
            raise ValueError("provider must be a non-empty string.")

        observation = ChasterProviderObservation(
            id=_new_id(), connection_id=connection_id, provider=provider,
            chaster_lock_id=chaster_lock_id, status=status, fetched_at=fetched_at, created_at=now,
            is_frozen=is_frozen, has_keyholder=has_keyholder, end_date=end_date, frozen_at=frozen_at,
        )
        with self._core.transaction() as tx:
            tx.execute(
                """
                INSERT INTO chaster_provider_observations
                    (id, connection_id, provider, chaster_lock_id, status, fetched_at, created_at,
                     is_frozen, has_keyholder, end_date, frozen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.id, observation.connection_id, observation.provider,
                    observation.chaster_lock_id, observation.status.value,
                    _iso(observation.fetched_at), _iso(observation.created_at),
                    None if is_frozen is None else int(is_frozen),
                    None if has_keyholder is None else int(has_keyholder),
                    None if end_date is None else _iso(end_date),
                    None if frozen_at is None else _iso(frozen_at),
                ),
            )
        return observation
