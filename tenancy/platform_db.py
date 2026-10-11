"""
tenancy/platform_db.py

Opens (and migrates) the platform database: a small SQLite file separate from every tenant database. It has its own numbered migrations
in tenancy/migrations/ and its own schema_version table; the migration SQL records its own version. Idempotent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from infrastructure.database import Database as CoreDatabase

__all__ = ["open_platform_db", "MIGRATIONS_DIR"]

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def open_platform_db(path: str | Path) -> CoreDatabase:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    core = CoreDatabase(path)
    with core.raw_connection() as conn:
        try:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = 0 if row is None or row["v"] is None else int(row["v"])
        except sqlite3.OperationalError:
            current = 0                                   # first run: the first migration creates schema_version
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if int(migration.name.split("_")[0]) > current:
                conn.executescript(migration.read_text(encoding="utf-8"))
        conn.commit()
    return core
