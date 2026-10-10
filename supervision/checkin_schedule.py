"""
supervision/checkin_schedule.py

Pure placement of a day's check-ins (v0.39). The number per day comes from the Trust
level (4: 0, 3: 1, 2: 2, 1: 3). They are placed ONLY inside the day's DISCRETIONARY
interval, i.e. never in quiet hours and never in work hours. The interval is cut into
`n` parts (for a single check-in, its later half: the "evening window") and one random
instant is drawn inside each part, at least `EDGE_MARGIN` from the part's edges.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

from schedule_windows.windows import WindowConfig, WindowKind, classify, discretionary_interval

__all__ = ["EDGE_MARGIN", "slots_for_day"]

EDGE_MARGIN = timedelta(minutes=15)


def slots_for_day(
    cfg: WindowConfig, local_date: date, count: int, rand_fraction: Callable[[], float],
) -> list[datetime]:
    """UTC instants, in order, length `count` (or fewer if the interval is too short).
    `rand_fraction()` returns a float in [0, 1)."""
    if count <= 0:
        return []
    start, end = discretionary_interval(cfg, local_date)
    if count == 1:
        parts = [(start + (end - start) / 2, end)]
    else:
        step = (end - start) / count
        parts = [(start + step * i, start + step * (i + 1)) for i in range(count)]
    slots: list[datetime] = []
    for lo, hi in parts:
        lo, hi = lo + EDGE_MARGIN, hi - EDGE_MARGIN
        if hi <= lo:
            continue
        slot = lo + (hi - lo) * rand_fraction()
        slot = slot.replace(microsecond=0)
        if classify(cfg, slot) is WindowKind.DISCRETIONARY:
            slots.append(slot)
    return slots
