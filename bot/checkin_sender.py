"""
bot/checkin_sender.py

The Discord adapter for proactive check-in prompts (v0.39). It implements the
`CheckInSender` protocol of the supervision package. The periodic cycle runs in a worker thread,
so delivery is handed to the Discord client's own event loop with run_coroutine_threadsafe.

Check-in prompts deliberately bypass the notification queue (they still use the one `send_dm`): a prompt's deadline starts at delivery, so the service must
know the outcome at once, and a prompt that was not provably delivered is never counted as missed.

Outcome mapping (a prompt that is not provably delivered is never treated as a missed check-in):
  delivered              -> DELIVERED
  not ready / no Discord identity / DMs closed (Forbidden) / unknown user (NotFound) -> UNDELIVERABLE
  timeout / any other HTTP failure -> UNKNOWN (the service then marks it needs_review; no penalty)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from messaging.discord_adapter import DmOutcome, send_dm
from supervision.checkins import SendOutcome

logger = logging.getLogger("ai_coach_keyholder.bot.checkin_sender")

__all__ = ["DiscordCheckInSender"]

_SEND_TIMEOUT_SECONDS = 30
_OUTCOME = {DmOutcome.DELIVERED: SendOutcome.DELIVERED, DmOutcome.UNDELIVERABLE: SendOutcome.UNDELIVERABLE, DmOutcome.UNKNOWN: SendOutcome.UNKNOWN}


class DiscordCheckInSender:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._client = None

    def attach(self, client) -> None:
        """The Discord client does not exist yet when the wiring is built; it is attached afterwards."""
        self._client = client

    def send(self, *, user_id: str, text: str) -> SendOutcome:
        client = self._client
        if client is None or not client.is_ready():
            return SendOutcome.UNDELIVERABLE
        discord_id = self._discord_id(user_id)
        if discord_id is None:
            return SendOutcome.UNDELIVERABLE
        future = asyncio.run_coroutine_threadsafe(self._send(client, discord_id, text), client.loop)
        try:
            return future.result(timeout=_SEND_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return SendOutcome.UNKNOWN
        except Exception:
            logger.exception("Check-in delivery failed with an unexpected error.")
            return SendOutcome.UNKNOWN

    def _discord_id(self, user_id: str) -> int | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT external_id FROM user_channel_identities WHERE user_account_id = ? AND channel = 'discord'",
                (user_id,),
            )
        try:
            return None if row is None else int(row["external_id"])
        except ValueError:
            return None

    @staticmethod
    async def _send(client, discord_id: int, text: str) -> SendOutcome:
        return _OUTCOME[await send_dm(client, discord_id, text)]
