"""
chaster/lock_directory.py

LockDirectory (v0.39 dual-role, Phase A): READ-ONLY discovery of every lock the bot can see, in both roles.

- WEARER locks: `GET /locks?status=active` with the wearer connection (scope `locks`); unchanged behaviour.
- KEYHOLDER locks: `POST /keyholder/locks/search` with the keyholder connection (scope `keyholder`).

The two sides are independent: a failure on one side is reported as a short problem code and never hides the other
side (partial results are explicit). Problem codes carry no tokens and no provider text. Nothing here writes anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.managed_locks import LockRole, ManagedLock, parse_lock
from chaster.repository import ChasterConnectionRepository
from chaster.token_manager import (
    ChasterNotConnectedError, ChasterReauthorizationRequiredError, ChasterRefreshUnavailableError, ChasterTokenManager,
)

logger = logging.getLogger("ai_coach_keyholder.chaster.lock_directory")

__all__ = ["LockDirectory", "LockListing", "WEARER_SCOPE", "KEYHOLDER_SCOPE"]

WEARER_SCOPE = "locks"
KEYHOLDER_SCOPE = "keyholder"


@dataclass
class LockListing:
    wearer_locks: tuple[ManagedLock, ...] = ()
    keyholder_locks: tuple[ManagedLock, ...] = ()
    problems: list[str] = field(default_factory=list)       # e.g. "keyholder: missing_scope:keyholder"


class LockDirectory:
    def __init__(self, *, token_manager: ChasterTokenManager, lock_client: ChasterLockClient,
                 connections: ChasterConnectionRepository) -> None:
        self._tokens = token_manager
        self._client = lock_client
        self._connections = connections

    def list_locks(self, *, wearer_user_id: str | None, keyholder_user_id: str | None, now: datetime) -> LockListing:
        listing = LockListing()
        listing.wearer_locks = self._side(
            "wearer", wearer_user_id, WEARER_SCOPE, LockRole.WEARER, now, listing.problems,
            lambda token: self._client.list_active_locks(access_token=token))
        listing.keyholder_locks = self._side(
            "keyholder", keyholder_user_id, KEYHOLDER_SCOPE, LockRole.KEYHOLDER, now, listing.problems,
            lambda token: self._client.search_keyholder_locks(access_token=token, status="locked"))
        return listing

    # ------------------------------------------------------------------ internals

    def _side(self, name, user_id, scope, role, now, problems, fetch) -> tuple[ManagedLock, ...]:
        if not user_id:
            problems.append(f"{name}: not_configured")
            return ()
        connection = self._connections.get(user_id)
        if connection is None:
            problems.append(f"{name}: not_connected")
            return ()
        if scope not in connection.granted_scopes:
            problems.append(f"{name}: missing_scope:{scope}")
            return ()
        try:
            raw = self._tokens.call_with_token(user_id, now=now, operation=fetch)
        except ChasterNotConnectedError:
            problems.append(f"{name}: not_connected")
        except (ChasterReauthorizationRequiredError, ChasterRefreshUnavailableError):
            problems.append(f"{name}: reauthorization_required")
        except ChasterLockApiError as exc:
            problems.append(f"{name}: api_error" + (f":{exc.status_code}" if exc.status_code else ""))
        except Exception:
            logger.exception("Unexpected failure while listing %s locks.", name)
            problems.append(f"{name}: failed")
        else:
            return tuple(lock for lock in (parse_lock(item, role) for item in raw) if lock is not None)
        return ()
