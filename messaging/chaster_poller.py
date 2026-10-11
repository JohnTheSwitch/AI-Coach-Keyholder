"""
messaging/chaster_poller.py

ChasterConversationPoller (Phase C): Chaster has no webhooks, so inbound chat is polled. Only conversations of users who OPTED IN
(a stored `chaster` route, see messaging/routes.py) are polled, and only when the feature flag is on.

Rules (every one tested):
- The first poll of a conversation only ARMS it: existing messages are marked seen and never processed (no replay of history).
- Ordering of the provider's page is not assumed; messages are sorted by `createdAt` here.
- A message is handed on only if it is a plain `message` (not a `log` entry) sent BY THE OPTED-IN USER's own Chaster account and created
  after arming. Anything else in the conversation (the bot's own messages, other participants, system logs) is ignored: untrusted text from
  a third party never reaches the conversation.
- At most MAX_PER_POLL messages per conversation and poll are processed (flood control); the rest are marked seen and dropped.
- A message id is recorded as seen BEFORE it is handed on (at-most-once: a crash loses a message instead of answering it twice).
- A failure in one conversation never stops the others; nothing here raises into the caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from chaster.conversation_client import ChasterConversationClient
from chaster.repository import ChasterConnectionRepository
from chaster.token_manager import ChasterTokenManager
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from messaging.chaster_adapter import ChasterMessagingAdapter
from messaging.models import UnifiedMessage
from messaging.routes import MessageRouteRepository

logger = logging.getLogger("ai_coach_keyholder.messaging.chaster_poller")

__all__ = ["ChasterConversationPoller", "MAX_PER_POLL", "SEEN_RETENTION", "MAX_MESSAGE_AGE"]

MAX_PER_POLL = 3
SEEN_RETENTION = timedelta(days=7)
MAX_MESSAGE_AGE = timedelta(days=1)      # older messages are never answered (the seen list is pruned after 7 days)


class ChasterConversationPoller:
    def __init__(
        self, db_path: str | Path, *, core: CoreDatabase | None = None, routes: MessageRouteRepository,
        connections: ChasterConnectionRepository, token_manager: ChasterTokenManager, client: ChasterConversationClient,
        adapter: ChasterMessagingAdapter, bot_user_id: str, clock: Clock,
    ) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._routes = routes
        self._connections = connections
        self._tokens = token_manager
        self._client = client
        self._adapter = adapter
        self._bot_user_id = bot_user_id
        self._clock = clock

    def poll_once(self) -> list[UnifiedMessage]:
        """Blocking (HTTP + SQLite): call it from a worker thread. Returns the new messages to handle, oldest first."""
        out: list[UnifiedMessage] = []
        now = self._clock.now()
        for user_id, conversation_id in self._routes.list_chaster_conversations():
            try:
                out.extend(self._poll_conversation(user_id, conversation_id, now))
            except Exception as exc:
                logger.warning("Polling a Chaster conversation failed (%s).", type(exc).__name__)
        self._prune(now)
        return out

    # ------------------------------------------------------------------ internals

    def _poll_conversation(self, user_id: str, conversation_id: str, now: datetime) -> list[UnifiedMessage]:
        wearer = self._connections.get(user_id)
        if wearer is None:
            return []
        wearer_chaster_id = wearer.chaster_account_id
        raw = self._tokens.call_with_token(
            self._bot_user_id, now=now,
            operation=lambda token: self._client.list_messages(access_token=token, conversation_id=conversation_id))
        armed_at = self._arm(user_id, conversation_id, now)
        seen = self._seen_ids(conversation_id)
        fresh = [m for m in raw if isinstance(m.get("_id"), str) and m["_id"] not in seen]
        fresh.sort(key=lambda m: str(m.get("createdAt", "")))
        if armed_at is None:                                     # first poll: remember what is there, process nothing
            self._mark_seen(conversation_id, [m["_id"] for m in fresh], now)
            return []

        eligible = [m for m in fresh if self._eligible(m, wearer_chaster_id, armed_at, now)]
        accepted, dropped = eligible[:MAX_PER_POLL], eligible[MAX_PER_POLL:]
        if dropped:
            logger.warning("Dropped %d Chaster message(s) beyond the per-poll limit.", len(dropped))
        self._mark_seen(conversation_id, [m["_id"] for m in fresh], now)           # BEFORE handing anything on
        return [self._adapter.unify(user_id=user_id, conversation_id=conversation_id, message=m) for m in accepted]

    @staticmethod
    def _eligible(message: dict, wearer_chaster_id: str, armed_at: datetime, now: datetime) -> bool:
        text = message.get("message")
        if message.get("type") != "message" or message.get("user") != wearer_chaster_id:
            return False
        if not isinstance(text, str) or not text.strip():
            return False
        try:
            created = _parse_iso(str(message["createdAt"]))
            return created >= armed_at and now - created <= MAX_MESSAGE_AGE
        except (KeyError, ValueError, TypeError):
            return False                                          # no trustworthy timestamp: not processed

    def _arm(self, user_id: str, conversation_id: str, now: datetime) -> datetime | None:
        """Returns the arming time, or None when this very call armed the conversation."""
        def write(tx: Transaction, _s: object) -> datetime | None:
            row = tx.fetch_one("SELECT armed_at FROM chaster_chat_state WHERE conversation_id = ?", (conversation_id,))
            if row is None:
                tx.execute("INSERT INTO chaster_chat_state (conversation_id, user_id, armed_at, last_polled_at) VALUES (?, ?, ?, ?)",
                           (conversation_id, user_id, _iso(now), _iso(now)))
                return None
            tx.execute("UPDATE chaster_chat_state SET last_polled_at = ? WHERE conversation_id = ?", (_iso(now), conversation_id))
            return _parse_iso(row["armed_at"])

        return apply_transition(self._core, write=write)

    def _seen_ids(self, conversation_id: str) -> set[str]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT message_id FROM chaster_chat_seen WHERE conversation_id = ?", (conversation_id,))
        return {r["message_id"] for r in rows}

    def _mark_seen(self, conversation_id: str, ids: list[str], now: datetime) -> None:
        if not ids:
            return

        def write(tx: Transaction, _s: object) -> None:
            for message_id in ids:
                tx.execute("INSERT OR IGNORE INTO chaster_chat_seen (conversation_id, message_id, seen_at) VALUES (?, ?, ?)",
                           (conversation_id, message_id, _iso(now)))

        apply_transition(self._core, write=write)

    def _prune(self, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("DELETE FROM chaster_chat_seen WHERE seen_at < ?", (_iso(now - SEEN_RETENTION),))

        try:
            apply_transition(self._core, write=write)
        except Exception:
            logger.exception("Pruning the Chaster chat state failed.")
