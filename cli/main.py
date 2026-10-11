"""
cli/main.py

`python -m cli list-locks` (v0.39 dual-role, Phase A): a READ-ONLY diagnostic that queries both lock roles and prints two
clearly separated tables. It reads the project configuration and database; it never prints a token, a lock combination
or provider text, and never changes anything. Network calls happen only when the command is invoked.

`freeze-lock` is the one guarded LIVE command (Phase D): owner only, --yes, runtime halt, managed locks only, audited.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Callable, TextIO

from chaster.lock_directory import KEYHOLDER_SCOPE, LockDirectory, LockListing
from chaster.managed_locks import ManagedLock

__all__ = ["main", "render_listing", "build_directory", "build_freeze_service", "describe"]

_HEADERS = ("LOCK ID", "TITLE", "WEARER", "WEARER ID", "STATUS", "isFrozen", "TEST")


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(_HEADERS)]
    out = ["  ".join(h.ljust(w) for h, w in zip(_HEADERS, widths)), "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    return out


def _rows(locks: tuple[ManagedLock, ...]) -> list[tuple[str, ...]]:
    return [(l.lock_id, _cell(l.title), _cell(l.wearer_username), _cell(l.wearer_chaster_id), _cell(l.status),
             _cell(l.is_frozen), _cell(l.is_test_lock)) for l in locks]


def render_listing(listing: LockListing) -> str:
    lines = ["SELF-LOCKS (wearer)", *_table(_rows(listing.wearer_locks)), "",
             "MANAGED LOCKS (keyholder)", *_table(_rows(listing.keyholder_locks))]
    if listing.problems:
        lines += ["", "PROBLEMS"] + [f"  {p}" for p in listing.problems]
        if any(p.endswith(f"missing_scope:{KEYHOLDER_SCOPE}") for p in listing.problems):
            lines.append('  hint: connect the keyholder account with CHASTER_OAUTH_SCOPES="locks keyholder" set.')
    return "\n".join(lines)


def build_directory() -> tuple[LockDirectory, str | None, str | None]:
    """The real wiring from the project configuration (tests inject their own directory instead)."""
    from chaster.lock_client import ChasterLockClient
    from chaster.oauth_client import ChasterOAuthClient
    from chaster.repository import ChasterConnectionRepository
    from chaster.token_encryptor import TokenEncryptor
    from chaster.token_manager import ChasterTokenManager
    from core.config import Config
    from infrastructure.database import Database as CoreDatabase

    config = Config.load()
    if not (config.chaster_token_encryption_key and config.chaster_client_id and config.chaster_client_secret
            and config.chaster_redirect_uri):
        raise SystemExit("Chaster is not fully configured (token key, client id, client secret and redirect URI are required).")
    core = CoreDatabase(config.db_path)
    connections = ChasterConnectionRepository(
        config.db_path, core=core, encryptor=TokenEncryptor(config.chaster_token_encryption_key))
    oauth = ChasterOAuthClient(client_id=config.chaster_client_id, client_secret=config.chaster_client_secret,
                               redirect_uri=config.chaster_redirect_uri,
                               scopes=tuple(config.chaster_oauth_scopes.split()) or ("locks",))
    directory = LockDirectory(token_manager=ChasterTokenManager(connections=connections, oauth_client=oauth),
                              lock_client=ChasterLockClient(), connections=connections)
    return directory, config.chaster_execution_wearer_user_id, config.chaster_execution_keyholder_user_id


_DESCRIPTIONS = {
    "applied": "Done: Chaster accepted the change.",
    "owner_not_configured": "Refused: OWNER_USER_ID is not configured.",
    "not_owner": "Refused: that identity is not the owner.",
    "not_confirmed": "Refused: add --yes to confirm this live action.",
    "invalid_lock_id": "Refused: not a valid lock id.",
    "halted": "Refused: the execution runtime is halted.",
    "scope_unverifiable": "Refused: could not verify the lock is managed by the keyholder account (check the connection and scope).",
    "not_managed_lock": "Refused: that lock is not a locked lock managed by the keyholder account.",
    "state_unknown": "Refused: Chaster did not report the lock's frozen state.",
    "already_in_state": "Nothing to do: the lock is already in that state.",
    "not_sent": "Failed: nothing was sent to Chaster.",
    "unauthorized": "Failed: Chaster rejected the credentials (reconnect the keyholder account).",
    "forbidden": "Failed: the keyholder account lacks permission to change this lock.",
    "not_eligible": "Failed: Chaster says the lock cannot be changed this way right now.",
    "not_found": "Failed: Chaster does not know that lock.",
    "unknown": "UNKNOWN: the request may or may not have been applied. Check the lock on Chaster before trying again.",
    "attempting": "UNKNOWN: the attempt was not completed.",
}


def describe(outcome: str) -> str:
    return _DESCRIPTIONS.get(outcome, outcome)


def build_freeze_service():
    """The real wiring of the guarded manual freeze (tests inject their own service instead)."""
    from chaster.lock_client import ChasterLockClient
    from chaster.oauth_client import ChasterOAuthClient
    from chaster.repository import ChasterConnectionRepository
    from chaster.token_encryptor import TokenEncryptor
    from chaster.token_manager import ChasterTokenManager
    from chaster_execution.control import ExecutionControl
    from chaster_execution.manual_freeze import ManualFreezeService
    from chaster_execution.transport import ChasterHttpTransport
    from core.config import Config
    from infrastructure.clock import SystemClock
    from infrastructure.database import Database as CoreDatabase

    config = Config.load()
    if not (config.chaster_token_encryption_key and config.chaster_client_id and config.chaster_client_secret
            and config.chaster_redirect_uri and config.chaster_execution_keyholder_user_id):
        raise SystemExit("Chaster is not fully configured (token key, client id/secret, redirect URI and CHASTER_EXECUTION_KEYHOLDER_USER_ID).")
    core = CoreDatabase(config.db_path)
    clock = SystemClock()
    connections = ChasterConnectionRepository(
        config.db_path, core=core, encryptor=TokenEncryptor(config.chaster_token_encryption_key))
    oauth = ChasterOAuthClient(client_id=config.chaster_client_id, client_secret=config.chaster_client_secret,
                               redirect_uri=config.chaster_redirect_uri,
                               scopes=tuple(config.chaster_oauth_scopes.split()) or ("locks",))
    tokens = ChasterTokenManager(connections=connections, oauth_client=oauth)
    return ManualFreezeService(
        config.db_path, core=core, control=ExecutionControl(config.db_path, core=core),
        directory=LockDirectory(token_manager=tokens, lock_client=ChasterLockClient(), connections=connections),
        transport=ChasterHttpTransport(token_manager=tokens, keyholder_user_id=config.chaster_execution_keyholder_user_id, clock=clock),
        owner_user_id=config.owner_user_id, keyholder_user_id=config.chaster_execution_keyholder_user_id, clock=clock)


def _state(value: str) -> bool:
    if value.strip().lower() in ("true", "1", "yes"):
        return True
    if value.strip().lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def main(argv: list[str], *, out: TextIO | None = None,
         directory_factory: Callable[[], tuple[LockDirectory, str | None, str | None]] = build_directory,
         now: Callable[[], datetime] | None = None, freeze_factory: Callable[[], object] = build_freeze_service) -> int:
    from infrastructure.clock import SystemClock

    out = out or sys.stdout
    now = now or SystemClock().now
    parser = argparse.ArgumentParser(prog="python -m cli", description="Chaster diagnostics (read-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-locks", help="List self-locks (wearer) and managed locks (keyholder).")
    freeze = sub.add_parser("freeze-lock", help="LIVE: freeze or unfreeze a managed lock (owner only, audited).")
    freeze.add_argument("lock_id")
    freeze.add_argument("--state", type=_state, required=True, help="true = freeze, false = unfreeze")
    freeze.add_argument("--owner-id", required=True, help="your internal user id (must equal OWNER_USER_ID)")
    freeze.add_argument("--yes", action="store_true", help="confirm that this really changes a live lock")
    args = parser.parse_args(argv)

    if args.command == "list-locks":
        directory, wearer_id, keyholder_id = directory_factory()
        listing = directory.list_locks(wearer_user_id=wearer_id, keyholder_user_id=keyholder_id, now=now())
        print(render_listing(listing), file=out)
        return 0 if not listing.problems else 2
    if args.command == "freeze-lock":
        service = freeze_factory()
        result = service.run(actor=args.owner_id, lock_id=args.lock_id, frozen=args.state, confirmed=args.yes)
        print(f"{describe(result.outcome.value)} [outcome={result.outcome.value} audit={result.audit_id}]", file=out)
        return 0 if result.outcome.value in ("applied", "already_in_state") else 3 if result.refused else 4
    return 1
