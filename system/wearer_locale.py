"""
system/wearer_locale.py

Multi-wearer parity, slice 2 (owner, 2026-11-13): an additional wearer's own time zone and public-holiday country, in THEIR database
(migration 061, one row). `tenant_config` applies it over the owner's APP_TIMEZONE / HOLIDAY_COUNTRY, so quiet hours, the workday, the check-in
slots, the daily evaluation's day and the release windows follow the wearer's clock. A setting that was never made falls back to the owner's.

Validation: a time zone must be a known IANA name (zoneinfo); a country must be one the `holidays` package supports, or `none` (no public
holidays). Only the wearer's own DM commands (`timezone ...`, `holidays ...`, handled by the gateway, never by a model) write it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso

__all__ = ["WearerLocale", "WearerLocaleStore", "valid_timezone", "normalize_country"]


@dataclass(frozen=True)
class WearerLocale:
    timezone: str | None = None          # None: not set (the owner's applies)
    holiday_country: str | None = None   # None: not set; "": no public holidays


def valid_timezone(name: str) -> str | None:
    """The canonical IANA name, or None. Plain offsets and abbreviations ('CET', 'UTC+1') other than UTC are refused: they ignore daylight saving."""
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        return None
    name = name.strip()
    if name.upper() == "UTC":
        return "UTC"
    canonical = _zones().get(name.lower())
    if canonical is None or "/" not in canonical or canonical.lower().startswith("etc/"):
        return None
    try:
        ZoneInfo(canonical)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return canonical


@lru_cache(maxsize=1)
def _zones() -> dict[str, str]:
    return {z.lower(): z for z in available_timezones()}


def normalize_country(code: str) -> str | None:
    """'' for none/off, an upper-case supported country code, or None when unknown."""
    if not isinstance(code, str):
        return None
    code = code.strip()
    if code.lower() in ("none", "off", "no"):
        return ""
    import holidays
    upper = code.upper()
    return upper if upper in holidays.list_supported_countries() else None


class WearerLocaleStore:
    def __init__(self, core: CoreDatabase) -> None:
        self._core = core

    def get(self) -> WearerLocale:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT timezone, holiday_country FROM wearer_locale WHERE id = 1")
        return WearerLocale() if row is None else WearerLocale(row["timezone"], row["holiday_country"])

    def set_timezone(self, name: str, *, now: datetime) -> None:
        self._write("timezone", name, now)

    def set_holiday_country(self, code: str, *, now: datetime) -> None:
        self._write("holiday_country", code, now)

    def _write(self, column: str, value: str, now: datetime) -> None:
        assert column in ("timezone", "holiday_country")

        def write(tx: Transaction, _s: object) -> None:
            tx.execute(f"INSERT INTO wearer_locale (id, {column}, updated_at) VALUES (1, ?, ?) "
                       f"ON CONFLICT(id) DO UPDATE SET {column} = excluded.{column}, updated_at = excluded.updated_at", (value, _iso(now)))

        apply_transition(self._core, write=write)
