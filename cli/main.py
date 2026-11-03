"""
cli/main.py

`python -m cli list-locks` (v0.39 dual-role, Phase A): a READ-ONLY diagnostic that queries both lock roles and prints two
clearly separated tables. It reads the project configuration and database; it never prints a token, a lock combination
or provider text, and never changes anything. Network calls happen only when the command is invoked.

`extension-config` (Phase I1) is READ-ONLY as well: it discovers every lock of the connected account in both roles and prints the
review settings (peer verification, visibility, punishments) of the `tasks` and `verification-picture` extensions.

`freeze-lock` is the one guarded LIVE command (Phase D): owner only, --yes, runtime halt, managed locks only, audited.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from chaster.lock_directory import KEYHOLDER_SCOPE, LockDirectory, LockListing
from chaster.managed_locks import ManagedLock

__all__ = ["main", "render_listing", "render_configuration", "build_directory", "build_configuration_reader", "build_freeze_service",
           "describe", "discovery_user_ids"]

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


def _chaster_wiring():
    """The configuration, connection repository and token manager every Chaster CLI command shares."""
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
    return config, connections, ChasterTokenManager(connections=connections, oauth_client=oauth)


def discovery_user_ids(wearer_user_id: str | None, keyholder_user_id: str | None) -> tuple[str | None, str | None]:
    """Role-agnostic default (Phase I1): without a separate CHASTER_EXECUTION_WEARER_USER_ID the one connected account is asked for
    the locks it wears as well as the locks it holds the keys of."""
    return wearer_user_id or keyholder_user_id, keyholder_user_id


def build_directory() -> tuple[LockDirectory, str | None, str | None]:
    """The real wiring from the project configuration (tests inject their own directory instead)."""
    from chaster.lock_client import ChasterLockClient

    config, connections, tokens = _chaster_wiring()
    directory = LockDirectory(token_manager=tokens, lock_client=ChasterLockClient(), connections=connections)
    return (directory, *discovery_user_ids(config.chaster_execution_wearer_user_id, config.chaster_execution_keyholder_user_id))


def build_configuration_reader():
    """(directory, reader, account user id) for `extension-config`; the account is the one holding the Chaster connection."""
    from chaster.extension_config import LockConfigurationReader
    from chaster.lock_client import ChasterLockClient

    config, connections, tokens = _chaster_wiring()
    client = ChasterLockClient()
    return (LockDirectory(token_manager=tokens, lock_client=client, connections=connections),
            LockConfigurationReader(token_manager=tokens, lock_client=client), config.chaster_execution_keyholder_user_id)


def render_configuration(listing: LockListing, report) -> str:
    """Read-only view of the review settings on every lock of the account (enum values only; no task text, no codes)."""
    from chaster.extension_config import EXTENSIONS_EDIT_PERMISSION, REVIEW_EXTENSIONS, describe_review, review_gaps

    lines = [f"ACCOUNT LOCKS: {len(listing.wearer_locks)} worn, {len(listing.keyholder_locks)} held as keyholder"]
    for c in report.configurations:
        lines += ["", f"LOCK {c.lock_id}  role {c.role.value if c.role else 'unknown'}  status {_cell(c.status)}  test lock {_cell(c.is_test_lock)}",
                  f"  extensions: {', '.join(e.slug for e in c.extensions) or 'none'}",
                  f"  may edit extensions: {'yes' if c.can_edit_extensions else 'NO'} "
                  f"({EXTENSIONS_EDIT_PERMISSION} granted to: {', '.join(sorted(c.edit_subjects)) or 'nobody'})"]
        width = max(len(s) for s in REVIEW_EXTENSIONS)
        lines += [f"  {slug.ljust(width)}  {describe_review(c.extension(slug))}" for slug in REVIEW_EXTENSIONS]
        gaps = review_gaps(c)
        if c.status != "locked":
            lines.append(f"  status {_cell(c.status)}: out of scope, no changes planned")
        elif gaps:
            lines += ["  to change:"] + [f"    - {g}" for g in gaps]
        else:
            lines.append("  matches the target (community review, no Chaster punishments)")
    problems = listing.problems + report.problems
    if problems:
        lines += ["", "PROBLEMS"] + [f"  {p}" for p in problems]
    return "\n".join(lines)


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


_ADMIN_TEXT = {
    "done": "Done.",
    "owner_not_configured": "Refused: OWNER_USER_ID is not configured.",
    "not_owner": "Refused: that identity is not the owner.",
    "not_confirmed": "Refused: add --yes to confirm.",
    "not_found": "No such tenant, or it is not in a state this action applies to.",
    "cap_reached": "Refused: the tenant cap is reached.",
    "refused": "Refused: that Discord id already belongs to a tenant.",
    "invalid": "Refused: invalid input (a Discord id is 5-25 digits; a name is 1-64 characters).",
    "wearer_halt": "Refused: the wearer halted their own execution; only the wearer can clear it (`resume` in their DM).",
}


def build_tenant_admin():
    """The real wiring of the owner tenant administration (tests inject their own)."""
    from core.config import Config
    from infrastructure.clock import SystemClock
    from infrastructure.database import Database as CoreDatabase
    from tenancy.admin import TenantAdmin
    from tenancy.services import open_platform_services

    config = Config.load()
    clock = SystemClock()
    services = open_platform_services(
        platform_db_path=config.platform_db_path or (config.db_path.parent / "platform.sqlite"), main_db_path=config.db_path,
        main_core=CoreDatabase(config.db_path), owner_user_id=config.owner_user_id, clock=clock, cap=config.tenant_cap)
    return TenantAdmin(registry=services.registry, halt=services.halt, owner_user_id=config.owner_user_id, clock=clock), services


_PAIR_TEXT = {
    "paired_awaiting_wearer": "Paired. The wearer must now send `confirm lock` in their DM before anything can act on it.",
    "paired_confirmed": "Paired and confirmed (the wearer's own connected Chaster account is this lock's wearer).",
    "not_a_tenant": "Refused: not an active tenant (tenant zero is never paired this way).",
    "unavailable": "Refused: the keyholder search could not be done (check the keyholder connection and its `keyholder` scope).",
    "not_managed": "Refused: that lock is not a locked lock held by the bot's keyholder account.",
    "already_paired_elsewhere": "Refused: that lock or that Chaster account is already paired with another tenant.",
    "invalid": "Refused: invalid lock id.",
}


def build_pairing_service(services):
    """The real lock-pairing wiring (tests inject their own)."""
    from chaster.lock_client import ChasterLockClient
    from chaster.oauth_client import ChasterOAuthClient
    from chaster.repository import ChasterConnectionRepository
    from chaster.token_encryptor import TokenEncryptor
    from chaster.token_manager import ChasterTokenManager
    from core.config import Config
    from infrastructure.clock import SystemClock
    from infrastructure.database import Database as CoreDatabase
    from system.lock_pairing import LockPairingService, wearer_account_lookup

    config = Config.load()
    if not (config.chaster_token_encryption_key and config.chaster_client_id and config.chaster_client_secret
            and config.chaster_redirect_uri and config.chaster_execution_keyholder_user_id):
        raise SystemExit("Chaster is not fully configured (token key, client id/secret, redirect URI and CHASTER_EXECUTION_KEYHOLDER_USER_ID).")
    connections = ChasterConnectionRepository(config.db_path, core=CoreDatabase(config.db_path),
                                              encryptor=TokenEncryptor(config.chaster_token_encryption_key))
    oauth = ChasterOAuthClient(client_id=config.chaster_client_id, client_secret=config.chaster_client_secret,
                               redirect_uri=config.chaster_redirect_uri, scopes=tuple(config.chaster_oauth_scopes.split()) or ("locks",))
    return LockPairingService(
        registry=services.registry, lock_client=ChasterLockClient(),
        keyholder_tokens=ChasterTokenManager(connections=connections, oauth_client=oauth), keyholder_connections=connections,
        keyholder_user_id=config.chaster_execution_keyholder_user_id, clock=SystemClock(),
        wearer_account_of=wearer_account_lookup(services.tenant_dir))


def _slave_training(args, services, out, now) -> int:
    """The owner's per-database switch, and (for the owner only) the current hidden factor of the database's users."""
    from infrastructure.database import Database as CoreDatabase
    from release_engine.conditioning import ProgressiveConditioning
    from tenancy.registry import resolve_db_path

    tenant = services.registry.get(args.tenant_id)
    if tenant is None or tenant.status.value == "removed":
        print("No such tenant.", file=out)
        return 3
    path = resolve_db_path(tenant, services.tenant_dir)
    if not path.exists():
        print("That tenant has no database yet.", file=out)
        return 3
    core = CoreDatabase(path)
    conditioning = ProgressiveConditioning(path, core=core)
    if args.enable or args.disable:
        conditioning.set_owner_enabled(bool(args.enable), now=now())
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT user_id, factor FROM training_state ORDER BY user_id")
    state = "on" if conditioning.owner_enabled() else "off"
    print(f"Progressive Conditioning owner switch for {tenant.id}: {state}.", file=out)
    for row in rows:
        print(f"  user {row['user_id'][:8]}: factor {row['factor']}", file=out)
    print("The wearer's own `conditioning off` still freezes the factor; ENABLE_SLAVE_TRAINING must be true for anything to change.", file=out)
    return 0


def _tenant_execution(admin, services, args, now):
    """Sets or clears the execution halt inside ONE tenant's own database (owner only; the file must already exist)."""
    from chaster_execution.control import ExecutionControl
    from infrastructure.database import Database as CoreDatabase
    from tenancy.admin import AdminOutcome, AdminResult
    from tenancy.registry import resolve_db_path

    refusal = admin.authorize(args.owner_id, "execution")
    if refusal is not None:
        return refusal
    tenant = services.registry.get(args.tenant_id)
    if tenant is None or tenant.is_tenant_zero or not tenant.is_managed:
        return AdminResult(AdminOutcome.NOT_FOUND)
    path = resolve_db_path(tenant, services.tenant_dir)
    if not path.exists():
        return AdminResult(AdminOutcome.NOT_FOUND)
    from tenancy.models import WEARER_HALT_REASON

    control = ExecutionControl(path, core=CoreDatabase(path))
    if args.action == "clear" and control.is_halted() and control.reason() == WEARER_HALT_REASON:
        return AdminResult(AdminOutcome.WEARER_HALT, tenant)                 # the wearer's own stop is theirs to lift
    try:
        control.set_halted(args.action == "halt", reason=args.reason, now=now())
    except ValueError:
        return AdminResult(AdminOutcome.INVALID)
    return AdminResult(AdminOutcome.DONE, tenant)


