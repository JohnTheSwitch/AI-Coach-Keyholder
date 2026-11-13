"""
cli/setup.py

`python -m cli setup check` and `python -m cli setup init` (owner, 2026-11-13: "for someone to download it and use it themselves").

- `setup check` is READ-ONLY. It reads the configuration and the local database and prints one line per piece of the setup: OK, MISSING (with
  what to do) or INFO. It never prints a secret: a token, key or secret is only ever reported as set or not set. It also lists the users the
  bot knows (internal id and Discord id), which is how a new owner finds the value for OWNER_USER_ID. Ollama is asked only whether it is up
  and has the model.
- `setup init --owner-id <id> --yes` creates the Trust domains the configuration names and that do not exist yet, each with its consent record
  (the same path as `trust-domain create`). Nothing else.

Nothing here touches Chaster, a lock or the runtime halt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable
from zoneinfo import ZoneInfo

__all__ = ["Line", "setup_report", "render_report", "named_trust_domains", "DOMAIN_TEXTS", "REQUIRED_KEYHOLDER_SCOPES"]

from trust_manager.domain_admin import DEFAULT_DOMAIN_TEXTS as DOMAIN_TEXTS, named_trust_domains  # noqa: E402,F401  (shared with tenants)

REQUIRED_KEYHOLDER_SCOPES = ("locks", "keyholder", "messaging", "offline_access")   # plain names; split ones count (chaster/oauth_client.py)
@dataclass(frozen=True)
class Line:
    status: str          # OK | MISSING | INFO
    item: str
    detail: str


def setup_report(config, core, *, model_ready: Callable[[], object] | None = None) -> list[Line]:
    out: list[Line] = []

    def add(ok: bool | None, item: str, detail: str) -> None:
        out.append(Line("INFO" if ok is None else "OK" if ok else "MISSING", item, detail))

    add(bool(getattr(config, "discord_token", None)), "Discord bot token", "set" if getattr(config, "discord_token", None) else
        "set DISCORD_TOKEN (Discord Developer Portal -> your application -> Bot -> Token; enable Message Content Intent)")

    readiness = model_ready() if model_ready is not None else None
    if readiness is not None:
        if not getattr(readiness, "reachable", False):
            add(False, "Ollama", f"not reachable at {config.ollama_host}: install Ollama and start it")
        elif not getattr(readiness, "installed", False):
            add(False, "Ollama model", f"run: ollama pull {config.ollama_model}")
        else:
            add(True, "Ollama model", f"{config.ollama_model} is installed")

    if core is None or not _table(core, "schema_version"):
        add(False, "Database", f"{config.db_path} does not exist yet: start the bot once (`python -m bot.discord_bot`), then run this again")
        return out
    with core.transaction() as tx:
        version = tx.fetch_one("SELECT MAX(version) AS v FROM schema_version")["v"]
        users = tx.fetch_all("SELECT a.id AS id, i.external_id AS discord FROM user_accounts a LEFT JOIN user_channel_identities i "
                             "ON i.user_account_id = a.id ORDER BY a.created_at") if _has(tx, "user_channel_identities", "external_id") else []
        domains = {r["domain_id"]: bool(r["is_active"]) for r in tx.fetch_all("SELECT domain_id, is_active FROM trust_domains")}
        halted_row = tx.fetch_one("SELECT halted FROM execution_control WHERE id = 1")
    add(True, "Database", f"{config.db_path} (schema {version})")

    known = {u["id"] for u in users}
    for u in users:
        add(None, "Known user", f"internal id {u['id']}  Discord id {u['discord'] or '-'}")
    if not users:
        add(None, "Known users", "none yet: start the bot and send it a DM, then run this again")
    owner = getattr(config, "owner_user_id", None)
    if not owner:
        add(False, "OWNER_USER_ID", "set it to YOUR internal id from the 'Known user' lines above")
    else:
        add(owner in known, "OWNER_USER_ID", "set and known" if owner in known else "set, but no such user in the database")

    chaster = all(getattr(config, k, None) for k in ("chaster_client_id", "chaster_client_secret", "chaster_token_encryption_key"))
    add(chaster, "Chaster app", "client id, secret and encryption key are set" if chaster else
        "set CHASTER_CLIENT_ID, CHASTER_CLIENT_SECRET (your Chaster developer app) and CHASTER_TOKEN_ENCRYPTION_KEY")
    scopes = set(str(getattr(config, "chaster_oauth_scopes", "") or "").split())
    missing_scopes = [s for s in REQUIRED_KEYHOLDER_SCOPES if s not in scopes]
    add(not missing_scopes, "Chaster scopes", "complete" if not missing_scopes else f"add to CHASTER_OAUTH_SCOPES: {' '.join(missing_scopes)}")
    keyholder = getattr(config, "chaster_execution_keyholder_user_id", None)
    add(bool(keyholder), "Keyholder user", "CHASTER_EXECUTION_KEYHOLDER_USER_ID is set" if keyholder else
        "set CHASTER_EXECUTION_KEYHOLDER_USER_ID to the internal id that ran `chaster connect` as the keyholder account")
    if keyholder:
        with core.transaction() as tx:
            row = tx.fetch_one("SELECT chaster_username, connection_status, granted_scopes_json FROM chaster_connections WHERE user_id = ?", (keyholder,))
        if row is None:
            add(False, "Keyholder connection", "DM the bot `chaster connect` and log in as the KEYHOLDER account")
        else:
            add(row["connection_status"] == "active", "Keyholder connection", f"{row['chaster_username']} ({row['connection_status']})")
            try:
                granted = set(json.loads(row["granted_scopes_json"] or "[]"))
            except ValueError:
                granted = set()
            lacking = [s for s in REQUIRED_KEYHOLDER_SCOPES if s not in granted]
            add(not lacking, "Granted permissions", "complete" if not lacking else
                f"the connection lacks {' '.join(lacking)}: fix CHASTER_OAUTH_SCOPES, restart, then `chaster connect` again")

    for name, label in (("release_lock_id", "RELEASE_LOCK_ID"), ("penalty_freeze_lock_id", "PENALTY_FREEZE_LOCK_ID"), ("exemption_lock_id", "EXEMPTION_LOCK_ID")):
        value = (getattr(config, name, None) or "").strip()
        add(None, label, "auto (your recognised lock)" if value.lower() == "auto" else value or "not set (that feature acts on no lock)")
    if any((getattr(config, n, None) or "").strip().lower() == "auto" for n in ("release_lock_id", "penalty_freeze_lock_id", "exemption_lock_id")):
        account = getattr(config, "owner_chaster_account_id", None)
        add(bool(account), "OWNER_CHASTER_ACCOUNT_ID", "set" if account else "needed for `auto`: your own (wearer) Chaster account id")

    for name in named_trust_domains(config):
        exists = domains.get(name)
        add(bool(exists), f"Trust domain {name}", "exists" if exists else "missing: run `python -m cli setup init --owner-id <id> --yes`")

    try:
        ZoneInfo(config.app_timezone)
        add(True, "Time zone", f"{config.app_timezone}; public holidays: {config.holiday_country or 'none'}")
    except Exception:
        add(False, "Time zone", f"APP_TIMEZONE {config.app_timezone!r} is not a valid IANA name (for example Europe/Berlin)")

    halted = halted_row is None or bool(halted_row["halted"])
    add(None, "Runtime halt", "HALTED: nothing acts on a lock until you run `python -m scripts.execution_control clear \"why\"`" if halted
        else "clear: lock actions are allowed")
    return out


def _table(core, name: str) -> bool:
    with core.transaction() as tx:
        return tx.fetch_one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)) is not None


def _has(tx, table: str, column: str) -> bool:
    return any(r["name"] == column for r in tx.fetch_all(f"PRAGMA table_info({table})"))


def render_report(lines: list[Line]) -> str:
    width = max((len(l.item) for l in lines), default=0)
    text = [f"{l.status:<8}{l.item:<{width}}  {l.detail}" for l in lines]
    missing = sum(1 for l in lines if l.status == "MISSING")
    text.append("")
    text.append("Everything needed is in place." if missing == 0 else f"{missing} item(s) still missing; see above.")
    return "\n".join(text)
