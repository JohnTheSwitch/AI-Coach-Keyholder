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
from system.composition import build_rules_and_execution
from chaster.conversation_client import ChasterConversationClient
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
        self.messaging_router = None            # set by main(): MessageRouter (Phase B)
        self.chaster_chat: tuple | None = None   # (poller, adapter, interval_seconds) when Chaster chat is active
        self._periodic_task: asyncio.Task | None = None
        self._chat_task: asyncio.Task | None = None

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
            core, rules_engine, executor, interval, supervision, release_task, check_ins, daily = self.periodic
            self._periodic_task = asyncio.create_task(
                periodic_loop(core, str(uuid.uuid4()), self.clock, rules_engine=rules_engine, executor=executor,
                              interval_seconds=interval, supervision=supervision,
                              release_task=release_task, check_ins=check_ins, daily=daily)
            )
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
        except Exception:
            logger.exception("Unhandled error processing a DM from user_id=%s", message.author.id)
            reply_text = "Something went wrong handling that. It's been logged."
            reply_embed = None

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
            if reply_embed is not None:
                sent = await message.channel.send(embed=reply_embed)
            else:
                sent = await message.channel.send(reply_text)
        except Exception:
            logger.exception("Failed to send a reply to user_id=%s -- persisted state is unaffected.", message.author.id)
            return

        self._log_message_best_effort(self.clock.now(), MessageRole.ASSISTANT, reply_text, message.channel.id, sent.id)

    def _log_message_best_effort(self, created_at, role: MessageRole, content: str, channel_id, message_id) -> None:
        try:
            self.db.save_conversation_message(
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
    ollama_model = OllamaConversationModel(host=config.ollama_host, model_name=config.ollama_model)
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
    check_in_sender = DiscordCheckInSender(config.db_path, core=core)

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

    conversation_engine = ConversationEngine(
        model=ollama_model, working_memory_reader=working_memory, working_memory_writer=working_memory,
        queue=conversation_queue, providers=context_providers, tool_executor=tool_executor,
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
        routes=message_routes, connections=chaster_connections, token_manager=chaster_token_manager, client=chat_client,
        bot_user_id=config.chaster_execution_keyholder_user_id, chaster_messaging_enabled=chat_ready)

    application_service = ApplicationService(
        config.db_path, core=core, conversation_engine=conversation_engine,
        lock_state=lock_state, task_runtime=task_runtime, task_catalog=task_catalog,
        chaster_connections=chaster_connections, chaster_oauth_client=chaster_oauth_client,
        chaster_token_manager=chaster_token_manager,
        check_in_listener=(wiring.check_ins.on_user_message if wiring.check_ins is not None else None),
        channel_service=channel_service,
    )
    bot = build_bot(config, db, clock, application_service)
    check_in_sender.attach(bot)
    # Messaging layer (Phase B): the Discord adapter shares the bot's client; the router picks the channel from the stored
    # preference (Discord unless a user has a route). Chaster delivery arrives with phase C.
    discord_adapter = DiscordMessagingAdapter()
    discord_adapter.attach(bot)
    adapters = [discord_adapter]
    if chat_ready:
        async def chaster_handler(unified):
            # Untrusted channel: conversation-only entry, never commands (ApplicationService.handle_conversation_only).
            out = await asyncio.to_thread(application_service.handle_conversation_only, unified.user_id, unified.content, clock.now())
            return None if out is None else out.text

        chaster_adapter = ChasterMessagingAdapter(
            token_manager=chaster_token_manager, client=chat_client, connections=chaster_connections,
            bot_user_id=config.chaster_execution_keyholder_user_id, clock=clock, handler=chaster_handler)
        adapters.append(chaster_adapter)
        bot.chaster_chat = (ChasterConversationPoller(
            config.db_path, core=core, routes=message_routes, connections=chaster_connections,
            token_manager=chaster_token_manager, client=chat_client, adapter=chaster_adapter,
            bot_user_id=config.chaster_execution_keyholder_user_id, clock=clock), chaster_adapter,
            config.chaster_chat_poll_seconds)
        logger.info("Chaster chat channel enabled (opt-in per user; inbound by polling).")
    # A user whose preferred channel is unavailable still gets the message on Discord.
    bot.messaging_router = MessageRouter(routes=message_routes, adapters=adapters, fallback_to_discord=True)
    if any(x is not None for x in (wiring.rules_engine, wiring.supervision, wiring.release_task, wiring.check_ins, wiring.daily)):
        bot.periodic = (core, wiring.rules_engine, wiring.executor, config.chaster_execution_interval_seconds,
                        wiring.supervision, wiring.release_task, wiring.check_ins, wiring.daily)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