class _CliClock:
    def __init__(self, now) -> None:
        self._now = now

    def now(self):
        return self._now()


def build_idea_sources():
    """(owner id, [(label, database path)]): the owner's main database plus, with multi-wearer on, every live tenant database."""
    from core.config import Config
    from infrastructure.database import Database as CoreDatabase
    from tenancy.registry import resolve_db_path

    config = Config.load()
    sources = [("tenant_0", config.db_path)]
    if config.enable_multi_wearer:
        from infrastructure.clock import SystemClock
        from tenancy.services import open_platform_services
        services = open_platform_services(
            platform_db_path=config.platform_db_path or (config.db_path.parent / "platform.sqlite"), main_db_path=config.db_path,
            main_core=CoreDatabase(config.db_path), owner_user_id=config.owner_user_id, clock=SystemClock(), cap=config.tenant_cap)
        for tenant in services.registry.list_tenants():
            if not tenant.is_tenant_zero:
                sources.append((tenant.id, resolve_db_path(tenant, services.tenant_dir)))
    return config.owner_user_id, sources


def _ai_ideas(args, out, ideas_factory, now) -> int:
    """Owner-only review of the idea box across tenants. Read-only except the verdict on one suggestion; never shown to a model."""
    import hmac

    from infrastructure.database import Database as CoreDatabase
    from idea_box.service import IdeaBox

    owner, sources = ideas_factory()
    if not owner or not hmac.compare_digest(str(args.owner_id).encode("utf-8"), str(owner).encode("utf-8")):
        print("Refused: that identity is not the owner (or OWNER_USER_ID is not configured).", file=out)
        return 3
    if args.ideas_command == "list":
        status = None if args.status == "all" else args.status
        shown = 0
        for label, path in sources:
            if args.tenant and label != args.tenant:
                continue
            if not path.exists():
                continue
            try:
                ideas = IdeaBox(path, core=CoreDatabase(path)).list(status=status)
            except Exception:
                print(f"[{label}] could not be read", file=out)
                continue
            for idea in ideas:
                shown += 1
                print(f"{label}:{idea.short_id}  [{idea.suggestion_type}] ({idea.status}) {idea.created_at:%Y-%m-%d}  {idea.content}", file=out)
        print(f"{shown} suggestion(s).", file=out)
        return 0
    label, _, short = args.ref.partition(":")
    path = dict(sources).get(label)
    if path is None or not short or not path.exists():
        print("No such tenant or suggestion.", file=out)
        return 3
    status = {"accept": "accepted", "reject": "rejected", "archive": "archived"}[args.ideas_command]
    if not IdeaBox(path, core=CoreDatabase(path)).review(short, status=status, note=args.note, now=now()):
        print("No single suggestion matches that id.", file=out)
        return 3
    print(f"Marked {status}. Nothing changes by itself: adopting an idea is a separate change that needs the wearer's consent.", file=out)
    return 0


