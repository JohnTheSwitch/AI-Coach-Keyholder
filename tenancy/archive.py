"""
tenancy/archive.py

TenantArchiver (v0.39 Phase E6): what happens to a removed tenant's database file.

When a tenant is removed (the keyholder removed it, or the wearer left or declined), its runtime and loop stop at once (E2/E3). After a grace period
(`ARCHIVE_GRACE`, so no cycle that was already running can still be writing), the archiver:

  1. checkpoints the SQLite write-ahead log into the main file;
  2. moves `tenant_<id>.sqlite` (and any `-wal` / `-shm` / `-journal` side files) from the live tenant directory into `tenants/archive/` under a
     time-stamped name, so the factory, the gateway and the CLI can never open it again (they only look in the live directory);
  3. makes the archived file read-only and records `archived_at` / `archive_file` in the platform registry with an audit event.

Nothing is deleted: the archive keeps the tenant's audit trail (consent, decisions, executions). Deleting an archived file is a deliberate manual operator
step described in the runbook. A failure (for example a file still held open) is logged and retried on the next pass; it never affects other tenants.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path

from infrastructure.clock import Clock
from tenancy.registry import TenantRegistry, resolve_db_path

logger = logging.getLogger("ai_coach_keyholder.tenancy.archive")

__all__ = ["TenantArchiver", "ARCHIVE_GRACE", "ARCHIVE_DIR_NAME"]

ARCHIVE_GRACE = timedelta(minutes=5)
ARCHIVE_DIR_NAME = "archive"
_SIDE_FILES = ("-wal", "-shm", "-journal")


class TenantArchiver:
    def __init__(self, *, registry: TenantRegistry, tenant_dir: str | Path, clock: Clock, grace: timedelta = ARCHIVE_GRACE) -> None:
        self._registry = registry
        self._dir = Path(tenant_dir)
        self._clock = clock
        self._grace = grace

    @property
    def archive_dir(self) -> Path:
        return self._dir / ARCHIVE_DIR_NAME

    def archive_pending(self, *, is_running=lambda tenant_id: False) -> list[str]:
        """Archives every removed tenant that is due. Returns the ids archived in this pass."""
        done: list[str] = []
        now = self._clock.now()
        for tenant in self._registry.pending_archives(now=now, grace=self._grace):
            if is_running(tenant.id):
                continue                                         # its loop has not stopped yet: next pass
            try:
                archive_name = self._archive_file(tenant, now)
            except Exception:
                logger.exception("Archiving tenant %s failed; will retry on the next pass.", tenant.id)
                continue
            if self._registry.mark_archived(tenant.id, archive_file=archive_name, now=now):
                done.append(tenant.id)
        return done

    def _archive_file(self, tenant, now) -> str | None:
        source = resolve_db_path(tenant, self._dir)
        if not source.exists():
            return None                                          # the wearer never got a database (e.g. declined an invitation)
        self._checkpoint(source)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        target = self.archive_dir / f"{tenant.id}.{stamp}.sqlite"
        if target.exists():
            raise FileExistsError("archive target already exists")
        for suffix in _SIDE_FILES:                               # side files first, so a crash never leaves a main file without its log
            side = Path(str(source) + suffix)
            if side.exists():
                os.replace(side, Path(str(target) + suffix))
        os.replace(source, target)
        try:
            os.chmod(target, stat.S_IREAD)
        except OSError:
            logger.warning("Could not make the archived database of tenant %s read-only.", tenant.id)
        return target.name

    @staticmethod
    def _checkpoint(path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
