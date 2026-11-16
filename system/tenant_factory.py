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
from system.wearer_locale import WearerLocale, WearerLocaleStore
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
    # Multi-wearer parity 3 (owner, 2026-11-13): (wearer user id, Discord id) -> an async `deliver(notification)` that reaches ONLY that wearer's
    # Discord DM. With it each tenant gets its own notification queue (its own database) and its notices go through it: penalty notices, check-in
    # warnings and release notices, with retries and the wearer's own quiet hours.
    notification_delivery_factory: Callable[[str, str], Callable] | None = None


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
    notifications: object | None = None               # notifications.dispatcher.NotificationDispatcher over THIS tenant's queue (parity 3)
    penalty_notices: dict | None = None               # system.penalty_notices handlers for THIS tenant's wearer (parity 3)
    proof_checks: object | None = None                # proof_checks.service.ProofChecks for THIS wearer (slice 8, work queue Q9)
    proof_tick: int = 0
    detectors: object | None = None                   # infractions.wiring.DetectorWiring for THIS wearer (parity 6, work queue Q6)
    exemptions: object | None = None                  # exemptions.service.ExemptionService for THIS wearer (parity 5, work queue Q5)
    opt_outs: object | None = None                    # opt_outs.service.OptOuts for THIS wearer (parity 10, owner 2026-11-16)
    features: object | None = None                    # features.service.Features for THIS wearer (parity 10)
    lines: object | None = None                       # lines.service.LineService for THIS wearer (parity 11)
    extra_consumers: dict | None = None               # further outbox consumers of THIS tenant (lines)
    obedience: object | None = None                   # obedience.service.ObedienceTasks for THIS wearer (parity 12)
    release_window: object | None = None              # release_earning.window.ReleaseWindow for THIS wearer (parity 13)
    dm_actions: dict = field(default_factory=dict)    # scope -> facade for THIS wearer's DM buttons (system/tenant_dm_actions.py)

    def handle_message(self, incoming: IncomingMessage) -> OutgoingMessage:
        """The tenant's own application entry (the Discord routing of E3 calls it). It never sees another tenant's data."""
        return self.application_service.handle_message(incoming)

    def cycle(self) -> bool:
        """One staggered periodic cycle of this tenant, under its own database lease. False when another instance holds the lease."""
        w = self.wiring
        if self.opt_outs is not None:
            try:
                self.opt_outs.tick(self.clock.now())       # parity 10: switch-offs asked about after 24 h while locked, in this tenant only
            except Exception:
                logger.exception("An opt-out pass of tenant %s failed; will retry next cycle.", self.tenant_id)
        if self.release_window is not None:
            self.release_window.tick(self.clock.now())     # parity 13: never raises (it logs and carries on)
        from system.graduation import graduation_tick       # owner, 2026-11-16: four weeks at Beginner without a severe slip: asked once
        graduation_tick(self.core, self.wearer_user_id, self.clock.now())
        if self.obedience is not None:
            try:
                self.obedience.tick(self.clock.now())      # parity 12: the daily task, on this wearer's paired lock only
            except Exception:
                logger.exception("An obedience pass of tenant %s failed; will retry next cycle.", self.tenant_id)
        if self.detectors is not None:
            try:
                self.detectors.run(self.clock.now())        # parity 6: the wearer's own detectors and questions, in this tenant only
            except Exception:
                logger.exception("A detector pass of tenant %s failed; will retry next cycle.", self.tenant_id)
        if self.proof_checks is not None:
            try:                                            # slice 8: the same rhythm as the owner's proof loop, once per cycle
                now = self.clock.now()
                self.proof_checks.start_pending(now)
                if self.proof_tick % 2 == 0 and self.proof_checks.get_open_check() is not None:
                    self.proof_checks.poll(now)
                contradictions = getattr(self.proof_checks, "contradictions", None)
                if contradictions is not None and self.proof_tick % 30 == 0:
                    contradictions.run(now)
            except Exception:
                logger.exception("A spot-check pass of tenant %s failed; will retry next cycle.", self.tenant_id)
            self.proof_tick += 1
        return run_periodic_cycle(
            self.core, self.process_id, self.clock, rules_engine=w.rules_engine, executor=w.executor, supervision=w.supervision,
            release_task=w.release_task, check_ins=w.check_ins, daily=w.daily, telemetry=w.telemetry, penalty_notices=self.penalty_notices,
            exemptions=self.exemptions, extra_consumers=self.extra_consumers)


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