def build_research_model(timeout: float):
    """The local Ollama model for the offline analysis. Refuses a non-local OLLAMA_HOST: the summary never leaves this machine."""
    from behavioral_telemetry.analysis import is_local_host
    from conversation_engine.ollama_adapter import OllamaConversationModel
    from core.config import Config

    config = Config.load()
    if not is_local_host(config.ollama_host):
        raise ValueError("OLLAMA_HOST is not this machine")
    return OllamaConversationModel(host=config.ollama_host, model_name=config.research_ollama_model or config.ollama_model,
                                   read_timeout_seconds=timeout, keep_alive=config.ollama_keep_alive)


_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm")


def _research_analyze(args, out, tenants_factory, model_factory, now) -> int:
    """Owner only. Reads one tenant's telemetry read-only, asks the local model for a report and prints it or writes it to a plain file. The report is never
    stored in any database and never reaches a prompt of the coaching conversation."""
    from behavioral_telemetry.analysis import analyze, bot_is_live, summarize
    from behavioral_telemetry.store import RETENTION_DAYS
    from conversation_engine.model_types import LLMGenerationError
    from tenancy.registry import resolve_db_path

    admin, services = tenants_factory()
    refusal = admin.authorize(args.owner_id, "research_analyze")
    if refusal is not None:
        print(f"{_ADMIN_TEXT[refusal.outcome.value]} [outcome={refusal.outcome.value}]", file=out)
        return 3
    if not 1 <= args.days <= RETENTION_DAYS:
        print(f"--days must be between 1 and {RETENTION_DAYS}.", file=out)
        return 3
    tenant = services.registry.get(args.tenant_id)
    if tenant is None or tenant.status.value == "removed":
        print("No such tenant.", file=out)
        return 3
    path = resolve_db_path(tenant, services.tenant_dir)
    if not path.exists():
        print("That tenant has no database yet.", file=out)
        return 3
    if args.output is not None:
        target = Path(args.output)
        if target.suffix.lower() in _DATABASE_SUFFIXES or target.resolve().parent == Path(services.tenant_dir).resolve():
            print("Refused: the report is written only to a plain file outside the database folders.", file=out)
            return 3
    zero = [t for t in services.registry.list_tenants() if t.is_tenant_zero]
    live_paths = [path] + [resolve_db_path(t, services.tenant_dir) for t in zero]
    if not args.allow_while_live and bot_is_live(live_paths, now()):
        print("Refused: the bot is running (its startup lease is held or was taken in the last 10 minutes). The analysis would compete with live replies "
              "for the GPU. Stop the bot, wait, or pass --allow-while-live.", file=out)
        return 3
    summaries = summarize(path, days=args.days, now=now())
    if not summaries:
        print(f"No behavioral telemetry for {tenant.id} in the last {args.days} days.", file=out)
        return 0
    try:
        model = model_factory(args.timeout)
    except ValueError as exc:
        print(f"Refused: {exc}. The analysis only uses a model on this machine.", file=out)
        return 3
    sections = []
    for summary in summaries:
        try:
            report = analyze(model, summary)
        except LLMGenerationError as exc:
            print(f"The local model did not answer ({exc.code}). Nothing was written.", file=out)
            return 4
        sections.append(f"=== Behavioral analysis: {tenant.id}, user {summary.user_id[:8]}, last {args.days} days, {summary.records} records, "
                        f"generated {now():%Y-%m-%d %H:%M} UTC ===\n\n{report}")
    text = "\n\n".join(sections) + "\n"
    if args.output is None:
        print(text, file=out)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Report written to {args.output} (it is not stored anywhere else).", file=out)
    return 0


