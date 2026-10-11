"""
messaging/chaster_loop.py

The asyncio loop that drives Chaster chat polling (Phase C). Each round polls on a worker thread (HTTP + SQLite), then hands every new
message to the Chaster adapter on the event loop. A failing round is logged and the loop continues; cancellation ends it cleanly.
"""

from __future__ import annotations

import asyncio
import logging

from messaging.chaster_adapter import ChasterMessagingAdapter
from messaging.chaster_poller import ChasterConversationPoller

logger = logging.getLogger("ai_coach_keyholder.messaging.chaster_loop")

__all__ = ["run_chaster_chat_loop", "MIN_POLL_SECONDS"]

MIN_POLL_SECONDS = 15          # be gentle with the provider


async def run_chaster_chat_loop(poller: ChasterConversationPoller, adapter: ChasterMessagingAdapter, *,
                                interval_seconds: float) -> None:
    interval = max(float(interval_seconds), MIN_POLL_SECONDS)
    while True:
        try:
            for message in await asyncio.to_thread(poller.poll_once):
                try:
                    await adapter.handle_incoming_message(message)
                except Exception:
                    logger.exception("Handling a Chaster chat message failed.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("A Chaster chat polling round failed; will retry.")
        await asyncio.sleep(interval)
