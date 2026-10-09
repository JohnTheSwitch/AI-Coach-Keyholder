"""
chaster/english_text.py

English number/unit formatting done in code so a small local model never
has to build a time span itself. Pure, deterministic, no clock access.

Rule: exactly 1 -> singular, everything else (0, 2, 3, ...) -> plural.
Time is truncated (not rounded) to whole minutes, matching the existing
"2d 3h 4m" display.
"""

from __future__ import annotations

__all__ = ["english_plural", "format_time_english"]


def english_plural(count: int, singular: str, plural: str | None = None) -> str:
    """`count` with the correct noun form, e.g. `2 days` / `1 day`."""
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural if plural is not None else singular + 's'}"


def format_time_english(seconds: int) -> str:
    """`189420` -> `2 days, 4 hours and 37 minutes`.

    Zero-valued units are omitted; units are joined as `a, b and c`.
    Less than one minute -> `less than a minute`. Raises TypeError for a
    non-int (bool included) and ValueError for a negative value, so a
    caller bug (e.g. an expired lock passed in as negative seconds)
    fails loudly instead of producing a wrong sentence."""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise TypeError("seconds must be an int")
    if seconds < 0:
        raise ValueError("seconds must not be negative")

    total_minutes = seconds // 60
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(english_plural(days, "day"))
    if hours:
        parts.append(english_plural(hours, "hour"))
    if minutes:
        parts.append(english_plural(minutes, "minute"))

    if not parts:
        return "less than a minute"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]