def _render_tenants(tenants) -> str:
    rows = [(t.id, t.display_name, t.status.value, "yes" if t.is_tenant_zero else "-", t.discord_id or "-", t.lock_id or "-")
            for t in tenants]
    headers = ("TENANT", "NAME", "STATUS", "ZERO", "DISCORD ID", "LOCK ID")
    rows = [r + (("archived" if t.archived_at else "-"),) for r, t in zip(rows, tenants)] if tenants else rows
    headers = headers + ("ARCHIVE",)
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)), "  ".join("-" * w for w in widths)]
    return "\n".join(lines + ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows])


def _state(value: str) -> bool:
    if value.strip().lower() in ("true", "1", "yes"):
        return True
    if value.strip().lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def main(argv: list[str], *, out: TextIO | None = None,
         directory_factory: Callable[[], tuple[LockDirectory, str | None, str | None]] = build_directory,
         now: Callable[[], datetime] | None = None, freeze_factory: Callable[[], object] = build_freeze_service,
         tenants_factory: Callable[[], tuple] = build_tenant_admin, pairing_factory: Callable[[object], object] | None = None,
         ideas_factory: Callable[[], tuple] | None = None, research_model_factory: Callable[[float], object] | None = None,
         config_factory: Callable[[], tuple] | None = None) -> int:
    from infrastructure.clock import SystemClock

    out = out or sys.stdout
    pairing_factory = pairing_factory or build_pairing_service
    ideas_factory = ideas_factory or build_idea_sources
    research_model_factory = research_model_factory or build_research_model
    config_factory = config_factory or build_configuration_reader
    now = now or SystemClock().now
    parser = argparse.ArgumentParser(prog="python -m cli", description="Chaster diagnostics (read-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-locks", help="List self-locks (wearer) and managed locks (keyholder).")
    sub.add_parser("extension-config", help="READ-ONLY: review settings (peer verification, visibility, punishments) of every lock of the account.")
    freeze = sub.add_parser("freeze-lock", help="LIVE: freeze or unfreeze a managed lock (owner only, audited).")
    freeze.add_argument("lock_id")
    freeze.add_argument("--state", type=_state, required=True, help="true = freeze, false = unfreeze")
    freeze.add_argument("--owner-id", required=True, help="your internal user id (must equal OWNER_USER_ID)")
    freeze.add_argument("--yes", action="store_true", help="confirm that this really changes a live lock")
    tenants = sub.add_parser("tenants", help="Multi-wearer tenant administration (owner only; list is read-only).")
    tsub = tenants.add_subparsers(dest="tenant_command", required=True)
    ideas = sub.add_parser("ai-ideas", help="Owner: review the model's private suggestions from every tenant (read across databases).")
    isub = ideas.add_subparsers(dest="ideas_command", required=True)
    il = isub.add_parser("list", help="List suggestions (default: new ones).")
    il.add_argument("--status", choices=("new", "accepted", "rejected", "archived", "all"), default="new")
    il.add_argument("--tenant", help="only this tenant id")
    for verb in ("accept", "reject", "archive"):
        ip = isub.add_parser(verb, help=f"Mark one suggestion {verb}ed (changes nothing else).")
        ip.add_argument("ref", help="<tenant id>:<suggestion id prefix>, as shown by list")
        ip.add_argument("--note", default=None)
    for p in isub.choices.values():
        p.add_argument("--owner-id", required=True, help="your internal user id (must equal OWNER_USER_ID)")
    research = sub.add_parser("research", help="Owner: offline analysis of Behavioral Telemetry with the LOCAL model (nothing is stored).")
    rsub = research.add_subparsers(dest="research_command", required=True)
    ra = rsub.add_parser("analyze", help="A 'Psychological Profile & Recommendations' report for one tenant (tenant_0 = your own database).")
    ra.add_argument("tenant_id")
    ra.add_argument("--days", type=int, default=30, help="how many days back (1-180, default 30)")
    ra.add_argument("--output", default=None, help="write the report to this plain file instead of printing it")
    ra.add_argument("--allow-while-live", action="store_true", help="run even while the bot is running (competes with live replies for the GPU)")
    ra.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for the model (default 600)")
    ra.add_argument("--owner-id", required=True, help="your internal user id (must equal OWNER_USER_ID)")
    lst = tsub.add_parser("list", help="List tenants and the platform halt.")
    lst.add_argument("--all", action="store_true", help="also show removed and archived tenants")
    tsub.add_parser("archive", help="Archive the database files of removed tenants now (normally the bot does it).")
    invite = tsub.add_parser("invite", help="Invite a person (they must accept in their own Discord DM).")
    invite.add_argument("--name", required=True)
    invite.add_argument("--discord-id", required=True)
    for name, help_text in (("suspend", "Pause a tenant (management and polling off)."), ("resume", "Resume a suspended tenant."),
                            ("remove", "Remove a tenant (immediate, unilateral).")):
        sp = tsub.add_parser(name, help=help_text)
        sp.add_argument("tenant_id")
        if name == "remove":
            sp.add_argument("--yes", action="store_true")
    for name, help_text in (("halt", "Stop execution for EVERY tenant."), ("unhalt", "Clear the global halt.")):
        hp = tsub.add_parser(name, help=help_text)
        hp.add_argument("--reason", required=True)
    pr = tsub.add_parser("pair", help="Pair a tenant with ONE managed lock (verified live; the wearer must confirm).")
    pr.add_argument("tenant_id")
    pr.add_argument("lock_id")
    up = tsub.add_parser("unpair", help="Remove a tenant's lock pairing.")
    up.add_argument("tenant_id")
    st = tsub.add_parser("slave-training", help="Owner switch for Progressive Conditioning in ONE database (tenant_0 = the owner's).")
    st.add_argument("tenant_id")
    group = st.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    group.add_argument("--status", action="store_true")
    ex = tsub.add_parser("execution", help="Halt or clear ONE tenant's execution runtime (a new tenant starts halted).")
    ex.add_argument("tenant_id")
    ex.add_argument("action", choices=("halt", "clear"))
    ex.add_argument("--reason", required=True)
    for p in tsub.choices.values():
        if p.prog.split()[-1] != "list":
            p.add_argument("--owner-id", required=True, help="your internal user id (must equal OWNER_USER_ID)")
    args = parser.parse_args(argv)

    if args.command == "list-locks":
        directory, wearer_id, keyholder_id = directory_factory()
        listing = directory.list_locks(wearer_user_id=wearer_id, keyholder_user_id=keyholder_id, now=now())
        print(render_listing(listing), file=out)
        return 0 if not listing.problems else 2
    if args.command == "extension-config":
        directory, reader, user_id = config_factory()
        if not user_id:
            print("CHASTER_EXECUTION_KEYHOLDER_USER_ID is not set: no connected Chaster account to read.", file=out)
            return 2
        listing = directory.list_account_locks(user_id=user_id, now=now())
        report = reader.read(user_id=user_id, lock_ids=[l.lock_id for l in listing.wearer_locks + listing.keyholder_locks], now=now())
        print(render_configuration(listing, report), file=out)
        return 0 if not (listing.problems or report.problems) else 2
    if args.command == "ai-ideas":
        return _ai_ideas(args, out, ideas_factory, now)
    if args.command == "research":
        return _research_analyze(args, out, tenants_factory, research_model_factory, now)
    if args.command == "tenants":
        admin, services = tenants_factory()
        cmd = args.tenant_command
        if cmd == "list":
            print(_render_tenants(services.registry.list_tenants(include_removed=args.all)), file=out)
            print(f"\nPlatform halt: {'HALTED' if services.halt.is_halted() else 'clear'}", file=out)
            return 0
        if cmd == "invite":
            result = admin.invite(actor=args.owner_id, display_name=args.name, discord_id=args.discord_id)
        elif cmd == "suspend":
            result = admin.suspend(actor=args.owner_id, tenant_id=args.tenant_id)
        elif cmd == "resume":
            result = admin.resume(actor=args.owner_id, tenant_id=args.tenant_id)
        elif cmd == "remove":
            result = admin.remove(actor=args.owner_id, tenant_id=args.tenant_id, confirmed=args.yes)
        elif cmd == "archive":
            refusal = admin.authorize(args.owner_id, "archive")
            if refusal is not None:
                result = refusal
            else:
                from tenancy.archive import TenantArchiver
                from tenancy.admin import AdminOutcome, AdminResult
                done = TenantArchiver(registry=services.registry, tenant_dir=services.tenant_dir, clock=_CliClock(now)).archive_pending()
                print(f"Archived {len(done)} tenant database(s).", file=out)
                result = AdminResult(AdminOutcome.DONE)
        elif cmd == "slave-training":
            refusal = admin.authorize(args.owner_id, "slave_training")
            if refusal is not None:
                result = refusal
            else:
                return _slave_training(args, services, out, now)
        elif cmd in ("pair", "unpair"):
            refusal = admin.authorize(args.owner_id, cmd)
            if refusal is not None:
                result = refusal
            elif cmd == "unpair":
                from tenancy.admin import AdminOutcome, AdminResult
                ok = services.registry.unpair(args.tenant_id, actor="owner", now=now())
                result = AdminResult(AdminOutcome.DONE if ok else AdminOutcome.NOT_FOUND)
            else:
                paired = pairing_factory(services).pair(args.tenant_id, args.lock_id, actor="owner")
                print(_PAIR_TEXT[paired.outcome.value] + (f" Lock: '{paired.title}' of Chaster user {paired.wearer_username}."
                                                          if paired.title or paired.wearer_username else ""), file=out)
                return 0 if paired.outcome.value.startswith("paired") else 3
        elif cmd == "execution":
            result = _tenant_execution(admin, services, args, now)
        elif cmd == "halt":
            result = admin.halt_all(actor=args.owner_id, reason=args.reason)
        else:
            result = admin.resume_all(actor=args.owner_id, reason=args.reason)
        suffix = f" tenant={result.tenant.id}" if result.tenant is not None else ""
        print(f"{_ADMIN_TEXT[result.outcome.value]} [outcome={result.outcome.value}{suffix}]", file=out)
        return 0 if result.done else 3
    if args.command == "freeze-lock":
        service = freeze_factory()
        result = service.run(actor=args.owner_id, lock_id=args.lock_id, frozen=args.state, confirmed=args.yes)
        print(f"{describe(result.outcome.value)} [outcome={result.outcome.value} audit={result.audit_id}]", file=out)
        return 0 if result.outcome.value in ("applied", "already_in_state") else 3 if result.refused else 4
    return 1
