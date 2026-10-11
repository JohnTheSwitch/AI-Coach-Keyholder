"""
messaging/routes.py

Per-user (optionally per-lock) channel preference and routing metadata (migration 032). Domain-service writes only: nothing
here is reachable from a model or a conversation path. Lookup order: the lock-specific route, then the user's default route,
then Discord. A route for the `chaster` channel must carry the Chaster conversation id (the database enforces it).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from messaging.models import ChannelType

__all__ = ["MessageRoute", "MessageRouteRepository"]


@dataclass(frozen=True, kw_only=True)
class MessageRoute:
    user_id: str
    channel: ChannelType
    chaster_lock_id: str | None = None
    chaster_conversation_id: str | None = None


def _require(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value.strip()


class MessageRouteRepository:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def set_route(self, *, user_id: str, channel: ChannelType, now: datetime, chaster_lock_id: str | None = None,
                  chaster_conversation_id: str | None = None) -> MessageRoute:
        user_id = _require(user_id, "user_id")
        if not isinstance(channel, ChannelType):
            raise TypeError("channel must be a ChannelType.")
        lock_id = None if chaster_lock_id is None else _require(chaster_lock_id, "chaster_lock_id")
        conversation = None if chaster_conversation_id is None else _require(chaster_conversation_id, "chaster_conversation_id")
        if channel is ChannelType.CHASTER and conversation is None:
            raise ValueError("a chaster route needs a chaster_conversation_id.")

        def write(tx: Transaction, _s: object) -> None:
            tx.execute(
                "INSERT INTO message_routes (user_id, lock_key, channel, chaster_lock_id, chaster_conversation_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, lock_key) DO UPDATE SET channel = excluded.channel, "
                "chaster_lock_id = excluded.chaster_lock_id, chaster_conversation_id = excluded.chaster_conversation_id, "
                "updated_at = excluded.updated_at",
                (user_id, lock_id or "", channel.value, lock_id, conversation, _iso(now)),
            )

        apply_transition(self._core, write=write)
        return MessageRoute(user_id=user_id, channel=channel, chaster_lock_id=lock_id, chaster_conversation_id=conversation)

    def clear_route(self, *, user_id: str, chaster_lock_id: str | None = None) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            cur = tx.execute("DELETE FROM message_routes WHERE user_id = ? AND lock_key = ?", (user_id, chaster_lock_id or ""))
            return cur.rowcount > 0

        return apply_transition(self._core, write=write)

    def get_route(self, user_id: str, chaster_lock_id: str | None = None) -> MessageRoute:
        """Never None: without any stored route the answer is Discord."""
        keys = ([chaster_lock_id] if chaster_lock_id else []) + [""]
        with self._core.transaction() as tx:
            for key in keys:
                row = tx.fetch_one("SELECT * FROM message_routes WHERE user_id = ? AND lock_key = ?", (user_id, key))
                if row is not None:
                    return MessageRoute(user_id=user_id, channel=ChannelType(row["channel"]), chaster_lock_id=row["chaster_lock_id"],
                                        chaster_conversation_id=row["chaster_conversation_id"])
        return MessageRoute(user_id=user_id, channel=ChannelType.DISCORD)

    def list_chaster_conversations(self) -> list[tuple[str, str]]:
        """(user id, conversation id) of every opted-in Chaster route, one entry per conversation."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all(
                "SELECT user_id, chaster_conversation_id FROM message_routes WHERE channel = 'chaster' "
                "AND chaster_conversation_id IS NOT NULL ORDER BY updated_at")
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for r in rows:
            if r["chaster_conversation_id"] not in seen:
                seen.add(r["chaster_conversation_id"])
                out.append((r["user_id"], r["chaster_conversation_id"]))
        return out

    def discord_id_for(self, user_id: str) -> str | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT external_id FROM user_channel_identities WHERE user_account_id = ? AND channel = 'discord'", (user_id,))
        return None if row is None else row["external_id"]
