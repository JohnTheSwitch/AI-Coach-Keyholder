"""
bot/discord_bot.py

The Discord adapter — Phase 3.1's minimal vertical slice. Deliberately
a THIN layer: this file's only responsibilities are

  1. connecting to Discord and filtering to direct messages,
  2. converting a discord.Message into a channel-agnostic
     application.models.IncomingMessage,
  3. calling application.service.ApplicationService.handle_message()
     (the ONLY call this file makes into the rest of the system --
     never a domain module directly, never a `_*_in_transaction` method),
  4. sending the returned OutgoingMessage.text back to the same DM,
  5. raw audit logging of both sides of the exchange (Phase 0's
     conversation_messages table -- an adapter-level concern, since the
     Discord-specific channel/message ids it records are inherently
     about THIS channel, not something the channel-agnostic application
     layer should know about),
  6. catching and logging any exception so a single bad message can
     never crash the bot or leak an internal error to the user.

No command parsing, no domain logic, and no decision-making lives here
-- see application/README.md for the actual boundary and
application/service.py for what "status"/"help" actually do.

Running:
    python -m bot.discord_bot
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import discord

from application.models import EmbedSpec, IncomingMessage
from application.service import ApplicationService
from chaster.callback_listener import ChasterCallbackListener
from chaster.callback_service import ChasterCallbackService, build_real_identity_resolver
from chaster.llm_tools import ChasterToolExecutor
from chaster.lock_client import ChasterLockClient
from chaster.oauth_client import ChasterOAuthClient
from chaster.repository import ChasterConnectionRepository
from chaster.token_encryptor import TokenEncryptor, TokenEncryptorConfigurationError
from chaster.token_manager import ChasterTokenManager
from conversation_engine.context_providers.active_task_provider import ActiveTaskContextProvider
from conversation_engine.context_providers.iron_contract_provider import IronContractContextProvider
from conversation_engine.context_providers.lock_state_provider import LockStateContextProvider
from conversation_engine.engine import ConversationEngine
from conversation_engine.ollama_adapter import OllamaConversationModel
from conversation_engine.subject_queue import SubjectConversationQueue
from lock_state.repository import LockState
from task_catalog.repository import TaskCatalog
from task_runtime.repository import TaskRuntime
from core.config import Config, ConfigError
from memory_system.working_memory import InMemoryWorkingMemory
from database.database import Database
from database.models import ConversationMessage, MessageRole
from infrastructure.clock import Clock, SystemClock
from infrastructure.database import Database as CoreDatabase
from bot.checkin_sender import DiscordCheckInSender
from conversation_engine.context_providers.release_phase_provider import ReleasePhaseContextProvider
from conversation_engine.model_types import ModelGenerationRequest, ModelMessage, ModelMessageRole
from exemptions.service import ExemptionService, is_exemption_request
from infractions.wiring import build_detectors, detector_loop, overrun_signal
from trust_manager.repository import TrustManager
from notifications.delivery import NotificationDelivery
from notifications.dispatcher import NotificationDispatcher, notification_loop
from notifications.queue import NotificationQueue
from schedule_windows.windows import WindowKind, classify
from system.composition import build_exemption_opening, build_extension_guard, build_rules_and_execution, extension_guard_loop, research_export_loop_for
from system.composition import window_config_from as quiet_config_from
from system.dm_actions import ExemptionDm, HygieneDm, InfractionDm, discord_user_resolver
from system.penalty_notices import penalty_notice_handlers
from system.penalty_policy import make_penalty_engine
from chaster.api_budget import ApiBudget, BudgetedTokenManager, Lane
from chaster.conversation_client import ChasterConversationClient
from tenancy.services import open_platform_services
from system.tenant_factory import SharedServices, TenantFactory, coaching_memory_parts
from system.tenant_loops import TenantLoopSupervisor
from system.tenant_gateway import InvitationNotifier, TenantGateway
from system.lock_pairing import LockPairingService, wearer_account_lookup
from tenancy.admin import TenantAdmin
from tenancy.archive import TenantArchiver
from bot.action_buttons import actions_view
from messaging.channel_service import ChannelPreferenceService
from messaging.chaster_adapter import ChasterMessagingAdapter
from messaging.chaster_loop import run_chaster_chat_loop
from messaging.chaster_poller import ChasterConversationPoller
from messaging.discord_adapter import DiscordMessagingAdapter
from messaging.router import MessageRouter
from messaging.routes import MessageRouteRepository
from system.periodic import periodic_loop
from system.startup import StartupLeaseNotAcquired, on_system_startup

logger = logging.getLogger("ai_coach_keyholder.bot")

_EMBED_COLOR_BY_NAME = {"red": discord.Color.red(), "blue": discord.Color.blue(), "green": discord.Color.green()}


def _build_discord_embed(spec: EmbedSpec) -> discord.Embed:
    """Translates a channel-agnostic EmbedSpec (application/models.py)
    into a real discord.Embed -- the only place in this project that
    does so; application/ itself never imports discord, per its own
    header docstring. An unrecognized color name falls back to the
    default embed color rather than raising -- a cosmetic detail is
    not a reason to fail an otherwise-valid reply."""
    embed = discord.Embed(title=spec.title, description=spec.description, color=_EMBED_COLOR_BY_NAME.get(spec.color))
    for f in spec.fields:
        embed.add_field(name=f.name, value=f.value, inline=False)
    return embed


class CoachKeyholderBot(discord.Client):
    def __init__(
        self, config: Config, db: Database, clock: Clock, application_service: ApplicationService,
        *, intents: discord.Intents,
    ):
        super().__init__(intents=intents)
        self.config = config
        self.db = db
        self.clock = clock
        self.application_service = application_service
        self._chaster_listener: ChasterCallbackListener | None = None
        # (core, rules_engine, executor, interval_seconds) when the Phase 2/3 periodic cycle is on.
        self.periodic: tuple | None = None
        self.gateway = None                     # system.tenant_gateway.TenantGateway when multi-wearer is on (Phase E3)
        self.invitations = None
        self._invite_task: asyncio.Task | None = None
        self.tenant_supervisor = None           # system.tenant_loops.TenantLoopSupervisor when multi-wearer is on (Phase E2)
        self._tenant_task: asyncio.Task | None = None
        self.platform = None                    # tenancy.services.PlatformServices when ENABLE_MULTI_WEARER (Phase E1)
        self.messaging_router = None            # set by main(): MessageRouter (Phase B)
        self.chaster_chat: tuple | None = None   # (poller, adapter, interval_seconds) when Chaster chat is active
        self._periodic_task: asyncio.Task | None = None
        self._chat_task: asyncio.Task | None = None
        self.research_export = None              # Phase G4: the export coroutine (system.composition.research_export_loop_for) when configured
        self._research_task: asyncio.Task | None = None
        self.penalty_barrier = None              # Phase J2: chaster_execution.penalty_barrier.PenaltyBarrierExecutor, run by the periodic cycle
        self.peer_reviews = None                 # Phase I4: the peer_review.wiring.peer_review_loop coroutine when the watch is active
        self.peer_review_buttons = None          # Phase I4: peer_review.discord_buttons.PeerReviewButtonHandler (owner's one-click decisions)
        self.review_consent_dm = None            # Phase J1: system.review_consent_dm.ReviewConsentDm (owner's consent buttons)
        self.coach_dm = None                     # Phase J3: system.coach_dm.CoachDm (the wearer's Accept / Decline / outcome buttons)
        self._peer_review_task: asyncio.Task | None = None
        self.notifications = None                # notifications.dispatcher.notification_loop coroutine
        self._notification_task: asyncio.Task | None = None
        self.detectors = None                    # infractions.wiring.detector_loop coroutine when a watch is active
        self._detector_task: asyncio.Task | None = None
        self.extension_guard = None              # chaster_execution.extension_guard.extension_guard_loop coroutine
        self._extension_guard_task: asyncio.Task | None = None
        self.dm_actions: dict = {}               # scope -> facade behind bot.action_buttons.DmActionButton
        self.exemptions = None                   # exemptions.service.ExemptionService, ticked by the periodic cycle
        self.penalty_notices = None              # system.penalty_notices handlers, registered as outbox consumers

    async def setup_hook(self) -> None:
        """Called once, on this same event loop, before the websocket
        connects (discord.py's own documented lifecycle -- see
        docs/architecture/chaster_integration_technical_design.md
        Section 9). Starts the Chaster OAuth callback listener IF
        Chaster is fully configured -- CRITICAL: any failure here is
        caught and logged, never allowed to propagate, since an
        uncaught exception in setup_hook() would prevent Discord
        itself from ever connecting. Chaster callback infrastructure
        being unavailable must never take down the whole bot."""
        if self.periodic is not None:
            core, rules_engine, executor, interval, supervision, release_task, check_ins, daily, telemetry = self.periodic
            self._periodic_task = asyncio.create_task(
                periodic_loop(core, str(uuid.uuid4()), self.clock, rules_engine=rules_engine, executor=executor,
                              interval_seconds=interval, supervision=supervision,
                              release_task=release_task, check_ins=check_ins, daily=daily, telemetry=telemetry,
                              penalty_barrier=self.penalty_barrier, exemptions=self.exemptions, penalty_notices=self.penalty_notices)
            )
        if self.tenant_supervisor is not None:
            self._tenant_task = asyncio.create_task(self.tenant_supervisor.run(), name="tenant-supervisor")
        if self.invitations is not None:
            self._invite_task = asyncio.create_task(self.invitations.run(), name="tenant-invitations")
        if self.research_export is not None:
            self._research_task = asyncio.create_task(self.research_export, name="research-export")
        if self.peer_reviews is not None:
            self._peer_review_task = asyncio.create_task(self.peer_reviews, name="peer-review-watch")
        if self.notifications is not None:
            self._notification_task = asyncio.create_task(self.notifications, name="notifications")
        if self.detectors is not None:
            self._detector_task = asyncio.create_task(self.detectors, name="detectors")
        if self.extension_guard is not None:
            self._extension_guard_task = asyncio.create_task(self.extension_guard, name="extension-guard")
        if self.dm_actions:
            from bot.action_buttons import DmActionButton
            self.add_dynamic_items(DmActionButton)
        if self.peer_review_buttons is not None:
            from peer_review.discord_buttons import PeerReviewDecisionButton
            self.add_dynamic_items(PeerReviewDecisionButton)    # persistent: buttons on older notices keep working after a restart
        if self.review_consent_dm is not None:
            from bot.action_buttons import ReviewConsentButton
            self.add_dynamic_items(ReviewConsentButton)
        if self.coach_dm is not None:
            from bot.action_buttons import CoachButton
            self.add_dynamic_items(CoachButton)
        if self.chaster_chat is not None:
            poller, adapter, interval = self.chaster_chat
            self._chat_task = asyncio.create_task(run_chaster_chat_loop(poller, adapter, interval_seconds=interval))
        if self.application_service.chaster_connections is None or self.application_service.chaster_oauth_client is None:
            logger.info("Chaster is not configured -- callback listener not started.")
            return
        try:
            service = ChasterCallbackService(
                states=self.application_service.chaster_states,
                connections=self.application_service.chaster_connections,
                oauth_client=self.application_service.chaster_oauth_client,
                resolve_identity=build_real_identity_resolver(self.application_service.chaster_oauth_client),
            )

            async def on_callback(state: str | None, code: str | None, error: str | None) -> str:
                outcome = await asyncio.to_thread(
                    service.handle_callback, state=state, code=code, error=error, now=self.clock.now(),
                )
                return outcome.message

            listener = ChasterCallbackListener(
                bind_host=self.config.chaster_callback_bind_host,
                bind_port=self.config.chaster_callback_bind_port,
                on_callback=on_callback,
            )
            await listener.start()
            self._chaster_listener = listener
        except Exception:
            logger.exception(
                "Chaster callback listener failed to start -- Chaster integration is unavailable "
                "this run, but Discord itself is unaffected."
            )
            self._chaster_listener = None

    async def close(self) -> None:
        if self._periodic_task is not None:
            self._periodic_task.cancel()
            self._periodic_task = None
        if self._chat_task is not None:
            self._chat_task.cancel()
            self._chat_task = None
        if self._tenant_task is not None:
            self._tenant_task.cancel()
            self._tenant_task = None
        if self._invite_task is not None:
            self._invite_task.cancel()
            self._invite_task = None
        if self._research_task is not None:
            self._research_task.cancel()
            self._research_task = None
        if self._peer_review_task is not None:
            self._peer_review_task.cancel()
            self._peer_review_task = None
        if self._chaster_listener is not None:
            try:
                await self._chaster_listener.stop()
            except Exception:
                logger.exception("Error stopping the Chaster callback listener during shutdown -- continuing.")
            self._chaster_listener = None
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s)", self.user, self.user.id if self.user else "?")
        logger.info("Ready. Listening for direct messages only (Phase 3.1 scope).")

    async def on_message(self, message: discord.Message) -> None:
        # Ignore the bot's own messages so it doesn't loop on itself.
        if message.author == self.user:
            return

        # DM only, per this phase's explicit scope -- anything sent in
        # a server channel is silently ignored, not processed and not
        # replied to.
        if not isinstance(message.channel, discord.DMChannel):
            return

        if self.gateway is not None:
            await self._on_message_via_gateway(message)
            return

        now = self.clock.now()

        # Audit logging is best-effort and never blocks the actual
        # conversation -- a failure here must not prevent a reply from
        # being generated or sent (a real gap found while writing this
        # adapter's own tests: the first implementation coupled the
        # incoming-message log with reply generation in one try/except,
        # so a logging failure produced the generic error reply instead
        # of the real one, and the OUTGOING log was unprotected entirely,
        # able to crash the handler after the user had already gotten a
        # reply).
        self._log_message_best_effort(now, MessageRole.USER, message.content, message.channel.id, message.id)

        try:
            incoming = IncomingMessage(
                channel="discord", external_user_id=str(message.author.id),
                text=message.content, received_at=now, external_message_id=str(message.id),
            )
            # ApplicationService.handle_message() itself never raises
            # (see its own docstring) -- this try/except is a second,
            # independent safety net in case something fails before or
            # after that call, not a substitute for that guarantee.
            # asyncio.to_thread() runs the whole synchronous
            # handle_message() call (including any blocking Ollama HTTP
            # call it may make for Slice 2's own conversational path) on
            # a worker thread -- never on this event loop. Database is
            # safe to call concurrently from multiple threads now (see
            # infrastructure/database.py's own thread-local guard).
            outgoing = await asyncio.to_thread(self.application_service.handle_message, incoming)
            reply_text = outgoing.text
            reply_embed = _build_discord_embed(outgoing.embed) if outgoing.embed is not None else None
            reply_view = actions_view(outgoing.actions) if outgoing.actions else None      # Phase J1: buttons, if any
        except Exception:
            logger.exception("Unhandled error processing a DM from user_id=%s", message.author.id)
            reply_text = "Something went wrong handling that. It's been logged."
            reply_embed = reply_view = None

        # Whatever state this reply reflects (including any onboarding
        # step transition) was already written by handle_message()
        # above -- write-before-send throughout
        # application/onboarding_service.py specifically so a failure
        # here can never leave persisted state inconsistent with what
        # was (or wasn't) shown to the user. Wrapped, not left to
        # propagate: a transient Discord API failure sending one reply
        # is not a reason to let an unhandled exception surface through
        # discord.py's own event dispatch -- the same "one bad message
        # never crashes the bot" posture this file already applies to
        # audit logging.
        try:
            # When an embed is present, it carries the full reply --
            # .text is still always populated on OutgoingMessage (see
            # its own docstring) but is not sent as separate plain
            # content alongside the embed, to avoid a duplicated
            # message body.
            extra = {"view": reply_view} if reply_view is not None else {}
            if reply_embed is not None:
                sent = await message.channel.send(embed=reply_embed, **extra)
            else:
                sent = await message.channel.send(reply_text, **extra)
        except Exception:
            logger.exception("Failed to send a reply to user_id=%s -- persisted state is unaffected.", message.author.id)
            return

        self._log_message_best_effort(self.clock.now(), MessageRole.ASSISTANT, reply_text, message.channel.id, sent.id)

    async def _on_message_via_gateway(self, message: discord.Message) -> None:
        """Multi-wearer path (Phase E3): the gateway picks the tenant; the exchange is stored only in THAT tenant's database
        (never for strangers or invitees)."""
        now = self.clock.now()
        incoming = IncomingMessage(channel="discord", external_user_id=str(message.author.id), text=message.content,
                                   received_at=now, external_message_id=str(message.id))
        try:
            reply = await self.gateway.handle(incoming)
        except Exception:
            logger.exception("The tenant gateway failed for a DM.")
            reply = None
        text = reply.text if reply is not None else "Something went wrong handling that. It's been logged."
        embed = _build_discord_embed(reply.embed) if reply is not None and reply.embed is not None else None
        log_db = reply.log_db if reply is not None else None
        if log_db is not None:
            self._log_message_best_effort(now, MessageRole.USER, message.content, message.channel.id, message.id, db=log_db)
        try:
            sent = await (message.channel.send(embed=embed) if embed is not None else message.channel.send(text))
        except Exception:
            logger.exception("Failed to send a reply through the tenant gateway.")
            return
        if log_db is not None:
            self._log_message_best_effort(self.clock.now(), MessageRole.ASSISTANT, text, message.channel.id, sent.id, db=log_db)

    def _log_message_best_effort(self, created_at, role: MessageRole, content: str, channel_id, message_id, *, db=None) -> None:
        try:
            (db or self.db).save_conversation_message(
                ConversationMessage(
                    created_at=created_at, role=role, content=content,
                    discord_channel_id=str(channel_id), discord_message_id=str(message_id),
                )
            )
        except Exception:
            logger.exception("Failed to log a conversation message (role=%s) -- continuing anyway.", role.value)


def build_bot(config: Config, db: Database, clock: Clock, application_service: ApplicationService) -> CoachKeyholderBot:
    intents = discord.Intents.default()
    intents.message_content = True  # required to read message content (privileged intent)
    return CoachKeyholderBot(config, db, clock, application_service, intents=intents)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        config = Config.load()
    except ConfigError as e:
        logger.error(str(e))
        raise SystemExit(1) from e

    logging.getLogger("ai_coach_keyholder").setLevel(config.log_level)

    clock = SystemClock()
    core = CoreDatabase(config.db_path)
    db = Database(config.db_path, backup_retention=config.backup_retention_count, core=core)

    # Migrations (they take care of their own backup before applying, if the DB already exists)
    applied = db.migrate(now=clock.now())
    if applied:
        logger.info("Applied migrations: %s", applied)

    # At most 1 automatic backup per day, even outside migrations
    daily_backup = db.ensure_daily_backup(now=clock.now())
    if daily_backup:
        logger.info("Created daily backup: %s", daily_backup)

    # system_state_machine.md Section 7: on_system_startup() must run
    # BEFORE the Discord bot starts / before the first request is
    # accepted -- this was never actually wired into main() until now
    # (Phase 0-2.x had no adapter calling it at all). process_id is a
    # fresh uuid per process start, sufficient for the startup lease's
    # purpose (distinguishing THIS run from a concurrently-running one).
    # Composition root: Conversation Engine Slice 2's own dependency
    # graph, built here and only here -- ApplicationService never
    # constructs OllamaConversationModel/the buffer/the queue itself
    # (dependency injection, per explicit review decision). Uses the
    # existing OLLAMA_HOST/OLLAMA_MODEL config fields; no new .env/Config
    # fields are introduced for timeouts in this slice (the adapter's
    # own DEFAULT_CONNECT_TIMEOUT_SECONDS/DEFAULT_READ_TIMEOUT_SECONDS
    # bootstrap defaults are used as-is).
    ollama_model = OllamaConversationModel(host=config.ollama_host, model_name=config.ollama_model,
                                           read_timeout_seconds=config.ollama_read_timeout_seconds, keep_alive=config.ollama_keep_alive,
                                           num_ctx=config.ollama_num_ctx)
    working_memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000)
    conversation_queue = SubjectConversationQueue()

    # Slice D: LockState/TaskRuntime/TaskCatalog are constructed ONCE
    # here, sharing this same `core` -- the identical read-only
    # instances go to both the new context providers below AND to
    # ApplicationService (as explicit DI, not self-construction) --
    # no duplicate state owners, no separate DB connection for the
    # provider graph. Only read-only types cross into the provider
    # graph -- never LockStateAdministration/TaskRuntimeAdministration/
    # TaskCatalogAdministration.
    lock_state = LockState(config.db_path, core=core)
    task_runtime = TaskRuntime(config.db_path, core=core)
    task_catalog = TaskCatalog(config.db_path, core=core)
    context_providers = (
        LockStateContextProvider(lock_state=lock_state),
        ActiveTaskContextProvider(task_runtime=task_runtime, task_catalog=task_catalog),
    )

    # CHASTER-01A: TokenEncryptor/OAuth client are genuinely optional --
    # constructed only if fully configured; a missing/invalid key or
    # incomplete client config disables Chaster (chaster connect
    # replies "not configured") without preventing the bot itself from
    # starting. Never generates a replacement key.
    chaster_connections = None
    chaster_oauth_client = None
    if config.chaster_token_encryption_key:
        try:
            token_encryptor = TokenEncryptor(config.chaster_token_encryption_key)
            chaster_connections = ChasterConnectionRepository(config.db_path, core=core, encryptor=token_encryptor)
        except TokenEncryptorConfigurationError as e:
            logger.error("Chaster token encryption is misconfigured -- Chaster integration disabled: %s", e)
    if config.chaster_client_id and config.chaster_client_secret and config.chaster_redirect_uri:
        chaster_oauth_client = ChasterOAuthClient(
            client_id=config.chaster_client_id, client_secret=config.chaster_client_secret,
            redirect_uri=config.chaster_redirect_uri, scopes=tuple(config.chaster_oauth_scopes.split()) or ("locks",),
        )

    # ONE ChasterTokenManager (and one ChasterLockClient) shared by
    # `chaster status` and the LLM tool, so refreshes are serialised by
    # the same per-user lock. The read-only Chaster tool is attached
    # only when explicitly enabled AND Chaster is fully configured.
    chaster_token_manager = None
    chaster_lock_client = ChasterLockClient()
    tool_executor = None
    if chaster_connections is not None and chaster_oauth_client is not None:
        chaster_token_manager = ChasterTokenManager(connections=chaster_connections, oauth_client=chaster_oauth_client)
        if config.chaster_llm_tools_enabled:
            tool_executor = ChasterToolExecutor(token_manager=chaster_token_manager, lock_client=chaster_lock_client)
            logger.info("Chaster LLM tool enabled (read-only get_chaster_status).")
    if config.chaster_llm_tools_enabled and tool_executor is None:
        # Loud, because the failure mode is otherwise silent: every
        # message goes to the plain model path and no embed is built.
        logger.warning(
            "CHASTER_LLM_TOOLS_ENABLED is true but the Chaster tool is NOT active: Chaster is not fully "
            "configured (needs CHASTER_TOKEN_ENCRYPTION_KEY, CHASTER_CLIENT_ID, CHASTER_CLIENT_SECRET "
            "and CHASTER_REDIRECT_URI). Lock questions will go to the plain model path.",
        )
    elif not config.chaster_llm_tools_enabled:
        logger.info("Chaster LLM tool disabled (set CHASTER_LLM_TOOLS_ENABLED=true to enable).")

    # Phase 2/3 composition (system/composition.py). Built after the Chaster
    # objects exist because the lock snapshot and the executor need them. All of
    # it is OFF unless explicitly enabled; startup reconciliation still runs
    # BEFORE the bot accepts any request (bot.run is below).
    platform = None
    if config.enable_multi_wearer:
        # Phase E1: the platform layer (registry, router, global halt). The single keyholder Chaster connection stays in tenant zero's database.
        platform = open_platform_services(
            platform_db_path=config.platform_db_path or (config.db_path.parent / "platform.sqlite"), main_db_path=config.db_path,
            main_core=core, owner_user_id=config.owner_user_id, clock=clock, cap=config.tenant_cap)
        platform.keyholder_token_manager = chaster_token_manager
        for note in platform.notes:
            logger.info(note)
    # Phase E5: one shared request budget for the keyholder (bot) account. Background traffic (observation, searches, chat) waits for room;
    # an already-decided lock action (essential lane) is never refused, only counted.
    background_tokens = essential_tokens = None
    if platform is not None and chaster_token_manager is not None:
        api_budget = ApiBudget(requests_per_minute=config.chaster_api_budget_per_minute)
        background_tokens = BudgetedTokenManager(chaster_token_manager, api_budget,
                                                 account_user_id=config.chaster_execution_keyholder_user_id, lane=Lane.BACKGROUND)
        essential_tokens = BudgetedTokenManager(chaster_token_manager, api_budget,
                                                account_user_id=config.chaster_execution_keyholder_user_id, lane=Lane.ESSENTIAL)
        logger.info("Shared Chaster request budget: %d per minute for the keyholder account.", api_budget.limit)
    chat_tokens = background_tokens or chaster_token_manager
    check_in_sender = DiscordCheckInSender(config.db_path, core=core)
    notification_queue = NotificationQueue(config.db_path, core=core)

    def check_in_complete(system_prompt: str, user_message: str) -> str:
        # The language model is used ONLY as a rubric classifier here (supervision/checkin_evaluator.py).
        return ollama_model.generate(request=ModelGenerationRequest(
            messages=(ModelMessage(role=ModelMessageRole.SYSTEM, content=system_prompt),
                      ModelMessage(role=ModelMessageRole.USER, content=user_message)),
            max_output_characters=200,
        ))

    wiring = build_rules_and_execution(
        config, core, connections=chaster_connections, oauth_client=chaster_oauth_client,
        lock_client=chaster_lock_client, token_manager=chaster_token_manager,
        check_in_sender=check_in_sender, check_in_complete=check_in_complete,
        keyholder_token_manager=essential_tokens,
        halt_source=platform.halt if platform is not None else None, notifications=notification_queue,
    )
    if wiring.release_engine is not None:
        # The only release datum the model ever sees: a coarse enum (never a time).
        context_providers = context_providers + (ReleasePhaseContextProvider(get_phase=wiring.release_phase),)
    for note in wiring.notes:
        (logger.warning if "NOT active" in note else logger.info)(note)

    try:
        on_system_startup(core, str(uuid.uuid4()), clock, wiring.rules_engine, wiring.supervision,
                          release_task=wiring.release_task)
    except StartupLeaseNotAcquired:
        logger.error("Another instance is already performing startup reconciliation. Exiting.")
        raise SystemExit(1)

    context_providers = context_providers + (IronContractContextProvider(is_locked=wiring.iron_contract.locked_for_context),)
    coaching, conditioning, memory_provider, memory_sink = coaching_memory_parts(config, config.db_path, core, coach=config.enable_coach)
    if memory_provider is not None:
        context_providers = context_providers + (memory_provider,)
    # Phase J3: goal and commitment proposals. Off unless ENABLE_COACH; the single wearer only, tenants are not wired (system/coach_dm.py).
    from conversation_engine.context_providers.coach_provider import CoachContextProvider
    from system.coach_dm import active_task_reader, build_coach, goal_targets_frozen
    from system.composition import window_config_from
    coach_dm = build_coach(config, core, clock=clock, targets_frozen=goal_targets_frozen(wiring.iron_contract), paused=wiring.vacation.is_paused,
                           today=lambda now, cfg=window_config_from(config): cfg.local(now).date(),
                           active_task=active_task_reader(task_runtime, task_catalog), memory=coaching) if config.enable_coach else None
    if coach_dm is not None:
        context_providers = context_providers + (CoachContextProvider(read=coach_dm.context),)
        if wiring.check_ins is not None:
            wiring.check_ins.prompt_suffix = coach_dm.prompt_suffix
        warning = coach_dm.check_ready(ollama_model)
        logger.warning(warning) if warning else logger.info("The coach is active.")
    conversation_engine = ConversationEngine(
        model=ollama_model, working_memory_reader=working_memory, working_memory_writer=working_memory,
        structured_output=config.conversation_structured_output, structured_sink=memory_sink,
        runtime_sink=wiring.telemetry_buffer.record,
        queue=conversation_queue, providers=context_providers, tool_executor=tool_executor, coach=coach_dm,
    )

    # Chaster chat (Phase C): off unless ENABLE_CHASTER_MESSAGING; every user must also opt in with `channel chaster`.
    message_routes = MessageRouteRepository(config.db_path, core=core)
    chat_client = ChasterConversationClient()
    chat_ready = bool(config.enable_chaster_messaging and chaster_connections is not None and chaster_token_manager is not None
                      and config.chaster_execution_keyholder_user_id)
    if config.enable_chaster_messaging and not chat_ready:
        logger.warning("ENABLE_CHASTER_MESSAGING is true but Chaster chat is NOT active: it needs Chaster fully configured and "
                       "CHASTER_EXECUTION_KEYHOLDER_USER_ID (the bot account).")
    channel_service = ChannelPreferenceService(
        routes=message_routes, connections=chaster_connections, token_manager=chat_tokens, client=chat_client,
        bot_user_id=config.chaster_execution_keyholder_user_id, chaster_messaging_enabled=chat_ready)

    # Phase J1 slice 2: the owner's community review consent overview + buttons (the only bot-side door to the extension service).
    from system.review_consent_dm import build_review_consent_dm, render_overview
    from peer_review.wiring import owner_discord_id
    review_consent_dm = build_review_consent_dm(config, core, token_manager=chaster_token_manager, lock_client=chaster_lock_client,
                                                connections=chaster_connections,
                                                owner_discord_id=owner_discord_id(core, config.owner_user_id), clock=clock)
    exemption_dm = exemption_reply = None
    resolve_user = discord_user_resolver(core)
    if config.enable_exemptions and config.owner_user_id:
        wearer = config.check_in_user_id or config.chaster_execution_wearer_user_id or config.owner_user_id
        exemption_opening = build_exemption_opening(config, core, token_manager=essential_tokens or chaster_token_manager, lock_client=chaster_lock_client,
                                                    halt_source=platform.halt if platform is not None else None, clock=clock)
        logger.info("Exemptions enabled: %s.", f"an approval opens lock {exemption_opening.lock_id}" if exemption_opening is not None else "no lock configured, an approval opens nothing")
        exemption_dm = ExemptionDm(ExemptionService(config.db_path, core=core, owner_user_id=config.owner_user_id, penalties=make_penalty_engine(core), opening=exemption_opening),
                                   resolve_user=resolve_user, now=clock.now)

        def exemption_reply(user_id, text, now):
            return exemption_dm.service.start(user_id, now) if user_id == wearer and is_exemption_request(text) else None
    application_service = ApplicationService(
        config.db_path, core=core, conversation_engine=conversation_engine,
        lock_state=lock_state, task_runtime=task_runtime, task_catalog=task_catalog,
        chaster_connections=chaster_connections, chaster_oauth_client=chaster_oauth_client,
        chaster_token_manager=chaster_token_manager,
        check_in_listener=(wiring.check_ins.on_user_message if wiring.check_ins is not None else None),
        channel_service=channel_service, coaching_memory=coaching, conditioning=conditioning,
        telemetry=wiring.telemetry_store, research=wiring.research_preferences, iron_contract=wiring.iron_contract, vacation=wiring.vacation,
        owner_user_id=config.owner_user_id,
        review_consent_reply=(lambda: render_overview(review_consent_dm.overview())) if review_consent_dm is not None else None,
        coach=coach_dm, exemption_reply=exemption_reply, clock=clock,
    )
    bot = build_bot(config, db, clock, application_service)
    bot.dm_actions["lock_report"] = application_service
    if exemption_dm is not None:
        bot.dm_actions["exemption"] = exemption_dm
        bot.exemptions = exemption_dm.service
    bot.review_consent_dm = review_consent_dm
    bot.coach_dm = coach_dm
    check_in_sender.attach(bot)
    if platform is not None:
        # Phase E2: every ACTIVE wearer runs as an isolated tenant (own database, own staggered loop); tenant zero keeps the loop above.
        def tenant_check_in_sender(path, tenant_core):
            sender = DiscordCheckInSender(path, core=tenant_core)
            sender.attach(bot)
            return sender

        shared = SharedServices(
            config=config, clock=clock, model=ollama_model, oauth_client=chaster_oauth_client, lock_client=chaster_lock_client,
            keyholder_connections=chaster_connections, keyholder_token_manager=essential_tokens or chaster_token_manager,
            platform_halt=platform.halt, keyholder_background_tokens=background_tokens,
            chat_enabled=config.enable_chaster_messaging, conversation_client=chat_client,
            check_in_sender_factory=tenant_check_in_sender, check_in_complete=check_in_complete)
        bot.tenant_supervisor = TenantLoopSupervisor(
            registry=platform.registry, factory=TenantFactory(registry=platform.registry, shared=shared, tenant_dir=platform.tenant_dir),
            interval_seconds=config.chaster_execution_interval_seconds,
            archiver=TenantArchiver(registry=platform.registry, tenant_dir=platform.tenant_dir, clock=clock))
    # Messaging layer (Phase B): the Discord adapter shares the bot's client; the router picks the channel from the stored
    # preference (Discord unless a user has a route). Chaster delivery arrives with phase C.
    bot.platform = platform
    discord_adapter = DiscordMessagingAdapter()
    discord_adapter.attach(bot)
    adapters = [discord_adapter]
    if chat_ready:
        async def chaster_handler(unified):
            # Untrusted channel: conversation-only entry, never commands (ApplicationService.handle_conversation_only).
            out = await asyncio.to_thread(application_service.handle_conversation_only, unified.user_id, unified.content, clock.now())
            return None if out is None else out.text

        chaster_adapter = ChasterMessagingAdapter(
            token_manager=chat_tokens, client=chat_client, connections=chaster_connections,
            bot_user_id=config.chaster_execution_keyholder_user_id, clock=clock, handler=chaster_handler)
        adapters.append(chaster_adapter)
        bot.chaster_chat = (ChasterConversationPoller(
            config.db_path, core=core, routes=message_routes, connections=chaster_connections,
            token_manager=chat_tokens, client=chat_client, adapter=chaster_adapter,
            bot_user_id=config.chaster_execution_keyholder_user_id, clock=clock), chaster_adapter,
            config.chaster_chat_poll_seconds)
        logger.info("Chaster chat channel enabled (opt-in per user; inbound by polling).")
    # A user whose preferred channel is unavailable still gets the message on Discord.
    bot.messaging_router = MessageRouter(routes=message_routes, adapters=adapters, fallback_to_discord=True)
    if platform is not None and bot.tenant_supervisor is not None:
        # Phase E3: every DM goes through the tenant gateway (routing by author, invitations, wearer safety commands).
        bot.gateway = TenantGateway(
            registry=platform.registry, supervisor=bot.tenant_supervisor,
            admin=TenantAdmin(registry=platform.registry, halt=platform.halt, owner_user_id=config.owner_user_id, clock=clock),
            owner_user_id=config.owner_user_id, tenant_zero_handler=application_service.handle_message, tenant_zero_db=db,
            tenant_dir=platform.tenant_dir, clock=clock,
            pairing=LockPairingService(
                registry=platform.registry, lock_client=chaster_lock_client, keyholder_tokens=chaster_token_manager,
                keyholder_connections=chaster_connections, keyholder_user_id=config.chaster_execution_keyholder_user_id, clock=clock,
                wearer_account_of=wearer_account_lookup(platform.tenant_dir)))
        bot.invitations = InvitationNotifier(registry=platform.registry, send=discord_adapter.send_message, clock=clock)
    def research_exporters():
        found = [wiring.research_exporter] if wiring.research_exporter is not None else []
        if bot.tenant_supervisor is not None:
            found += [r.wiring.research_exporter for r in list(bot.tenant_supervisor.runtimes.values()) if r.wiring.research_exporter is not None]
        return found

    bot.research_export = research_export_loop_for(config, research_exporters, clock)
    # Phase I4: community review results -> UNCONFIRMED Incidents for the keyholder's review (its own loop, read-only towards Chaster).
    from peer_review.wiring import build_peer_review_watch, peer_review_loop
    peer = build_peer_review_watch(config, core, token_manager=background_tokens or chaster_token_manager, lock_client=chaster_lock_client,
                                   connections=chaster_connections)
    for note in peer.notes:
        (logger.warning if "NOT active" in note else logger.info)(note)
    if peer.watch is not None:
        from peer_review.discord_buttons import PeerReviewButtonHandler, decision_actions
        from peer_review.wiring import build_peer_review_decisions, queued_send
        bot.peer_review_buttons = PeerReviewButtonHandler(build_peer_review_decisions(config, core), owner_user_id=config.owner_user_id,
                                                          owner_discord_id=peer.owner_target, now=clock.now)
        bot.peer_reviews = peer_review_loop(peer.watch, clock, interval_seconds=peer.interval_seconds,
                                            send=queued_send(notification_queue, user_id=config.owner_user_id, clock=clock),
                                            owner_target=peer.owner_target, view_for=decision_actions, embed_for=lambda spec: spec)
    if bot.research_export is not None:
        logger.info("Research telemetry link configured (records only with ENABLE_RESEARCH_TELEMETRY and each wearer's `research on`).")
    # The two detectors: read-only towards Chaster, UNCONFIRMED records only, and the keyholder's button is the only way further.
    detectors = build_detectors(config, core, token_manager=background_tokens or chaster_token_manager, lock_client=chaster_lock_client,
                                connections=chaster_connections, vacation=wiring.vacation)
    for note in detectors.notes:
        (logger.warning if "NOT active" in note else logger.info)(note)
    if detectors.signals is not None:
        bot.dm_actions["infraction"] = InfractionDm(detectors.signals, resolve_user=resolve_user, now=clock.now)
    if detectors.hygiene is not None:
        bot.dm_actions["hygiene"] = HygieneDm(detectors.hygiene, resolve_user=resolve_user, now=clock.now)
    if detectors.active:
        bot.detectors = detector_loop(detectors, clock)
    if exemption_dm is not None and detectors.signals is not None:
        domain = config.hygiene_trust_domain or "hygiene"
        if TrustManager(config.db_path, core=core).get_domain_state(domain) is not None:
            exemption_dm.service.set_overrun_signal(overrun_signal(detectors.signals, trust_domain=domain))
    # The bot keeps the owner's required lock settings true on Chaster by itself, never past a halt.
    extension_guard = build_extension_guard(config, core, notifications=notification_queue, token_manager=essential_tokens or chaster_token_manager, lock_client=chaster_lock_client,
                                            connections=chaster_connections, halt_source=platform.halt if platform is not None else None, clock=clock)
    if extension_guard is not None:
        bot.extension_guard = extension_guard_loop(extension_guard, clock, interval_seconds=config.extension_guard_poll_seconds)
        logger.info("Extension guard enabled: every %d s, subject to the runtime halt.", config.extension_guard_poll_seconds)
    # One outbound queue for every proactive notice: retries, quiet hours and the channel preference live here.
    notice_user = config.check_in_user_id or config.chaster_execution_wearer_user_id or config.owner_user_id
    bot.penalty_notices = penalty_notice_handlers(notice_user) if notice_user else None
    try:
        quiet_windows = quiet_config_from(config)
    except ValueError:
        quiet_windows = None

    def is_quiet(now):
        return quiet_windows is not None and classify(quiet_windows, now) is WindowKind.QUIET

    bot.notifications = notification_loop(
        NotificationDispatcher(notification_queue, is_quiet=is_quiet, deliver=NotificationDelivery(
            discord=discord_adapter, discord_id_for=message_routes.discord_id_for, router=bot.messaging_router, embed_for=_build_discord_embed, view_for=actions_view)),
        clock)
    # Phase J2: the watch can produce confirmed Incidents, so the outbox must be drained continuously, not only at start.
    bot.penalty_barrier = wiring.penalty_barrier
    if any(x is not None for x in (wiring.rules_engine, wiring.supervision, wiring.release_task, wiring.check_ins, wiring.daily, peer.watch,
                                   bot.exemptions, detectors.signals)):
        bot.periodic = (core, wiring.rules_engine, wiring.executor, config.chaster_execution_interval_seconds,
                        wiring.supervision, wiring.release_task, wiring.check_ins, wiring.daily, wiring.telemetry)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
