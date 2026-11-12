"""
system/memory_seed.py

Refills the in-memory working memory from the stored conversation after a restart (owner, 2026-11-10: "the bot keeps forgetting"). Working memory lives in
the process, so every restart used to start the conversation from nothing, while `conversation_messages` held every message. Each stored user message and
the assistant reply right after it become one exchange again, oldest first, within `since`; the working memory's own limits still apply.

This database has one wearer, and `conversation_messages` has no user column, so everything goes to that one subject. Read-only towards the database.
"""

from __future__ import annotations

import logging
from datetime import datetime

from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

logger = logging.getLogger("ai_coach_keyholder.system.memory_seed")

__all__ = ["seed_working_memory"]


def seed_working_memory(memory, core: CoreDatabase, *, subject_key: str | None, since: datetime, limit: int = 40) -> int:
    """Returns how many exchanges were restored. Never raises: a failure only means the conversation starts fresh, as before."""
    if not subject_key:
        return 0
    try:
        with core.transaction() as tx:
            rows = tx.fetch_all("SELECT role, content, created_at FROM (SELECT role, content, created_at, rowid AS r FROM conversation_messages WHERE created_at >= ? "
                                "ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, r", (_iso(since), limit))
        restored, pending, pending_at = 0, None, None
        for row in rows:
            content = (row["content"] or "").strip()
            if row["role"] == "user":
                pending, pending_at = content or None, (row["created_at"] if "created_at" in row.keys() else None)
            elif row["role"] == "assistant" and pending is not None and content:
                memory.commit_exchange(subject_key=subject_key, user_content=pending, assistant_content=content,
                                       at=_parse_iso(pending_at) if pending_at else None)
                restored, pending = restored + 1, None
        return restored
    except Exception:
        logger.exception("The conversation could not be restored; it starts fresh.")
        return 0
