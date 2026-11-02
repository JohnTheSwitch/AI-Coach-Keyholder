"""
hive_mind_receiver/clock.py

The receiver's only reading of the wall clock (it is a standalone service and does not use the bot's Clock). Everything else takes an injected `now`
callable, so tests run on fixed times.
"""

from datetime import datetime, timezone


def system_now() -> datetime:
    return datetime.now(timezone.utc)
