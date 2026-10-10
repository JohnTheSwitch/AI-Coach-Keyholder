"""
chaster/managed_locks.py

Role-aware lock values (v0.39 dual-role, Phase A). A lock is seen either as a WEARER lock (the connected account wears it;
`GET /locks`, schema LockForWearer) or as a KEYHOLDER lock (the connected account holds the keys of somebody else's lock;
`POST /keyholder/locks/search`, schema LockForKeyholder). Field names below are confirmed against the OpenAPI document
(api.chaster.app/api-json, fetched 2026-10-10): `_id`, `title`, `status`, `isFrozen`, `frozenAt`, `endDate`, `isTestLock`,
`user` {`_id`, `username`} and `keyholder` {`_id`, `username`}.

SECRETS: the raw payload also carries `combination`, `offerToken` and `sharedLinkToken`. They are NEVER copied into these
values, so they cannot reach a log, a table, a CLI row or a model. Unknown or ill-typed fields become None (nothing is guessed).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = ["LockRole", "ManagedLock", "parse_lock"]


class LockRole(StrEnum):
    WEARER = "wearer"
    KEYHOLDER = "keyholder"


@dataclass(frozen=True, kw_only=True)
class ManagedLock:
    role: LockRole
    lock_id: str
    title: str | None
    status: str | None
    is_frozen: bool | None
    is_test_lock: bool | None
    wearer_chaster_id: str | None
    wearer_username: str | None
    keyholder_chaster_id: str | None
    keyholder_username: str | None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _user(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    return _str(value.get("_id")), _str(value.get("username"))


def parse_lock(raw: Any, role: LockRole) -> ManagedLock | None:
    """Returns None for anything that is not a lock object with an id (never raises on odd provider data)."""
    if not isinstance(raw, dict):
        return None
    lock_id = _str(raw.get("_id"))
    if lock_id is None:
        return None
    wearer_id, wearer_name = _user(raw.get("user"))
    holder_id, holder_name = _user(raw.get("keyholder"))
    return ManagedLock(
        role=role, lock_id=lock_id, title=_str(raw.get("title")), status=_str(raw.get("status")),
        is_frozen=_bool(raw.get("isFrozen")), is_test_lock=_bool(raw.get("isTestLock")),
        wearer_chaster_id=wearer_id, wearer_username=wearer_name,
        keyholder_chaster_id=holder_id, keyholder_username=holder_name,
    )
