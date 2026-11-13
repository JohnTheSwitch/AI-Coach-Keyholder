"""
messaging/keyholder_inbox.py

KeyholderInboxWatch (owner, 2026-11-13: "I won't be online on this account"): the keyholder account (AI_Coach_Keyholder) is not read by a
person, but strangers may message it, for example someone who joined its premade lock by mistake and needs releasing. This watch lists the
account's Chaster conversations (`GET /conversations`, statuses `pending` (message requests) and `approved`; scope `messaging`; read-only)
and FORWARDS each new message from anyone but the owner and the account itself to the owner's Discord DM, once. Owner's choice: forward
only. Nothing is ever sent from the account, nothing is answered, nothing touches a lock.

Only each conversation's `lastMessage` is read: several messages between two passes arrive as the newest one, with a note to open the
conversation. The first pass ever is the baseline: what is already there is recorded and not forwarded. State lives in `keyholder_inbox_seen`
(migration 059): one row per conversation with the last message id handled. Message text is not stored there; the forwarded excerpt goes
through the notification queue like every other notice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso

logger = logging.getLogger("ai_coach_keyholder.messaging.keyholder_inbox")

__all__ = ["KeyholderInboxWatch", "keyholder_inbox_loop", "INBOX_POLL_SECONDS", "EXCERPT"]

INBOX_POLL_SECONDS = 300          # every five minutes
EXCERPT = 300                     # characters of a message in the notice
_STATUSES = ("pending", "approved")
_BASELINE = "__baseline__"


def _username(users: object, user_id: str | None) -> str:
    if isinstance(users, list):
        for user in users:
            if isinstance(user, dict) and user.get("_id") == user_id and isinstance(user.get("username"), str):
                return user["username"]
    return "someone"


class KeyholderInboxWatch:
    def __init__(self, core: CoreDatabase, *, tokens, client, keyholder_user_id: str, own_account_id: Callable[[], str | None],
                 owner_account_id: str | None, notify: Callable[[str, str], None]) -> None:
        """`own_account_id()` is the keyholder account's own Chaster id (read when needed: it changes when the account is re-linked);
        `notify(key, text)` queues a notice for the owner."""
        self._core = core
        self._tokens = tokens
        self._client = client
        self._kh_user = keyholder_user_id
        self._own = own_account_id
        self._owner = owner_account_id
        self._notify = notify

    def run(self, now: datetime) -> int:
        """One pass; returns how many messages were forwarded. Raises on a failed read (the loop logs it and tries again later)."""
        own = self._own()
        conversations: list[dict] = []
        for status in _STATUSES:
            body = self._tokens.call_with_token(self._kh_user, now=now, operation=lambda token, s=status: self._client.list_conversations(
                access_token=token, status=s))
            conversations.extend(c for c in body if isinstance(c, dict))
        with self._core.transaction() as tx:      # the first pass that succeeds is the baseline, even with no conversation at all
            baseline = tx.fetch_one("SELECT 1 FROM keyholder_inbox_seen WHERE conversation_id = ?", (_BASELINE,)) is None
        forwarded = 0
        for conversation in conversations:
            conversation_id, last = conversation.get("_id"), conversation.get("lastMessage")
            if not isinstance(conversation_id, str) or not isinstance(last, dict) or not isinstance(last.get("_id"), str):
                continue
            with self._core.transaction() as tx:
                row = tx.fetch_one("SELECT last_message_id FROM keyholder_inbox_seen WHERE conversation_id = ?", (conversation_id,))
            if row is not None and row["last_message_id"] == last["_id"]:
                continue
            sender = last.get("user")
            worth = not baseline and sender not in (own, self._owner) and sender is not None
            if worth:
                text = last.get("message") if isinstance(last.get("message"), str) else ""
                excerpt = text[:EXCERPT] + ("..." if len(text) > EXCERPT else "")
                request = " (a message request)" if conversation.get("status") == "pending" else ""
                self._notify(f"keyholder_inbox:{last['_id']}",
                             f"New Chaster message to your keyholder account{request} from {_username(conversation.get('users'), sender)}: "
                             f"\"{excerpt}\" Open the conversation on Chaster for the rest; nothing was answered.")
                forwarded += 1
            with self._core.transaction() as tx:
                tx.execute("INSERT INTO keyholder_inbox_seen (conversation_id, last_message_id, seen_at) VALUES (?, ?, ?) "
                           "ON CONFLICT(conversation_id) DO UPDATE SET last_message_id = excluded.last_message_id, seen_at = excluded.seen_at",
                           (conversation_id, last["_id"], _iso(now)))
        if baseline:
            with self._core.transaction() as tx:
                tx.execute("INSERT OR IGNORE INTO keyholder_inbox_seen (conversation_id, last_message_id, seen_at) VALUES (?, '-', ?)", (_BASELINE, _iso(now)))
        return forwarded


async def keyholder_inbox_loop(watch: KeyholderInboxWatch, clock, *, interval_seconds: float = INBOX_POLL_SECONDS,
                               initial_delay_seconds: float = 60.0) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            forwarded = await asyncio.to_thread(watch.run, clock.now())
            if forwarded:
                logger.info("Keyholder inbox: %d new message(s) forwarded to the owner.", forwarded)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("The keyholder inbox pass failed (%s); will retry next interval.", type(exc).__name__)
        await asyncio.sleep(interval_seconds)
