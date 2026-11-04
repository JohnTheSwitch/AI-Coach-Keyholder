"""
notifications/dispatcher.py

Empties the queue through one injected async port, `deliver(Notification) -> DeliveryResult`. A failing notice never stops the next one and nothing here ever
raises out of the loop. It holds no business rule: what a notice says, and whether it may wait for quiet hours, was decided when it was queued.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

from infrastructure.clock import Clock
from notifications.queue import DeliveryResult, Notification, NotificationQueue

logger = logging.getLogger("ai_coach_keyholder.notifications")

__all__ = ["NotificationDispatcher", "notification_loop", "DEFAULT_INTERVAL_SECONDS"]

DEFAULT_INTERVAL_SECONDS = 20


class NotificationDispatcher:
    def __init__(self, queue: NotificationQueue, *, deliver: Callable[[Notification], Awaitable[DeliveryResult]],
                 is_quiet: Callable[[datetime], bool] = lambda _now: False) -> None:
        self._queue = queue
        self._deliver = deliver
        self._is_quiet = is_quiet

    async def run_once(self, now: datetime) -> int:
        """Returns how many notices went out in this pass."""
        try:
            quiet = self._is_quiet(now)
        except Exception:
            logger.exception("Could not read quiet hours; holding routine notices back for now.")
            quiet = True
        delivered = 0
        for notification in await asyncio.to_thread(self._queue.claim_due, now, quiet=quiet):
            try:
                result = await self._deliver(notification)
            except Exception:
                logger.exception("Delivering a %s notice failed; it stays queued.", notification.kind)
                result = DeliveryResult.UNKNOWN
            await asyncio.to_thread(self._queue.record, notification, result, now)
            if result is DeliveryResult.DELIVERED:
                delivered += 1
            else:
                logger.warning("A %s notice was not delivered (%s); retrying later.", notification.kind, result.value)
        return delivered


async def notification_loop(dispatcher: NotificationDispatcher, clock: Clock, *, interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                            initial_delay_seconds: float = 10.0) -> None:
    await asyncio.sleep(initial_delay_seconds)          # let the Discord client become ready first
    while True:
        try:
            await dispatcher.run_once(clock.now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("The notification pass failed; will retry next interval.")
        await asyncio.sleep(interval_seconds)
