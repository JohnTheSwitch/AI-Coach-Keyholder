"""
system/tenant_factory.py

The tenant factory (v0.39 Phase E2): turns one ACTIVE registry entry into a fully isolated runtime. A tenant owns its OWN SQLite file (`tenant_<id>.sqlite`),
its own migrations, startup lease, users, Trust/Penalty/Goal state, conversation memory and queue, ApplicationService and periodic wiring. Nothing mutable is
shared between two tenants; what IS shared is stateless or platform-owned: the language model, the OAuth client, the Chaster lock client, the platform-wide
halt and the single keyholder Chaster connection (Chaster refresh tokens rotate, so exactly one place may own it).

Tenant zero (the owner) is NOT built here: it keeps running through the existing single-wearer composition in bot/discord_bot.py, untouched.

Safety defaults for a new tenant:
- Its execution runtime starts HALTED (an empty execution_control row counts as halted); the owner enables it explicitly
  (`python -m cli tenants execution <id> clear --reason ...`). Until the managed-lock observation source exists (E4) the Chaster-dependent parts report
  "NOT active" and nothing is ever sent for the tenant.
- The Chaster LLM tool and the Chaster chat channel are not enabled for tenants yet.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from application.models import IncomingMessage, OutgoingMessage
from application.service import ApplicationService
from application.user_service import UserService
from chaster.lock_client import ChasterLockClient
from chaster.oauth_client import ChasterOAuthClient
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenEncryptor, TokenEncryptorConfigurationError
from chaster.token_manager import ChasterTokenManager
from conversation_engine.context_providers.active_task_provider import ActiveTaskContextProvider
from conversation_engine.context_providers.lock_state_provider import LockStateContextProvider
from conversation_engine.context_providers.release_phase_provider import ReleasePhaseContextProvider
from conversation_engine.engine import ConversationEngine
from conversation_engine.subject_queue import SubjectConversationQueue
from core.config import Config
from database.database import Database
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from lock_state.repository import LockState
from memory_system.working_memory import InMemoryWorkingMemory
from system.composition import RulesExecutionWiring, build_rules_and_execution
from system.periodic import run_periodic_cycle
from system.startup import on_system_startup
from task_catalog.repository import TaskCatalog
from task_runtime.repository import TaskRuntime
from tenancy.registry import TenantError, TenantRegistry, resolve_db_path

logger = logging.getLogger("ai_coach_keyholder.system.tenant_factory")

__all__ = ["SharedServices", "TenantRuntime", "TenantFactory", "tenant_config"]


@dataclass(frozen=True)
class SharedServices:
    """Everything a tenant may share: stateless collaborators and the platform-owned pieces."""
    config: Config
    clock: Clock
    model: object                                           # the conversation model (stateless)
    oauth_client: ChasterOAuthClient | None
    lock_client: ChasterLockClient
    keyholder_connections: ChasterConnectionRepository | None
    keyholder_token_manager: ChasterTokenManager | None
    platform_halt: object | None                            # anything with is_halted()
    check_in_sender_factory: Callable[[Path, CoreDatabase], object] | None = None   # Discord delivery bound to the TENANT's database
    check_in_complete: Callable[[str, str], str] | None = None                       # rubric classifier completion (stateless)


@dataclass
class TenantRuntime:
    tenant_id: str
    db_path: Path
    core: CoreDatabase
    wearer_user_id: str
    application_service: ApplicationService
    wiring: RulesExecutionWiring
    process_id: str
    clock: Clock
    notes: list[str] = field(default_factory=list)

    def handle_message(self, incoming: IncomingMessage) -> OutgoingMessage:
        """The tenant's own application entry (the Discord routing of E3 calls it). It never sees another tenant's data."""
        return self.application_service.handle_message(incoming)

    def cycle(self) -> bool:
        """One staggered periodic cycle of this tenant, under its own database lease. False when another instance holds the lease."""
        w = self.wiring
        return run_periodic_cycle(
            self.core, self.process_id, self.clock, rules_engine=w.rules_engine, executor=w.executor, supervision=w.supervision,
            release_task=w.release_task, check_ins=w.check_ins, daily=w.daily)


def tenant_config(base: Config, *, db_path: Path, wearer_user_id: str) -> Config:
    """The tenant's settings: the owner's flags and tuning, but THIS tenant's database and wearer. Settings that name a person of the
    owner's system (check-in user, execution wearer) never leak into a tenant; the keyholder id is the platform's single value."""
    return dataclasses.replace(
        base, db_path=db_path, chaster_execution_wearer_user_id=wearer_user_id, check_in_user_id=None,
        enable_chaster_messaging=False, chaster_llm_tools_enabled=False, enable_multi_wearer=False)


class TenantFactory:
    def __init__(self, *, registry: TenantRegistry, shared: SharedServices, tenant_dir: str | Path) -> None:
        self._registry = registry
        self._shared = shared
        self._dir = Path(tenant_dir)

    def build(self, tenant_id: str) -> TenantRuntime:
        tenant = self._registry.get(tenant_id)
        if tenant is None or not tenant.is_managed:
            raise TenantError("only an active tenant can be built")
        if tenant.is_tenant_zero:
            raise TenantError("tenant zero runs through the existing composition, not the factory")
        if not tenant.discord_id:
            raise TenantError("a tenant needs a Discord id")
        s = self._shared
        path = resolve_db_path(tenant, self._dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = s.clock.now()

        # 1. The tenant's own database, migrated before anything else touches it.
        core = CoreDatabase(path)
        db = Database(path, backup_retention=s.config.backup_retention_count, core=core)
        applied = db.migrate(now=now)
        if applied:
            logger.info("Tenant %s: applied migrations %s.", tenant.id, applied)
        db.ensure_daily_backup(now=now)

        # 2. The wearer's local account inside THIS database (idempotent).
        wearer = UserService(path, core=core).get_or_create_user("discord", tenant.discord_id, now=now)

        # 3. Wearer-side Chaster objects live in the tenant's database; the keyholder side is the platform's.
        connections = tokens = None
        if s.config.chaster_token_encryption_key and s.oauth_client is not None:
            try:
                connections = ChasterConnectionRepository(path, core=core, encryptor=TokenEncryptor(s.config.chaster_token_encryption_key))
                tokens = ChasterTokenManager(connections=connections, oauth_client=s.oauth_client)
            except TokenEncryptorConfigurationError:
                logger.error("Tenant %s: Chaster token encryption is misconfigured; Chaster is off for this tenant.", tenant.id)

        # 4. Conversation stack: a fresh memory, queue and context providers over THIS database only.
        memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
        lock_state, task_runtime, task_catalog = LockState(path, core=core), TaskRuntime(path, core=core), TaskCatalog(path, core=core)
        providers = (LockStateContextProvider(lock_state=lock_state), ActiveTaskContextProvider(task_runtime=task_runtime, task_catalog=task_catalog))

        # 5. Rules / execution / release / check-ins / daily evaluation for this tenant.
        cfg = tenant_config(s.config, db_path=path, wearer_user_id=wearer.id)
        sender = s.check_in_sender_factory(path, core) if s.check_in_sender_factory is not None else None
        wiring = build_rules_and_execution(
            cfg, core, connections=connections, oauth_client=s.oauth_client, lock_client=s.lock_client, token_manager=tokens,
            clock=s.clock, check_in_sender=sender, check_in_complete=s.check_in_complete,
            keyholder_connections=s.keyholder_connections, keyholder_token_manager=s.keyholder_token_manager, halt_source=s.platform_halt)
        if wiring.release_engine is not None:
            providers = providers + (ReleasePhaseContextProvider(get_phase=wiring.release_phase),)

        process_id = str(uuid.uuid4())
        on_system_startup(core, process_id, s.clock, wiring.rules_engine, wiring.supervision, release_task=wiring.release_task)

        engine = ConversationEngine(model=s.model, working_memory_reader=memory, working_memory_writer=memory,
                                    queue=SubjectConversationQueue(), providers=providers, tool_executor=None)
        service = ApplicationService(
            path, core=core, conversation_engine=engine, lock_state=lock_state, task_runtime=task_runtime, task_catalog=task_catalog,
            chaster_connections=connections, chaster_oauth_client=s.oauth_client, chaster_token_manager=tokens,
            check_in_listener=(wiring.check_ins.on_user_message if wiring.check_ins is not None else None))
        runtime = TenantRuntime(tenant_id=tenant.id, db_path=path, core=core, wearer_user_id=wearer.id, application_service=service,
                                wiring=wiring, process_id=process_id, clock=s.clock, notes=list(wiring.notes))
        logger.info("Tenant %s is ready (%d note(s)).", tenant.id, len(runtime.notes))
        return runtime
