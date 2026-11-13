"""
system/tenant_factory.py

The tenant factory (v0.39 Phase E2): turns one ACTIVE registry entry into a fully isolated runtime. A tenant owns its OWN SQLite file (`tenant_<id>.sqlite`),
its own migrations, startup lease, users, Trust/Penalty/Goal state, conversation memory and queue, ApplicationService and periodic wiring. Nothing mutable is
shared between two tenants; what IS shared is stateless or platform-owned: the language model, the OAuth client, the Chaster lock client, the platform-wide
halt and the single keyholder Chaster connection (Chaster refresh tokens rotate, so exactly one place may own it).

Tenant zero (the owner) is NOT built here: it keeps running through the existing single-wearer composition in bot/discord_bot.py, untouched.

Safety defaults for a new tenant:
- Its lock is observed ONLY through `ManagedLockObservationSource` (keyholder search + pairing check, Phase E4); without a confirmed pairing
  nothing is observed and nothing can act.
- Its execution runtime starts HALTED (an empty execution_control row counts as halted); the owner enables it explicitly
  (`python -m cli tenants execution <id> clear --reason ...`).
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
from conversation_engine.context_providers.iron_contract_provider import IronContractContextProvider
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
from system.managed_lock_source import ManagedLockObservationSource
from system.periodic import run_periodic_cycle
from system.startup import on_system_startup
from task_catalog.repository import TaskCatalog
from task_runtime.repository import TaskRuntime
from tenancy.registry import TenantError, TenantRegistry, resolve_db_path
from trust_manager.domain_admin import ensure_domains_with_consent, named_trust_domains

logger = logging.getLogger("ai_coach_keyholder.system.tenant_factory")

__all__ = ["SharedServices", "TenantRuntime", "TenantFactory", "tenant_config", "coaching_memory_parts", "TENANT_CONSENT_TARGET"]

# The consent_log target of an accepted invitation (= system.tenant_gateway.CONSENT_TARGET, which imports this module indirectly; a test keeps them equal).
TENANT_CONSENT_TARGET = "multi_wearer_keyholder"


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
    # Phase E5: the keyholder token manager for BACKGROUND traffic (observation, searches, chat), wrapped by the shared API budget; the plain
    # `keyholder_token_manager` above is the execution plane's (essential lane). Chat is per tenant and needs ENABLE_CHASTER_MESSAGING.
    keyholder_background_tokens: object | None = None
    chat_enabled: bool = False
    conversation_client: object | None = None


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
    chat_poller: object | None = None                 # messaging.chaster_poller.ChasterConversationPoller over THIS tenant's database
    chat_adapter: object | None = None

    def handle_message(self, incoming: IncomingMessage) -> OutgoingMessage:
        """The tenant's own application entry (the Discord routing of E3 calls it). It never sees another tenant's data."""
        return self.application_service.handle_message(incoming)

    def cycle(self) -> bool:
        """One staggered periodic cycle of this tenant, under its own database lease. False when another instance holds the lease."""
        w = self.wiring
        return run_periodic_cycle(
            self.core, self.process_id, self.clock, rules_engine=w.rules_engine, executor=w.executor, supervision=w.supervision,
            release_task=w.release_task, check_ins=w.check_ins, daily=w.daily, telemetry=w.telemetry)


def coaching_memory_parts(config, db_path, core, *, coach: bool = False):
    """(memory service, conditioning preferences, context provider or None, structured sink or None) for ONE database. The service and the
    preferences are always built, so a user can view and erase their memory and use `conditioning` even when the feature is switched off;
    the provider (reading memory into prompts) and the memory sink exist only with ENABLE_COACHING_MEMORY and structured output on; the
    idea-box sink (Phase F3, write-only) only with ENABLE_AI_SUGGESTIONS and structured output on. `coach=True` (Phase J3, the single wearer only) counts as
    structured output: a coach turn is always a JSON reply."""
    from conversation_engine.context_providers.coaching_memory_provider import CoachingMemoryContextProvider
    from coaching_memory.service import CoachingMemoryService
    from release_engine.conditioning import ConditioningPreferences

    memory = CoachingMemoryService(db_path, core=core)
    conditioning = ConditioningPreferences(db_path, core=core)
    structured = coach or getattr(config, "conversation_structured_output", False)
    sinks = []
    provider = None
    if structured and getattr(config, "enable_coaching_memory", False):
        provider = CoachingMemoryContextProvider(get_entries=lambda user_id: [(e.category, e.content, e.created_at)
                                                                             for e in memory.recent(user_id, limit=20)])
        sinks.append(memory.sink)
    if structured and getattr(config, "enable_ai_suggestions", False):
        # Phase F3: the idea box is WRITE-ONLY from here; nothing that builds model context reads it.
        from idea_box.service import IdeaBox
        sinks.append(IdeaBox(db_path, core=core).sink)
    if not sinks:
        return memory, conditioning, provider, None

    def sink(subject_key, parsed, now, user_message=None):
        for one in sinks:
            try:
                one(subject_key, parsed, now, user_message)
            except Exception:
                logger.exception("A structured-output sink failed; the others and the reply are unaffected.")

    return memory, conditioning, provider, sink


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
        if tenant.archived_at is not None:
            raise TenantError("an archived tenant is never built again")
        s = self._shared
        path = resolve_db_path(tenant, self._dir)
        # Hardening: a tenant database lives directly in the live tenant directory and is never the owner's database.
        if path.resolve().parent != self._dir.resolve() or path.resolve() == Path(s.config.db_path).resolve():
            raise TenantError("refusing a tenant database outside the tenant directory")
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

        # 2b. Owner, 2026-11-13: the Trust domains this tenant's configuration names, created once with consent: the accepted invitation names
        # the wearer's trust records. Without them the daily evaluation, the check-in level and supervision never ran for an additional wearer.
        from system.tenant_gateway import invitation_text           # lazy: the gateway imports this module
        created = ensure_domains_with_consent(core, named_trust_domains(tenant_config(s.config, db_path=path, wearer_user_id=wearer.id)),
                                              explanation=invitation_text(tenant.display_name), comment="tenant: accepted invitation", now=now)
        if created:
            logger.info("Tenant %s: created the Trust domains %s.", tenant.id, created)

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
        # The tenant's lock is observed through the keyholder search and only after the pairing check (Phase E4).
        source = ManagedLockObservationSource(
            path, core=core, get_tenant=lambda tid=tenant.id: self._registry.get(tid), wearer_user_id=wearer.id,
            keyholder_user_id=s.config.chaster_execution_keyholder_user_id, keyholder_tokens=s.keyholder_background_tokens or s.keyholder_token_manager,
            keyholder_connections=s.keyholder_connections, lock_client=s.lock_client, clock=s.clock,
            wearer_tokens=tokens, wearer_connections=connections)
        cfg = tenant_config(s.config, db_path=path, wearer_user_id=wearer.id)
        sender = s.check_in_sender_factory(path, core) if s.check_in_sender_factory is not None else None
        wiring = build_rules_and_execution(
            cfg, core, connections=connections, oauth_client=s.oauth_client, lock_client=s.lock_client, token_manager=tokens,
            clock=s.clock, check_in_sender=sender, check_in_complete=s.check_in_complete,
            keyholder_connections=s.keyholder_connections, keyholder_token_manager=s.keyholder_token_manager, halt_source=s.platform_halt,
            observation_source=source, telemetry_consent_target=TENANT_CONSENT_TARGET, telemetry_tenant_id=tenant.id)
        if wiring.release_engine is not None:
            providers = providers + (ReleasePhaseContextProvider(get_phase=wiring.release_phase),)
        providers = providers + (IronContractContextProvider(is_locked=wiring.iron_contract.locked_for_context),)

        process_id = str(uuid.uuid4())
        on_system_startup(core, process_id, s.clock, wiring.rules_engine, wiring.supervision, release_task=wiring.release_task)

        coaching, conditioning, memory_provider, memory_sink = coaching_memory_parts(s.config, path, core)
        if memory_provider is not None:
            providers = providers + (memory_provider,)
        engine = ConversationEngine(model=s.model, working_memory_reader=memory, working_memory_writer=memory,
                                    queue=SubjectConversationQueue(), providers=providers, tool_executor=None,
                                    structured_output=s.config.conversation_structured_output, structured_sink=memory_sink,
                                    runtime_sink=wiring.telemetry_buffer.record)
        chat = self._chat_stack(tenant.id, path, core, wearer.id, connections)
        service = ApplicationService(
            path, core=core, conversation_engine=engine, lock_state=lock_state, task_runtime=task_runtime, task_catalog=task_catalog,
            chaster_connections=connections, chaster_oauth_client=s.oauth_client, chaster_token_manager=tokens,
            check_in_listener=(wiring.check_ins.on_user_message if wiring.check_ins is not None else None),
            channel_service=chat[0] if chat else None, coaching_memory=coaching, conditioning=conditioning,
            telemetry=wiring.telemetry_store, research=wiring.research_preferences, iron_contract=wiring.iron_contract, vacation=wiring.vacation)
        runtime = TenantRuntime(tenant_id=tenant.id, db_path=path, core=core, wearer_user_id=wearer.id, application_service=service,
                                wiring=wiring, process_id=process_id, clock=s.clock, notes=list(wiring.notes))
        if chat:
            chat[3]["service"] = service
            runtime.chat_adapter, runtime.chat_poller = chat[1], chat[2]
        logger.info("Tenant %s is ready (%d note(s)).", tenant.id, len(runtime.notes))
        return runtime

    # ------------------------------------------------------------------ Phase E5: per-tenant Chaster chat

    def _chat_stack(self, tenant_id: str, path: Path, core: CoreDatabase, wearer_id: str, wearer_connections):
        """(channel service, adapter, poller, holder) over THIS tenant's database, or None when chat is off or the bot is not ready.
        The wearer's Chaster account is the CONFIRMED pairing's account, or else the wearer's own connected account; the chat routes, polling
        state and seen ids live in the tenant's own tables (migrations 032 / 033)."""
        s = self._shared
        bot_id = s.config.chaster_execution_keyholder_user_id
        tokens = s.keyholder_background_tokens or s.keyholder_token_manager
        if not s.chat_enabled or not bot_id or tokens is None or s.keyholder_connections is None:
            return None
        from chaster.conversation_client import ChasterConversationClient
        from messaging.channel_service import ChannelPreferenceService
        from messaging.chaster_adapter import ChasterMessagingAdapter
        from messaging.chaster_poller import ChasterConversationPoller
        from messaging.routes import MessageRouteRepository

        registry = self._registry

        def account_of(user_id: str) -> str | None:
            if user_id != wearer_id:
                return None                                       # only this tenant's wearer has a Chaster identity here
            tenant = registry.get(tenant_id)
            if tenant is not None and tenant.lock_ready:
                return tenant.chaster_account_id
            own = wearer_connections.get(user_id) if wearer_connections is not None else None
            return None if own is None else own.chaster_account_id

        holder: dict = {}

        async def handler(unified):
            import asyncio
            out = await asyncio.to_thread(holder["service"].handle_conversation_only, unified.user_id, unified.content, s.clock.now())
            return None if out is None else out.text

        routes = MessageRouteRepository(path, core=core)
        client = s.conversation_client or ChasterConversationClient()
        channel = ChannelPreferenceService(routes=routes, connections=s.keyholder_connections, token_manager=tokens, client=client,
                                           bot_user_id=bot_id, chaster_messaging_enabled=True, chaster_account_of=account_of)
        adapter = ChasterMessagingAdapter(token_manager=tokens, client=client, connections=s.keyholder_connections, bot_user_id=bot_id,
                                          clock=s.clock, handler=handler)
        poller = ChasterConversationPoller(path, core=core, routes=routes, connections=s.keyholder_connections, token_manager=tokens,
                                           client=client, adapter=adapter, bot_user_id=bot_id, clock=s.clock, chaster_account_of=account_of)
        return channel, adapter, poller, holder