def tenant_config(base: Config, *, db_path: Path, wearer_user_id: str, locale: WearerLocale | None = None) -> Config:
    """The tenant's settings: the owner's flags and tuning, but THIS tenant's database and wearer. Settings that name a person of the
    owner's system (check-in user, execution wearer) never leak into a tenant; the keyholder id is the platform's single value.
    Slice 2 (owner, 2026-11-13): the wearer's own time zone and holiday country (`timezone` / `holidays` in their DM) replace the owner's;
    a setting never made falls back to the owner's."""
    locale = locale or WearerLocale()
    zone = {"app_timezone": locale.timezone} if locale.timezone else {}
    days = {"holiday_country": locale.holiday_country} if locale.holiday_country is not None else {}
    return dataclasses.replace(
        base, db_path=db_path, chaster_execution_wearer_user_id=wearer_user_id, check_in_user_id=None,
        enable_chaster_messaging=False, chaster_llm_tools_enabled=False, enable_multi_wearer=False,
        **zone, **days)     # slice 8 (Q9): spot checks and the Honesty domain now that the invitation discloses them and `proof checks off` exists


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
        locale = WearerLocaleStore(core).get()
        cfg = tenant_config(s.config, db_path=path, wearer_user_id=wearer.id, locale=locale)
        created = ensure_domains_with_consent(core, named_trust_domains(cfg),
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
        sender = s.check_in_sender_factory(path, core) if s.check_in_sender_factory is not None else None
        queue = dispatcher = None
        if s.notification_delivery_factory is not None:
            queue, dispatcher = self._notifications(path, core, cfg, wearer.id, tenant.discord_id)
        wiring = build_rules_and_execution(
            cfg, core, connections=connections, oauth_client=s.oauth_client, lock_client=s.lock_client, token_manager=tokens,
            clock=s.clock, check_in_sender=sender, check_in_complete=s.check_in_complete,
            keyholder_connections=s.keyholder_connections, keyholder_token_manager=s.keyholder_token_manager, halt_source=s.platform_halt,
            observation_source=source, telemetry_consent_target=TENANT_CONSENT_TARGET, telemetry_tenant_id=tenant.id, notifications=queue)
        if wiring.release_engine is not None:
            providers = providers + (ReleasePhaseContextProvider(get_phase=wiring.release_phase),)
        providers = providers + (IronContractContextProvider(is_locked=wiring.iron_contract.locked_for_context),)

        process_id = str(uuid.uuid4())
        on_system_startup(core, process_id, s.clock, wiring.rules_engine, wiring.supervision, release_task=wiring.release_task)

        coaching, conditioning, memory_provider, memory_sink = coaching_memory_parts(s.config, path, core)
        if memory_provider is not None:
            providers = providers + (memory_provider,)
        coach_dm = self._coach(cfg, core, wiring, task_runtime, task_catalog, coaching)       # slice 8 (Q9)
        if coach_dm is not None:
            from conversation_engine.context_providers.coach_provider import CoachContextProvider
            providers = providers + (CoachContextProvider(read=coach_dm.context),)
        engine = ConversationEngine(model=s.model, working_memory_reader=memory, working_memory_writer=memory,
                                    queue=SubjectConversationQueue(), providers=providers, tool_executor=None,
                                    structured_output=s.config.conversation_structured_output, structured_sink=memory_sink,
                                    runtime_sink=wiring.telemetry_buffer.record)
        chat = self._chat_stack(tenant.id, path, core, wearer.id, connections)
        exemptions, exemption_reply = self._exemptions(tenant.id, path, core, cfg, db, wearer.id)
        service = ApplicationService(
            path, core=core, conversation_engine=engine, lock_state=lock_state, task_runtime=task_runtime, task_catalog=task_catalog,
            chaster_connections=connections, chaster_oauth_client=s.oauth_client, chaster_token_manager=tokens,
            check_in_listener=(wiring.check_ins.on_user_message if wiring.check_ins is not None else None),
            channel_service=chat[0] if chat else None, coaching_memory=coaching, conditioning=conditioning,
            telemetry=wiring.telemetry_store, research=wiring.research_preferences, iron_contract=wiring.iron_contract, vacation=wiring.vacation,
            exemption_reply=exemption_reply, coach=coach_dm)
        runtime = TenantRuntime(tenant_id=tenant.id, db_path=path, core=core, wearer_user_id=wearer.id, application_service=service,
                                wiring=wiring, process_id=process_id, clock=s.clock, notes=list(wiring.notes))
        detectors = self._detectors(path, core, cfg, wearer.id, tokens, connections, wiring)
        if detectors is not None and detectors.signals is not None:
            from system.dm_actions import InfractionDm, discord_user_resolver as _resolver
            runtime.detectors = detectors
            service.explanations = detectors.signals
            runtime.dm_actions["infraction"] = InfractionDm(detectors.signals, resolve_user=_resolver(core), now=s.clock.now)
            if exemptions is not None:
                from infractions.wiring import overrun_signal
                exemptions.set_overrun_signal(overrun_signal(detectors.signals, trust_domain=cfg.hygiene_trust_domain or "hygiene"))
            runtime.notes.extend(detectors.notes)
            if detectors.hygiene is not None:
                from system.dm_actions import HygieneDm
                runtime.dm_actions["hygiene"] = HygieneDm(detectors.hygiene, resolve_user=_resolver(core), now=s.clock.now)
            # Parity 8 (work queue Q8): a confession takes the same deterministic path as the owner's (a hygiene confession is offered as
            # a self-report, with the same buttons; the model only adds its CONFESSION_NOTE tone).
            from infractions.self_report import is_hygiene_confession
            from infractions.signals import SELF_REPORT_QUESTION, self_report_buttons

            def offer_self_report(user_id, text, now, signals=detectors.signals, domain=cfg.hygiene_trust_domain or "hygiene"):
                if not is_hygiene_confession(text):
                    return None
                signal_id = signals.offer_self_report(user_id=user_id, statement=text, trust_domain=domain, now=now)
                return (SELF_REPORT_QUESTION, self_report_buttons(signal_id)) if signal_id else None

            service.self_reports = offer_self_report
            service.confession_check = is_hygiene_confession
        self._photo_check_ins(runtime, service, wiring, wearer.id, tokens, connections)
        self._switches(runtime, service, cfg, core, wiring)
        if coach_dm is not None:
            if wiring.check_ins is not None:
                wiring.check_ins.prompt_suffix = coach_dm.prompt_suffix
            self._proof_checks(tenant.id, runtime, service, cfg, core, coach_dm, wiring)
        if exemptions is not None:
            from system.dm_actions import ExemptionDm, discord_user_resolver
            runtime.exemptions = exemptions
            service.exemption_reason = lambda user_id, text, now, ex=exemptions: ex.give_reason(user_id, text, now) if ex.awaiting_reason(user_id) else None
            resolve = discord_user_resolver(core)
            runtime.dm_actions["exemption"] = ExemptionDm(exemptions, resolve_user=resolve, now=s.clock.now)
        self._lines(runtime, service, core, wiring, wearer.id)        # after the exemptions: owed lines hold an opening
        self._obedience(runtime, service, cfg, core)                  # after the spot checks and the detectors it needs
        self._release_and_relock(runtime, cfg, core, wiring, tokens, connections)   # after the lines and the obedience it reads
        if dispatcher is not None:
            from system.penalty_notices import penalty_notice_handlers
            runtime.notifications, runtime.penalty_notices = dispatcher, penalty_notice_handlers(wearer.id)
        if chat:
            chat[3]["service"] = service
            runtime.chat_adapter, runtime.chat_poller = chat[1], chat[2]
        logger.info("Tenant %s is ready (%d note(s)).", tenant.id, len(runtime.notes))
        return runtime

    # ------------------------------------------------------------------ parity 10: feature switches and the 24-hour switch-off wait

    def _switches(self, runtime, service, cfg, core: CoreDatabase, wiring) -> None:
        """Owner, 2026-11-16 (multi-wearer first): the wearer's own `features` switches and G3's wait (photo check-ins, conditioning, the
        weekly review and every feature switch-off wait 24 hours while locked, then are asked about), over THIS tenant's database."""
        from features.service import FEATURES, Features
        from opt_outs.service import SWITCHES, OptOuts
        from system.coach_dm import goal_targets_frozen
        from system.composition import window_config_from
        from system.dm_actions import OptOutDm, discord_user_resolver
        window = window_config_from(cfg)
        features = Features(core)
        opt_outs = OptOuts(core, locked=goal_targets_frozen(wiring.iron_contract),
                           local_clock=lambda t, c=window: c.local(t).strftime("%a %d %b %H:%M"))
        for key in SWITCHES:
            opt_outs.register(key, lambda user, now, k=key: service.apply_off(k, user, now))
        opt_outs.register_features(features.apply_off, label=lambda key: FEATURES[key][0] if key in FEATURES else None)
        features.request_off, features.withdraw = opt_outs.request_off, opt_outs.withdraw
        service.opt_outs, service.features_command = opt_outs, features.command
        runtime.opt_outs, runtime.features = opt_outs, features
        runtime.dm_actions["optout"] = OptOutDm(opt_outs, resolve_user=discord_user_resolver(core), now=self._shared.clock.now)

    # ------------------------------------------------------------------ parity 13: earning the release, its window, the relock picture

    def _release_and_relock(self, runtime, cfg, core: CoreDatabase, wiring, tokens, connections) -> None:
        """Owner, 2026-11-16 (multi-wearer first), all over THIS wearer's database and Chaster connection:
        - earning the release: a token waits for that day's verified training session or the release lines (the owner's rule);
        - the release window: 2 hours to relock and send a verification picture, unless their `release relock` switch is off;
        - the relock picture after a planned hygiene opening while their hygiene Trust is below 0.70.
        The pictures come from the locks the wearer wears, through their own connection; without it there is no window and no relock
        picture check (unknown never counts against them)."""
        wearer_id = runtime.wearer_user_id
        release_task = wiring.release_task if getattr(wiring.release_task, "user_id", None) == wearer_id else None
        if release_task is not None and runtime.lines is not None:
            from obedience.service import get_training_verified_since
            from release_earning.service import ReleaseEarning
            training = getattr(runtime.obedience, "training", None) if runtime.obedience is not None else None
            earning = ReleaseEarning(
                user_id=wearer_id, last_spend_at=lambda rt=release_task: rt.engine.get_last_spend_at(rt.user_id),
                training_verified_since=lambda since: get_training_verified_since(core, wearer_id, since),
                training_today=(lambda now, tp=training: tp.is_training_day_now(now)) if training is not None else (lambda now: False),
                release_lines_done_since=runtime.lines.release_done_since, assign_release_lines=runtime.lines.assign_release)
            release_task.release_earned = earning.hold
        if tokens is None or connections is None or runtime.detectors is None or runtime.detectors.signals is None:
            return
        from chaster.hygiene_policy import HYGIENE_PICTURE_BELOW
        from chaster.lock_directory import LockDirectory
        from chaster.peer_reviews import PeerReviewReader
        from infractions.signals import SignalKind
        from infrastructure.time_format import parse_iso
        from system.composition import window_config_from
        from trust_manager.repository import TrustManager
        s, signals = self._shared, runtime.detectors.signals
        domain = cfg.hygiene_trust_domain or "hygiene"
        directory = LockDirectory(token_manager=tokens, lock_client=s.lock_client, connections=connections)
        reader = PeerReviewReader(token_manager=tokens, lock_client=s.lock_client)

        def pictures(start, end):
            now = s.clock.now()
            listing = directory.list_account_locks(user_id=wearer_id, now=now)
            ids = [lock.lock_id for lock in listing.wearer_locks if lock.status == "locked"]
            if not ids:
                return None
            times = reader.picture_times(user_id=wearer_id, lock_ids=ids, now=now)
            return None if times is None else any(start <= t <= end for t in times)

        if runtime.exemptions is not None:
            def hygiene_score():
                state = TrustManager(core.db_path, core=core).get_domain_state(domain)
                return None if state is None else float(state.score)

            def raise_missing_picture(row, now):
                signals.raise_signal(user_id=row["user_id"], kind=SignalKind.RELOCK_PICTURE_MISSING, subject_id=row["id"], trust_domain=domain,
                                     now=now, detail="No verification picture within 30 minutes of relocking after a planned hygiene opening.",
                                     wearer_text="No relock picture arrived in Chaster within 30 minutes of closing your hygiene opening. What happened?",
                                     facts=f"Planned hygiene opening relocked at {row['relocked_at']}; no verification picture by 30 minutes later.",
                                     window=(parse_iso(row["relocked_at"]), now))

            runtime.exemptions.relock_pictures = pictures
            runtime.exemptions.picture_required = lambda _at: (lambda score: score is not None and score < HYGIENE_PICTURE_BELOW)(hygiene_score())
            runtime.exemptions.raise_missing_picture = raise_missing_picture
        if release_task is not None:
            from features.service import get_feature_on
            from release_earning.window import ReleaseWindow
            window = window_config_from(cfg)

            def raise_overrun(row, now):
                signals.raise_signal(user_id=wearer_id, kind=SignalKind.RELEASE_WINDOW_OVERRUN, subject_id=row["authorization_id"], trust_domain=domain,
                                     now=now, detail="No verification picture within the 2-hour window after a release.",
                                     wearer_text="Your release window ended without a verification picture in Chaster. What happened?",
                                     facts=f"Release confirmed at {row['confirmed_at']}; no verification picture by {row['due_at']} + 10 minutes.",
                                     window=(parse_iso(row["confirmed_at"]), now))

            release_window = ReleaseWindow(core, user_id=wearer_id, last_release=lambda rt=release_task: rt.engine.last_confirmed_release(rt.user_id),
                                           pictures=pictures, local_clock=lambda t, c=window: c.local(t).strftime("%H:%M"), raise_overrun=raise_overrun)
            release_window.enabled = lambda: get_feature_on(core, wearer_id, "release_relock")
            runtime.release_window = release_window

    # ------------------------------------------------------------------ parity 12: obedience tasks

    # A new wearer's limits and gear are unknown until onboarding asks (roadmap D8): these start off and the gear list starts empty.
    SEEDED_OFF = ("erotic_tasks", "workouts", "temptations", "protocol")   # owner, 2026-11-16: the beginner level starts with these off

    def _obedience(self, runtime, service, cfg, core: CoreDatabase) -> None:
        """Owner, 2026-11-16 (multi-wearer first): the daily obedience task for an additional wearer, on THEIR confirmed paired lock through
        their spot checks' Chaster setup, missed ones through their own detector questions. Erotic tasks and workouts start OFF (their
        limits and gear are not known yet; `erotic tasks on` / `workouts on` start them) and the gear list starts empty (`gear add`).
        The training plan is the owner's own programme: a wearer who switches workouts on gets it as it is until onboarding sets theirs."""
        if runtime.features is None:
            return
        runtime.features.seed_off(runtime.wearer_user_id, self.SEEDED_OFF, self._shared.clock.now())
        from advanced_mode.repository import AdvancedModeAdministration       # owner, 2026-11-16: a new wearer starts at Beginner
        AdvancedModeAdministration(core.db_path, core=core).start_new_wearer_at_beginner(now=self._shared.clock.now())
        if runtime.proof_checks is None or runtime.detectors is None or runtime.detectors.signals is None:
            runtime.notes.append("Daily obedience tasks are NOT active for this wearer: they need the spot checks and the detector questions.")
            return
        from system.composition import window_config_from
        from system.dm_actions import ObedienceDm, discord_user_resolver
        from system.obedience_wiring import build_obedience
        obedience = build_obedience(cfg, core, proof_checks=runtime.proof_checks, signals=runtime.detectors.signals,
                                    window_cfg=window_config_from(cfg), notes=runtime.notes, exemptions=runtime.exemptions,
                                    features=runtime.features)
        if obedience is None:
            return
        obedience.default_owned = frozenset()
        runtime.obedience = obedience
        service.gear_command = obedience.gear_command
        runtime.dm_actions["obedience"] = ObedienceDm(obedience, resolve_user=discord_user_resolver(core), now=self._shared.clock.now)
        if runtime.opt_outs is not None:
            from obedience.service import GEAR
            opt_outs = runtime.opt_outs
            opt_outs.register_gear(lambda user, piece, now, ob=obedience: ob.set_gear(piece, 0, now, reason="asked_after_24h"),
                                   label=lambda piece: GEAR.get(piece, piece))
            obedience.remove_wait = lambda user, piece, now, o=opt_outs: o.request_off(user, f"gear:{piece}", now)
            obedience.add_hook = lambda user, piece, now, o=opt_outs: o.withdraw(user, f"gear:{piece}", now)

    # ------------------------------------------------------------------ parity 11: lines

    def _lines(self, runtime, service, core: CoreDatabase, wiring, wearer_id: str) -> None:
        """Owner, 2026-11-16 (multi-wearer first): lines for every confirmed incident, typed in the DM (paper lines stay owner-only), with
        swats or a written request as the other choices; owed lines hold the next hygiene opening and a release. The wearer's `lines`
        feature switch applies. Everything over THIS tenant's database."""
        from features.service import get_feature_on_in_transaction
        from lines.service import LineService
        from system.dm_actions import ObedienceDm, discord_user_resolver
        line_service = LineService(core, user_id=wearer_id)
        line_service.enabled = lambda tx, u=wearer_id: get_feature_on_in_transaction(tx, u, "lines")
        runtime.lines, runtime.extra_consumers = line_service, {"lines": line_service.consumers()}
        service.lines = line_service
        runtime.dm_actions["lines"] = ObedienceDm(line_service, resolve_user=discord_user_resolver(core), now=self._shared.clock.now)
        if runtime.exemptions is not None:
            runtime.exemptions.lines_open = line_service.open_lines
        if wiring.release_task is not None and getattr(wiring.release_task, "user_id", None) == wearer_id:
            wiring.release_task.release_debt = lambda u=wearer_id: line_service.open_lines(u)

    # ------------------------------------------------------------------ slice 8: the coach and the spot checks (work queue Q9)

    def _coach(self, cfg, core: CoreDatabase, wiring, task_runtime, task_catalog, coaching):
        if not getattr(cfg, "enable_coach", False):
            return None
        from datetime import time as dt_time, timedelta
        from system.coach_dm import active_task_reader, build_coach, goal_targets_frozen
        from system.composition import window_config_from
        window = window_config_from(cfg)
        return build_coach(cfg, core, clock=self._shared.clock, targets_frozen=goal_targets_frozen(wiring.iron_contract), paused=wiring.vacation.is_paused,
                           today=lambda now: window.local(now).date(), local_now=window.local,
                           active_task=active_task_reader(task_runtime, task_catalog), memory=coaching,
                           day_bounds=lambda d: (window.to_utc(d, dt_time(0, 0)), window.to_utc(d + timedelta(days=1), dt_time(0, 0))),
                           to_utc=window.to_utc, quiet=(window.quiet_start, window.quiet_end))

    def _proof_checks(self, tenant_id: str, runtime, service, cfg, core: CoreDatabase, coach_dm, wiring) -> None:
        """Spot checks on THIS wearer's confirmed paired lock (a Chaster task through the platform keyholder), on by default once disclosed,
        `proof checks off` at any time. Needs the detector questions (a failed check is asked about first)."""
        detectors = runtime.detectors
        if detectors is None or detectors.signals is None:
            runtime.notes.append("Spot checks are NOT active for this wearer: they need the detector questions (the wearer's Chaster connection).")
            return
        from system.composition import window_config_from
        from system.dm_actions import ProofDm, discord_user_resolver
        from system.proof_wiring import build_proof_checks
        s = self._shared

        class _TenantLock:
            def current(self, now=None, tid=tenant_id, registry=self._registry):
                tenant = registry.get(tid)
                return tenant.lock_id if tenant is not None and tenant.lock_ready else None

        notes: list[str] = []
        checks = build_proof_checks(dataclasses.replace(cfg, release_lock_id="auto"), core, coach_dm=coach_dm, signals=detectors.signals,
                                    keyholder_tokens=s.keyholder_background_tokens or s.keyholder_token_manager, lock_client=s.lock_client,
                                    owner_lock=_TenantLock(), vacation=wiring.vacation, window_cfg=window_config_from(cfg), clock=s.clock, notes=notes)
        runtime.notes.extend(notes)
        if checks is None:
            return
        runtime.proof_checks = checks
        runtime.dm_actions["proof"] = ProofDm(checks, resolve_user=discord_user_resolver(core), now=s.clock.now)
        service.proof_switch = checks.set_switch

    # ------------------------------------------------------------------ parity 7: photo check-ins (work queue Q7)

    def _photo_check_ins(self, runtime, service, wiring, wearer_id: str, tokens, connections) -> None:
        """Off until the wearer sends `photo check-ins on` themselves (the consent fails closed). The pictures are read from the locks THIS
        wearer wears, through their own connection; none is sent during their wear exemption."""
        if wiring.check_ins is None or tokens is None or connections is None:
            return
        from chaster.lock_directory import LockDirectory
        from chaster.peer_reviews import PeerReviewReader
        from supervision.photo_check_ins import PhotoCheckInSwitch
        s = self._shared
        directory = LockDirectory(token_manager=tokens, lock_client=s.lock_client, connections=connections)
        reader = PeerReviewReader(token_manager=tokens, lock_client=s.lock_client)

        def photo_times(now):
            listing = directory.list_account_locks(user_id=wearer_id, now=now)
            ids = [lock.lock_id for lock in listing.wearer_locks if lock.status == "locked"]
            return reader.picture_times(user_id=wearer_id, lock_ids=ids, now=now) if ids else None

        wiring.check_ins.photo_times = photo_times
        wiring.check_ins.photo_suspended = (lambda now, rt=runtime, u=wearer_id:
                                            rt.exemptions is not None and rt.exemptions.wear_exemption_in_force(u))
        service.photo_check_ins = PhotoCheckInSwitch(consent=wiring.check_ins.photo_consent, user_id=wearer_id)

    # ------------------------------------------------------------------ parity 6: detectors and the judge (work queue Q6)

    def _detectors(self, path: Path, core: CoreDatabase, cfg, wearer_id: str, tokens, connections, wiring):
        """Hygiene misuse, lock claims and the one question-and-judge path, through THIS wearer's own Chaster connection and signals;
        incidents go to the tenant's own Penalty Engine. Needs the wearer's Chaster connection; without it nothing is watched (a note says so)."""
        s = self._shared
        if tokens is None or connections is None:
            return None
        from conversation_engine.model_types import ModelGenerationRequest, ModelMessage, ModelMessageRole
        from infractions.wiring import build_detectors
        complete = None
        if s.model is not None:
            def complete(system_prompt: str, user_message: str, model=s.model) -> str:
                return model.generate(request=ModelGenerationRequest(
                    messages=(ModelMessage(role=ModelMessageRole.SYSTEM, content=system_prompt), ModelMessage(role=ModelMessageRole.USER, content=user_message)),
                    max_output_characters=600, deterministic=True))
        # The watches list the locks THIS wearer wears, through their own connection; the "owner" of a tenant's questions is the wearer.
        own = dataclasses.replace(cfg, chaster_execution_keyholder_user_id=wearer_id, owner_user_id=wearer_id)
        return build_detectors(own, core, token_manager=tokens, lock_client=s.lock_client, connections=connections,
                               vacation=wiring.vacation, complete=complete)

    # ------------------------------------------------------------------ parity 5: exemptions and hygiene openings (work queue Q5)

    def _exemptions(self, tenant_id: str, path: Path, core: CoreDatabase, cfg, db, wearer_id: str):
        """Same rules as the owner's, in THIS wearer's database: the judge decides requests (no keyholder button), hygiene openings follow the
        wearer's own hygiene Trust, and an opening acts only on the tenant's CONFIRMED paired lock after the owner cleared this tenant's execution
        halt. The relock picture, lines and recovery tasks stay owner-only (owner, 2026-11-14). (None, None) when exemptions are off."""
        s = self._shared
        if not getattr(s.config, "enable_exemptions", False):
            return None, None
        from datetime import timedelta
        from conversation_engine.model_types import ModelGenerationRequest, ModelMessage, ModelMessageRole
        from exemptions.judge import ExemptionJudge
        from exemptions.service import ExemptionService, is_exemption_request, is_hygiene_request
        from infractions.hygiene import policy_for
        from system.composition import build_tenant_exemption_opening
        from system.penalty_policy import make_penalty_engine
        from trust_manager.repository import TrustManager

        def lock_of(tid=tenant_id):
            tenant = self._registry.get(tid)
            return tenant.lock_id if tenant is not None and tenant.lock_ready else None

        opening = build_tenant_exemption_opening(cfg, core, token_manager=s.keyholder_token_manager, lock_client=s.lock_client, lock_of=lock_of,
                                                 halt_source=s.platform_halt, clock=s.clock)
        domain = cfg.hygiene_trust_domain or "hygiene"

        def allowance(trust=TrustManager(path, core=core)):
            state = trust.get_domain_state(domain)
            policy = policy_for(float(state.score) if state is not None else 0.0)
            return policy.breaks_per_week, policy.session_minutes

        service = ExemptionService(path, core=core, owner_user_id=None, penalties=make_penalty_engine(core), opening=opening, hygiene_allowance=allowance)
        service.hygiene_trust_domain = domain
        if s.model is not None:
            def complete(system_prompt: str, user_message: str, model=s.model) -> str:
                return model.generate(request=ModelGenerationRequest(
                    messages=(ModelMessage(role=ModelMessageRole.SYSTEM, content=system_prompt), ModelMessage(role=ModelMessageRole.USER, content=user_message)),
                    max_output_characters=600, deterministic=True))
            service.set_judge(ExemptionJudge(complete))

        def reply(user_id, text, now):
            if user_id != wearer_id:
                return None
            if is_hygiene_request(text):
                return service.start_hygiene(user_id, now)
            if not is_exemption_request(text):
                return None
            try:
                earlier = [t for t in db.get_user_texts_since(now - timedelta(minutes=15)) if t != text][-3:]
            except Exception:
                earlier = []
            return service.start(user_id, now, said="\n".join([*earlier, text]))

        return service, reply

    # ------------------------------------------------------------------ parity 3: the tenant's own notification queue

    def _notifications(self, path: Path, core: CoreDatabase, cfg, wearer_id: str, discord_id: str):
        """(queue, dispatcher) over THIS tenant's database. Delivery reaches only this tenant's wearer (any other user id is undeliverable), and
        routine notices wait for the end of the wearer's own quiet hours (an unreadable layout holds them, like the owner's)."""
        from notifications.dispatcher import NotificationDispatcher
        from notifications.queue import NotificationQueue
        from schedule_windows.windows import WindowKind, classify
        from system.composition import window_config_from

        try:
            windows = window_config_from(cfg)
        except ValueError:
            windows = None

        def is_quiet(now) -> bool:
            if windows is None:
                raise ValueError("no valid quiet hours")
            return classify(windows, now) is WindowKind.QUIET

        queue = NotificationQueue(path, core=core)
        return queue, NotificationDispatcher(queue, deliver=self._shared.notification_delivery_factory(wearer_id, discord_id), is_quiet=is_quiet)

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
