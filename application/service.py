"""
application/service.py

ApplicationService.handle_message() is THE boundary between any
adapter (bot/discord_bot.py today) and the rest of this system.
Channel-agnostic in, channel-agnostic out (IncomingMessage ->
OutgoingMessage) -- an adapter never needs to know which domain modules
exist, and this layer never needs to know which channel it's being
called from.

Only calls domain modules through their PUBLIC read APIs
(get_active_or_frozen_penalty_window(), etc.) -- never a
`_*_in_transaction` method, the same boundary every consumer handler in
`system/startup.py` respects for a different reason (transactional
self-containment there; here, simply because those methods are not
this layer's to call).
"""

from __future__ import annotations

from uuid import uuid4

import functools
import logging
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from ai.identity_catalog import get_identity
from advanced_mode.models import (
    ActiveModeTransitionExistsError,
    MinimumTimeInAdvancedNotMetError,
    ModeTransitionInterruptedByPenaltyWindowError,
    ModeTransitionNotConfirmableError,
    ModeTransitionSourceModeMismatchError,
    ModeTransitionStatus,
    NoActiveModeTransitionError,
    OperatingMode,
)
from advanced_mode.repository import AdvancedMode, AdvancedModeAdministration
from application.models import ActionSpec, EmbedField, EmbedSpec, IncomingMessage, OutgoingMessage
from application.onboarding_service import OnboardingService
from application.router import CommandRouter, RequestContext
from conversation_engine.engine import ConversationEngine
from conversation_engine.fallback import FallbackReason, render_fallback
from conversation_engine.models import ResponseCategory, UnknownIdentityError
from application.progress import is_progress_query, read_progress, render_progress
from application.natural_queries import CAPABILITIES_TEXT, read_only_command
from application.review_consent_query import is_review_consent_query
from application.terms_notice import TERMS_NOTICE
from application.user_service import UserService
from chaster.models import ConnectionStatus
from chaster.repository import ChasterConnectionRepository, ChasterOAuthStateRepository
from chaster.oauth_client import ChasterOAuthClient
from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.llm_tools import (
    ChasterToolOutcome, ChasterToolOutcomeKind, is_chaster_status_query, mentions_lock_topic,
)
from chaster.status_context import build_chaster_chat_context
from chaster.presentation import lock_status_text, lock_time_remaining, lock_title
from chaster.token_manager import (
    ChasterNotConnectedError,
    ChasterReauthorizationRequiredError,
    ChasterRefreshUnavailableError,
    ChasterTokenManager,
)
from goal_management.repository import GoalManager
from infrastructure.database import Database as CoreDatabase
from iron_contract.vacation import MAX_LEAD_DAYS as VACATION_MAX_LEAD_DAYS
from iron_contract.vacation import MAX_VACATION_DAYS as VACATION_MAX_DAYS
from lock_state.models import LockKnowledgeState, LockReportStatus
from lock_state.repository import LockState, LockStateAdministration
from penalty_engine.repository import PenaltyEngine
from penalty_engine.window import remaining_active_hours, target_active_hours
from recovery_plan.repository import RecoveryPlanManager
from task_catalog.models import TaskInstanceRole
from task_catalog.repository import TaskCatalog
from task_runtime.repository import (
    TaskAssignmentConcurrencyError,
    TaskAssignmentNotFoundError,
    TaskAssignmentReferentialIntegrityError,
    TaskAssignmentTransitionError,
    TaskNotEligibleError,
    TaskRuntime,
    TaskRuntimeAdministration,
    TaskTemplateNotFoundForAssignmentError,
)
from task_runtime.selection import select_eligible_template
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.application")

# Phase G6: `forget all` is a two-step command; the second step is `yes` within this window.
FORGET_CONFIRM_WINDOW = timedelta(minutes=5)
FORGET_ALL_WARNING = ("This will permanently delete your psychological profile and revoke sharing consent: your Coaching Memory, the coach's proposals and commitments and all "
                      "behavioral telemetry about you are erased, and research sharing is switched off with an erase request to the research server. "
                      "Reply `yes` within 5 minutes to confirm; anything else cancels.")


def requires_unlocked(*, always: bool = False):
    """Phase G6, the Iron Contract: the handler runs only while the user is known to be unlocked (iron_contract.guard.IronContract.refusal).
    `always=True` applies even where the contract was not disclosed (vacations need "unlocked" by their own rules). Never put this on a consent,
    safety or erasure command (philosophy 2.5, 2.3)."""
    def wrap(handler):
        @functools.wraps(handler)
        def inner(self, ctx):
            refusal = self._iron_refusal(ctx, always=always)
            return refusal if refusal is not None else handler(self, ctx)
        return inner
    return wrap


class ApplicationService:
    def __init__(
        self, db_path: str | Path, *, core: CoreDatabase | None = None,
        conversation_engine: ConversationEngine | None = None,
        lock_state: LockState | None = None, task_runtime: TaskRuntime | None = None,
        task_catalog: TaskCatalog | None = None,
        chaster_connections: ChasterConnectionRepository | None = None,
        chaster_oauth_client: ChasterOAuthClient | None = None,
        chaster_token_manager: ChasterTokenManager | None = None,
        check_in_listener=None,
        channel_service=None,
        coaching_memory=None, conditioning=None, telemetry=None, research=None, iron_contract=None, vacation=None,
        owner_user_id: str | None = None, review_consent_reply=None, coach=None, exemption_reply=None, clock=None,
    ) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._conversation_engine = conversation_engine  # DI only -- never constructed here
        # Optional callable(user_id, text, now): lets the supervision check-in service see a free-text
        # reply. Called only for messages that are neither commands nor Chaster status questions.
        self._check_in_listener = check_in_listener
        # Optional messaging.channel_service.ChannelPreferenceService: the user's choice between Discord and Chaster chat.
        self._channel_service = channel_service
        # Phase F2: the user's own Coaching Memory (view / erase) and Progressive Conditioning switch.
        self._coaching_memory = coaching_memory
        self._conditioning = conditioning
        # Phase G1: the user's own Behavioral Telemetry (view counts / erase); behavioral_telemetry.store.TelemetryStore by injection.
        self._telemetry = telemetry
        # Phase G4: the user's own research-export switch (behavioral_telemetry.research.ResearchPreferences by injection).
        self._research = research
        # Phase G6: the Iron Contract guard (iron_contract.guard.IronContract) and vacations (iron_contract.vacation.VacationService), by injection.
        self._iron_contract = iron_contract
        self._vacation = vacation
        self._pending_forget: dict[str, datetime] = {}       # user id -> until when `yes` confirms `forget all` (in memory)
        # Phase J1: the owner (the wearer of the connected account's locks) also sees community review and penalty-window progress.
        self._owner_user_id = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None
        # Phase J1 slice 2: callable() -> OutgoingMessage with the community review consent overview and its buttons (owner only;
        # system/review_consent_dm.py, injected by the bot). Decisions are button presses, never this text path.
        self._review_consent_reply = review_consent_reply
        # Phase J3: system.coach_dm.CoachDm by injection: `guidance(...)` for a turn, `present(...)` for its cards. A coach failure never touches a reply.
        self._coach = coach
        # Callable(user_id, text, now) -> OutgoingMessage | None: the exemption entry point (exemptions/service.py, injected by the bot). The word opens the
        # buttons; the buttons commit. Deterministic and BEFORE any model.
        self._exemption_reply = exemption_reply
        self._clock = clock                                  # only a button press needs it: a message brings its own time

        self.user_service = UserService(self.db_path, core=self._core)
        self.onboarding_service = OnboardingService(self.db_path, core=self._core)
        self.trust_manager = TrustManager(self.db_path, core=self._core)
        self.penalty_engine = PenaltyEngine(self.db_path, core=self._core)
        self.recovery_plan = RecoveryPlanManager(self.db_path, core=self._core)
        self.goal_management = GoalManager(self.db_path, core=self._core)
        self.advanced_mode = AdvancedMode(self.db_path, core=self._core)
        self.advanced_mode_admin = AdvancedModeAdministration(self.db_path, core=self._core)
        # lock_state/task_runtime/task_catalog: Slice D's own production
        # composition root (bot/discord_bot.py) constructs these ONCE and
        # passes the SAME read instances here AND into the new
        # ConversationEngine context providers -- no duplicate state
        # owners. The self-construction fallback below exists only for
        # backward compatibility with existing tests that construct
        # ApplicationService(db_path, core=core) without these params;
        # it must never diverge in behavior from the DI path, since both
        # ultimately wrap the same `core`.
        self.lock_state = lock_state if lock_state is not None else LockState(self.db_path, core=self._core)
        self.lock_state_admin = LockStateAdministration(self.db_path, core=self._core)
        self.task_catalog = task_catalog if task_catalog is not None else TaskCatalog(self.db_path, core=self._core)
        self.task_runtime = task_runtime if task_runtime is not None else TaskRuntime(self.db_path, core=self._core)
        self.task_runtime_admin = TaskRuntimeAdministration(self.db_path, core=self._core)

        # CHASTER-01A: chaster_states always constructable (needs no
        # config -- no encryption, no client secret); chaster_connections/
        # chaster_oauth_client are genuinely optional -- DI only, never
        # self-constructed here, since doing so would require a master
        # encryption key / OAuth client credentials this layer has no
        # business sourcing itself (bot/discord_bot.py's own composition
        # root is where Config is read). If either is None, `chaster
        # connect` replies that Chaster integration is not configured,
        # rather than raising.
        self.chaster_states = ChasterOAuthStateRepository(self.db_path, core=self._core)
        self.chaster_connections = chaster_connections
        self.chaster_oauth_client = chaster_oauth_client
        # Self-healing token handling (refresh before expiry, refresh +
        # retry once on HTTP 401). Only constructible when both the
        # connection store and the OAuth client exist.
        if chaster_token_manager is None and chaster_connections is not None and chaster_oauth_client is not None:
            chaster_token_manager = ChasterTokenManager(connections=chaster_connections, oauth_client=chaster_oauth_client)
        self.chaster_token_manager = chaster_token_manager
        # ChasterLockClient needs no credentials/config to construct
        # (just the two confirmed endpoints) -- always available,
        # unlike chaster_oauth_client above, matching lock_state's own
        # self-construction pattern rather than chaster_oauth_client's
        # genuinely-optional one.
        self.chaster_lock_client = ChasterLockClient()

        self.router = CommandRouter()
        self._register_commands()

    def handle_message(self, incoming: IncomingMessage) -> OutgoingMessage:
        """
        The single entry point every adapter calls. Never raises --
        any exception from user resolution, onboarding, or routing is
        caught, logged, and turned into a safe, generic reply. An
        adapter still wraps its OWN call to this method in its own
        try/except too (defense in depth, the same layering this
        project already uses elsewhere -- e.g. `consume_event()`'s
        dedup plus `UNIQUE(completion_id)` as a second, independent
        guard) in case this method's own plumbing (e.g. opening the
        DB) fails before any of this function's own code runs.

        Onboarding (docs/architecture/user_onboarding_technical_design.md)
        takes priority over normal command routing: an incomplete
        user's message is always interpreted as an onboarding answer
        (or shown the current onboarding prompt, for a brand-new user's
        first-ever message), never matched against the command table --
        a new user is never required to already know a command like
        `help` before onboarding has even asked them anything.
        """
        try:
            user = self.user_service.get_or_create_user(
                incoming.channel, incoming.external_user_id, now=incoming.received_at,
            )
            preferences, was_created = self.onboarding_service.get_or_create_preferences(
                user.id, now=incoming.received_at,
            )

            if was_created:
                # A brand-new user's first-ever message is never itself
                # treated as an answer -- they haven't been asked
                # anything yet.
                return self.onboarding_service.prompt_for(preferences)

            if not self.onboarding_service.is_complete(preferences):
                result = self.onboarding_service.process_message(preferences, incoming.text, now=incoming.received_at)
                return result.reply

            context = RequestContext(user=user, now=incoming.received_at, external_message_id=incoming.external_message_id)
            confirmed = self._confirm_pending_forget(incoming.text, context)
            if confirmed is not None:
                return confirmed
            forget = self._forget_one(incoming.text, context)
            if forget is not None:
                return forget
            vacation = self._vacation_schedule(incoming.text, context)
            if vacation is not None:
                return vacation
            result = self.router.route(incoming.text, context)
            if result.matched:
                return result.outgoing

            # Deterministic guardrail, BEFORE any model: a natural-language
            # question about the user's Chaster lock is answered by the
            # same read-only path as `chaster status` (embed, or a clear
            # not-connected / reconnect / unavailable / not-configured
            # embed). It does not depend on CHASTER_LLM_TOOLS_ENABLED, on
            # the model supporting tools, or on the model being up, so a
            # model can never improvise manual commands for it.
            if is_chaster_status_query(incoming.text):
                return self._answer_chaster_status_question(context)

            # Phase J1: "show my progress" / "how am I doing?" -- answered from real records, never by the model, never with Trust.
            if is_progress_query(incoming.text):
                return self._answer_progress(context)

            # Phase J1 slice 2: "who can see my photos?" / "stop community review" -> the consent overview with buttons (owner only).
            if self._review_consent_reply is not None and self._is_owner(context) and is_review_consent_query(incoming.text):
                return self._answer_review_consent()

            if self._exemption_reply is not None:
                exemption = self._exemption_reply(user.id, incoming.text, incoming.received_at)
                if exemption is not None:
                    return exemption

            # Zero-Command UX: a natural question for a read-only command gets that command's own answer, from real records.
            command = read_only_command(incoming.text)
            if command is not None:
                routed = self.router.route(command, context)
                if routed.matched:
                    return routed.outgoing

            check_in_reply = self._notify_check_in(user.id, incoming.text, incoming.received_at)

            if self._conversation_engine is None:
                return OutgoingMessage(text=self.router.unrecognized_text())

            if preferences.identity_id is None:
                return OutgoingMessage(text=render_fallback(FallbackReason.MISSING_REQUIRED_CONTEXT).text)

            chat_context = self._chaster_chat_context(incoming.text, context)
            context_kwargs: dict = (
                {"extra_system_context": chat_context, "remember_exchange": True} if chat_context is not None else {}
            )
            guidance = self._coach_guidance(user.id, incoming.text, incoming.received_at, check_in_reply)
            if guidance:
                context_kwargs["coach_guidance"] = guidance
            try:
                response = self._conversation_engine.generate_response(
                    subject_key=user.id, current_user_message=incoming.text,
                    language="en", identity_id=preferences.identity_id,
                    now=incoming.received_at, **context_kwargs,
                )
            except UnknownIdentityError:
                # A stored identity_id that no longer matches any catalog
                # entry -- same deterministic fallback as identity_id is
                # None, not the generic top-level error path; this is a
                # "missing required context" situation, not a bug crash.
                return OutgoingMessage(text=render_fallback(FallbackReason.MISSING_REQUIRED_CONTEXT).text)
            embed = self._embed_from_tool_outputs(response.tool_outputs, now=incoming.received_at)
            return self._with_coach_cards(OutgoingMessage(text=response.text, embed=embed), user.id, incoming.text, response, check_in_reply, incoming.received_at)
        except Exception:
            logger.exception("ApplicationService.handle_message failed for channel=%r", incoming.channel)
            return OutgoingMessage(text="Something went wrong handling that. It's been logged.")

    def handle_conversation_only(self, user_id: str, text: str, now) -> OutgoingMessage | None:
        """Entry for text that arrives over an UNTRUSTED channel (Chaster chat). It is conversation input and nothing else:
        never matched against commands, never an onboarding answer, never a check-in reply, never a status shortcut. Returns None
        (no reply) for a user who has not finished onboarding or has no identity, and also on any failure. Replies are plain text."""
        try:
            if self._conversation_engine is None or not isinstance(text, str) or not text.strip():
                return None
            preferences, was_created = self.onboarding_service.get_or_create_preferences(user_id, now=now)
            if was_created or not self.onboarding_service.is_complete(preferences) or preferences.identity_id is None:
                return None
            response = self._conversation_engine.generate_response(
                subject_key=user_id, current_user_message=text, language="en",
                identity_id=preferences.identity_id, now=now, allow_side_data=False,   # untrusted channel: never stored as memory
            )
            return OutgoingMessage(text=response.text)
        except UnknownIdentityError:
            return None
        except Exception:
            logger.exception("ApplicationService.handle_conversation_only failed.")
            return None

    def _notify_check_in(self, user_id: str, text: str, now) -> bool:
        """Best effort and never raises: a check-in problem must not break a conversation. True when the message answered a check-in."""
        if self._check_in_listener is None:
            return False
        try:
            return self._check_in_listener(user_id, text, now) is True
        except Exception:
            logger.exception("Check-in listener failed; the message is handled normally.")
            return False

    def _coach_guidance(self, user_id: str, text: str, now, check_in_reply: bool) -> str | None:
        if self._coach is None:
            return None
        try:
            return self._coach.guidance(user_id, text, now, check_in_reply=check_in_reply)
        except Exception:
            logger.exception("The coach guidance failed; this turn is ordinary conversation.")
            return None

    def _with_coach_cards(self, outgoing: OutgoingMessage, user_id: str, user_message: str, response, check_in_reply: bool, now) -> OutgoingMessage:
        if self._coach is None:
            return outgoing
        try:
            return self._coach.present(outgoing, user_id=user_id, outcome=response.proposal_outcome, check_in_reply=check_in_reply, now=now,
                                       user_message=user_message)
        except Exception:
            logger.exception("The coach cards failed; the reply goes out without them.")
            return outgoing

    # -------------------------------------------------------------------
    # Explicit, limited command set (per this phase's own scope --
    # not a natural-language router, not full Coach/Keyholder reasoning)
    # -------------------------------------------------------------------

    def _register_commands(self) -> None:
        self.router.register("help", "Show this list of commands", self._handle_help)
        self.router.register("status", "Show the current penalty window, if any", self._handle_status)
        self.router.register("preferences", "Show your saved language/AI voice/personality choices", self._handle_preferences)
        self.router.register("mode", "Show your current operating mode and any pending transition", self._handle_mode_status)
        self.router.register("mode status", "Show your current operating mode and any pending transition", self._handle_mode_status)
        self.router.register(
            "mode request advanced",
            "Request switching to Advanced Mode (needs a full 24h uninterrupted wait, then a separate second confirmation)",
            self._handle_mode_request_advanced,
        )
        self.router.register(
            "mode request standard",
            "Request switching back to Standard Mode (needs 30+ days in Advanced first, then a 24h wait and a separate second confirmation)",
            self._handle_mode_request_standard,
        )
        self.router.register("mode cancel", "Cancel your pending mode transition request", self._handle_mode_cancel)
        self.router.register(
            "mode confirm",
            "Give final confirmation for a pending mode transition, once eligible -- must be a separate message from the original request",
            self._handle_mode_confirm,
        )
        self.router.register_family("mode", invalid_handler=self._handle_mode_invalid)

        self.router.register("lock status", "Show your current reported lock status", self._handle_lock_status)
        self.router.register("lock report locked", "Report yourself as locked", self._handle_lock_report_locked)
        self.router.register("lock report unlocked", "Report yourself as unlocked", self._handle_lock_report_unlocked)
        self.router.register_family("lock", invalid_handler=self._handle_lock_invalid)

        self.router.register("task request", "Request a new eligible task, if you don't already have an active one", self._handle_task_request)
        self.router.register("task active", "Show your currently active task, if any", self._handle_task_active)
        self.router.register("task complete", "Mark your active task as completed", self._handle_task_complete)
        self.router.register("task cancel", "Cancel your active task", self._handle_task_cancel)
        self.router.register_family("task", invalid_handler=self._handle_task_invalid)

        self.router.register("chaster connect", "Connect your Chaster account", self._handle_chaster_connect)
        self.router.register("chaster status", "Check your Chaster lock status", self._handle_chaster_status)
        self.router.register("chaster disconnect", "Remove your Chaster connection from this bot", self._handle_chaster_disconnect)
        self.router.register_family("chaster", invalid_handler=self._handle_chaster_invalid)

        self.router.register("memory", "Show what the coach remembers about you (Coaching Memory)", self._handle_memory)
        self.router.register("forget all", "Erase everything in your Coaching Memory and your behavioral telemetry", self._handle_forget_all)
        self.router.register("telemetry", "Show what behavioral telemetry is recorded about you", self._handle_telemetry)
        self.router.register("vacation", "Show your planned or running vacation (pause mode)", self._handle_vacation_status)
        self.router.register("vacation status", "Show your planned or running vacation (pause mode)", self._handle_vacation_status)
        self.router.register("vacation cancel", "Cancel a planned vacation that has not started (only while unlocked)", self._handle_vacation_cancel)
        self.router.register("vacation end", "End a running vacation now", self._handle_vacation_end)
        self.router.register("research status", "Show whether your pseudonymized telemetry is shared for research", self._handle_research_status)
        self.router.register("research on", "Share pseudonymized numeric telemetry with the owner's research server", self._handle_research_on)
        self.router.register("research off", "Stop research sharing and ask the server to erase what was sent", self._handle_research_off)
        self.router.register_family("research", invalid_handler=self._handle_research_status)
        self.router.register("conditioning status", "Show whether Progressive Conditioning is on for you", self._handle_conditioning_status)
        self.router.register("conditioning off", "Stop Progressive Conditioning from increasing (it stays at its current level)",
                             self._handle_conditioning_off)
        self.router.register("conditioning on", "Let Progressive Conditioning continue", self._handle_conditioning_on)
        self.router.register_family("conditioning", invalid_handler=self._handle_conditioning_status)

        self.router.register("channel status", "Show where the bot sends its messages (Discord or Chaster chat)", self._handle_channel_status)
        self.router.register("channel discord", "Send the bot's messages to Discord (the default)", self._handle_channel_discord)
        self.router.register("channel chaster", "Send the bot's messages to your Chaster chat instead (opt-in)", self._handle_channel_chaster)
        self.router.register_family("channel", invalid_handler=self._handle_channel_invalid)

    def _is_owner(self, ctx: RequestContext) -> bool:
        return self._owner_user_id is not None and ctx.user.id == self._owner_user_id

    def _answer_review_consent(self) -> OutgoingMessage:
        try:
            return self._review_consent_reply()
        except Exception:
            logger.exception("Building the community review consent overview failed.")
            return OutgoingMessage(text="I could not read your locks on Chaster right now, so I cannot show your community review "
                                        "settings. Please try again in a few minutes.")

    def _answer_progress(self, ctx: RequestContext) -> OutgoingMessage:
        assignment = self.task_runtime.get_active_assignment(ctx.user.id)
        title = None
        if assignment is not None:
            template = self.task_catalog.get_template(assignment.template_id, assignment.template_version)
            title = self._present_title(template.title if template is not None else None)
        is_owner = self._is_owner(ctx)
        snapshot = read_progress(self._core, user_id=ctx.user.id, now=ctx.now, active_task_title=title, include_reviews=is_owner,
                                 vacation=self._vacation)
        if is_owner:
            snapshot = replace(snapshot, penalty_running=self.penalty_engine.get_active_or_frozen_penalty_window() is not None)
        return OutgoingMessage(text=render_progress(snapshot))

    def _handle_help(self, ctx: RequestContext) -> OutgoingMessage:
        return OutgoingMessage(text=CAPABILITIES_TEXT)

    def _handle_status(self, ctx: RequestContext) -> OutgoingMessage:
        """
        Deliberately scoped to Penalty Engine only in this slice --
        TrustManager/GoalManager have no "list everything relevant"
        read API yet (both are designed around looking up a specific
        domain_id/goal_group_id, which this generic status command
        does not have). Adding one is a real future need, not invented
        here just to make this command feel more complete.
        """
        window = self.penalty_engine.get_active_or_frozen_penalty_window()
        if window is None:
            return OutgoingMessage(text="No active penalty window right now.")
        remaining = remaining_active_hours(window, ctx.now)
        target = target_active_hours(window)
        return OutgoingMessage(
            text=(
                f"Penalty window: {window.status.value}\n"
                f"~{remaining:.1f}h remaining of {target:.1f}h target."
            ),
        )

    def _handle_preferences(self, ctx: RequestContext) -> OutgoingMessage:
        """
        Read-only. Only ever reachable once onboarding is complete
        (handle_message() routes an incomplete user through onboarding
        before this table is ever consulted) -- so `preferences` here
        is guaranteed to have language/ai_gender/identity_id all set,
        but this method re-checks rather than assuming, since a future
        caller of this handler shouldn't have to know that invariant to
        stay safe.
        """
        preferences, _was_created = self.onboarding_service.get_or_create_preferences(ctx.user.id, now=ctx.now)
        if not self.onboarding_service.is_complete(preferences):
            return OutgoingMessage(text="You haven't finished setup yet.")
        entry = get_identity(preferences.identity_id) if preferences.identity_id else None
        name = entry.display_name(preferences.language or "en") if entry else "(unknown)"
        return OutgoingMessage(
            text=(
                f"Language: {preferences.language}\n"
                f"AI voice: {preferences.ai_gender}\n"
                f"Personality: {name}"
            ),
        )

    # -------------------------------------------------------------------
    # Advanced Mode -- read/request/cancel/confirm
    # -------------------------------------------------------------------

    def _settle_mode_state(self, ctx: RequestContext) -> None:
        """Explicit application-layer orchestration, called at the start
        of every `mode ...` command -- never hidden inside a read.
        Settles Penalty Window state first (its own owner's job, via
        its own public API), then applies any deterministic,
        time/PW-driven mode-transition-request transitions. Both are
        writes, both are explicit here; `AdvancedMode`'s own read-only
        API (`get_current_mode`/`get_active_request`) never triggers
        either on its own -- see advanced_mode/README.md."""
        self.penalty_engine.ensure_current_state(ctx.now)
        self.advanced_mode_admin.advance_transition_state(self.penalty_engine, now=ctx.now)

    def _consent_id_for(self, ctx: RequestContext) -> str | None:
        """Builds a stable, per-message consent reference from the
        Discord message that triggered this call -- no general-purpose
        consent module or table; this is the smallest extension that
        gives `mode request ...` and `mode confirm` two independently
        auditable references, since they are necessarily two different
        incoming messages. `None` if this adapter call somehow has no
        message id available (handlers must check and respond safely,
        never construct a consent reference without one)."""
        if ctx.external_message_id is None:
            return None
        return f"discord_message:{ctx.external_message_id}"

    def _handle_mode_invalid(self, ctx: RequestContext) -> OutgoingMessage:
        """The 'mode' command-family invalid_handler -- catches
        anything starting with the token 'mode' that isn't one of the
        exact registered mode commands (e.g. 'mode nonsense',
        'mode request nonsense'). Deterministic, never falls through
        to Conversation Engine -- no fuzzy matching, just the family
        token check CommandRouter.route() itself performs."""
        return OutgoingMessage(
            text="That's not a recognized `mode` command. Try `mode`, `mode status`, "
                 "`mode request advanced`, `mode request standard`, `mode cancel`, or `mode confirm`."
        )

    def _handle_mode_status(self, ctx: RequestContext) -> OutgoingMessage:
        self._settle_mode_state(ctx)
        state = self.advanced_mode.get_current_mode()
        request = self.advanced_mode.get_active_request()

        lines = [
            f"Current mode: {state.current_mode.value}",
            f"Activated: {state.mode_activated_at.isoformat()}",
        ]
        if request is None:
            lines.append("No active mode transition request.")
        else:
            lines.append("")
            lines.append(f"Pending request: {request.source_mode.value} -> {request.target_mode.value}")
            lines.append(f"Status: {request.status.value}")
            lines.append(f"Requested: {request.requested_at.isoformat()}")
            if request.wait_started_at is not None:
                lines.append(f"Wait started: {request.wait_started_at.isoformat()}")
            if request.wait_interrupted_at is not None:
                lines.append(f"Wait interrupted: {request.wait_interrupted_at.isoformat()}")
            if request.confirmable_at is not None:
                lines.append(f"Confirmable at: {request.confirmable_at.isoformat()}")
            can_confirm = request.status == ModeTransitionStatus.AWAITING_CONFIRMATION
            lines.append(f"Can confirm now: {'yes' if can_confirm else 'no'}")
        return OutgoingMessage(text="\n".join(lines))

    def _handle_mode_request_advanced(self, ctx: RequestContext) -> OutgoingMessage:
        self._settle_mode_state(ctx)
        consent_id = self._consent_id_for(ctx)
        if consent_id is None:
            return OutgoingMessage(text="Couldn't process that -- no stable message reference was available.")

        state = self.advanced_mode.get_current_mode()
        if state.current_mode == OperatingMode.ADVANCED:
            return OutgoingMessage(text="You're already in Advanced Mode.")

        try:
            request = self.advanced_mode_admin.request_transition(
                self.penalty_engine, target_mode=OperatingMode.ADVANCED,
                requested_via_consent_id=consent_id, now=ctx.now,
            )
        except ActiveModeTransitionExistsError:
            return OutgoingMessage(
                text="You already have a pending mode transition request. Send `mode status` to "
                     "see it, or `mode cancel` to cancel it first."
            )

        lines = [
            "Requested: Standard -> Advanced.",
            "This requires a full, uninterrupted 24-hour wait, followed by a second, separate "
            "confirmation -- send `mode confirm` once eligible.",
        ]
        if request.status == ModeTransitionStatus.BLOCKED_BY_PENALTY_WINDOW:
            lines.append(
                "A Penalty Window is currently active or frozen -- the 24-hour wait will only "
                "start once it ends."
            )
        lines.append(
            "Note: most other Advanced Mode rules (delegated task authority, tokens, hygiene "
            "values, etc.) are not wired in yet -- only the mode itself changes for now."
        )
        return OutgoingMessage(text="\n".join(lines))

    @requires_unlocked()
    def _handle_mode_request_standard(self, ctx: RequestContext) -> OutgoingMessage:
        self._settle_mode_state(ctx)
        consent_id = self._consent_id_for(ctx)
        if consent_id is None:
            return OutgoingMessage(text="Couldn't process that -- no stable message reference was available.")

        state = self.advanced_mode.get_current_mode()
        if state.current_mode == OperatingMode.STANDARD:
            return OutgoingMessage(text="You're already in Standard Mode.")

        try:
            request = self.advanced_mode_admin.request_transition(
                self.penalty_engine, target_mode=OperatingMode.STANDARD,
                requested_via_consent_id=consent_id, now=ctx.now,
            )
        except ActiveModeTransitionExistsError:
            return OutgoingMessage(
                text="You already have a pending mode transition request. Send `mode status` to "
                     "see it, or `mode cancel` to cancel it first."
            )
        except MinimumTimeInAdvancedNotMetError:
            minimum_met_at = state.mode_activated_at + timedelta(days=30)
            return OutgoingMessage(
                text=f"You need at least 30 days in Advanced Mode before requesting Standard -- "
                     f"eligible from {minimum_met_at.isoformat()}."
            )

        lines = [
            "Requested: Advanced -> Standard.",
            "This requires a full, uninterrupted 24-hour wait, followed by a second, separate "
            "confirmation -- send `mode confirm` once eligible.",
        ]
        if request.status == ModeTransitionStatus.BLOCKED_BY_PENALTY_WINDOW:
            lines.append(
                "A Penalty Window is currently active or frozen -- the 24-hour wait will only "
                "start once it ends."
            )
        return OutgoingMessage(text="\n".join(lines))

    def _handle_mode_cancel(self, ctx: RequestContext) -> OutgoingMessage:
        self._settle_mode_state(ctx)
        request = self.advanced_mode.get_active_request()
        if request is None:
            return OutgoingMessage(text="You don't have a pending mode transition request to cancel.")
        self.advanced_mode_admin.cancel_request(request.id, now=ctx.now)
        return OutgoingMessage(
            text=f"Cancelled your pending {request.source_mode.value} -> {request.target_mode.value} "
                 f"request. Your current mode is unchanged."
        )

    def _handle_mode_confirm(self, ctx: RequestContext) -> OutgoingMessage:
        self._settle_mode_state(ctx)
        consent_id = self._consent_id_for(ctx)
        if consent_id is None:
            return OutgoingMessage(text="Couldn't process that -- no stable message reference was available.")

        request = self.advanced_mode.get_active_request()
        if request is None:
            return OutgoingMessage(text="You don't have a pending mode transition request.")
        if request.target_mode == OperatingMode.STANDARD:          # lowering the difficulty: the Iron Contract (Phase G6)
            refusal = self._iron_refusal(ctx)
            if refusal is not None:
                return refusal

        if request.status != ModeTransitionStatus.AWAITING_CONFIRMATION:
            if request.status == ModeTransitionStatus.BLOCKED_BY_PENALTY_WINDOW:
                return OutgoingMessage(
                    text="Not yet -- a Penalty Window is still active or frozen. The 24-hour wait hasn't started."
                )
            if request.status == ModeTransitionStatus.PAUSED_BY_PENALTY_WINDOW:
                return OutgoingMessage(
                    text="Not yet -- a Penalty Window interrupted your wait. It will restart, from a "
                         "full 24 hours, once the Penalty Window ends."
                )
            if request.status == ModeTransitionStatus.WAITING:
                if request.confirmable_at is not None and request.confirmable_at > ctx.now:
                    hours = (request.confirmable_at - ctx.now).total_seconds() / 3600
                    return OutgoingMessage(
                        text=f"Not yet -- about {hours:.1f} more hour(s) of uninterrupted waiting "
                             f"needed. Send `mode status` to check anytime."
                    )
                return OutgoingMessage(text="Not yet -- still waiting. Send `mode status` to check.")
            return OutgoingMessage(text=f"Your request is currently {request.status.value} -- confirmation isn't available.")

        try:
            confirmed = self.advanced_mode_admin.confirm_transition(
                request.id, self.penalty_engine, confirmed_via_consent_id=consent_id, now=ctx.now,
            )
        except ModeTransitionInterruptedByPenaltyWindowError:
            return OutgoingMessage(
                text="A Penalty Window became active just before confirming -- your previous 24-hour "
                     "wait is no longer valid. It will restart, from a full 24 hours, once the "
                     "Penalty Window ends."
            )
        except ModeTransitionSourceModeMismatchError:
            return OutgoingMessage(
                text="Your mode changed by some other means before this could be confirmed -- this "
                     "request has been invalidated. Send a new `mode request ...` if you still want to switch."
            )
        except ModeTransitionNotConfirmableError:
            return OutgoingMessage(text="That request isn't ready to confirm right now. Send `mode status` to check.")

        return OutgoingMessage(text=f"Confirmed. Your mode is now: {confirmed.target_mode.value}.")

    # -------------------------------------------------------------------
    # Lock State commands (First Testable Keyholder Milestone, Slice C)
    # -------------------------------------------------------------------

    def _handle_lock_invalid(self, ctx: RequestContext) -> OutgoingMessage:
        """The 'lock' command-family invalid_handler -- same pattern as
        _handle_mode_invalid. Deterministic, never falls through to
        Conversation Engine."""
        return OutgoingMessage(
            text="I didn't catch that. Ask me what your lock status is, and you can update it with a button."
        )

    def _handle_lock_status(self, ctx: RequestContext) -> OutgoingMessage:
        state = self.lock_state.get_current_knowledge_state(ctx.user.id)
        token = str(uuid4())                        # a fresh audit reference for each card; a press is a new report, made when it is pressed
        actions = (ActionSpec(action_id=f"lock_report:locked:{token}", label="I'm locked"), ActionSpec(action_id=f"lock_report:unlocked:{token}", label="I'm unlocked"))
        if state == LockKnowledgeState.UNKNOWN:
            return OutgoingMessage(text="You haven't told me your lock status yet.", actions=actions)
        if state == LockKnowledgeState.LOCKED_USER_REPORTED:
            return OutgoingMessage(text="Locked (as you reported).", actions=actions)
        return OutgoingMessage(text="Unlocked (as you reported).", actions=actions)

    def press(self, *, discord_user_id: str, action: str, ident: str) -> OutgoingMessage:
        """The `lock_report:<locked|unlocked>:<token>` buttons. The presser's own account decides who reported; the same write as the exact phrases."""
        if action not in ("locked", "unlocked") or self._clock is None:
            return OutgoingMessage(text="That isn't one of the options.")
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT user_account_id FROM user_channel_identities WHERE channel = 'discord' AND external_id = ?", (discord_user_id,))
        if row is None:
            return OutgoingMessage(text="This isn't yours to answer.")
        status = LockReportStatus.LOCKED_USER_REPORTED if action == "locked" else LockReportStatus.UNLOCKED_USER_REPORTED
        self.lock_state_admin.report_status(user_id=row["user_account_id"], status=status, reported_via_consent_id=f"discord_button:{ident}", now=self._clock.now())
        return OutgoingMessage(text=f"Recorded: you reported yourself as {action}.")

    def _handle_lock_report_locked(self, ctx: RequestContext) -> OutgoingMessage:
        return self._report_lock_status(ctx, status=LockReportStatus.LOCKED_USER_REPORTED)

    def _handle_lock_report_unlocked(self, ctx: RequestContext) -> OutgoingMessage:
        return self._report_lock_status(ctx, status=LockReportStatus.UNLOCKED_USER_REPORTED)

    def _report_lock_status(self, ctx: RequestContext, *, status: LockReportStatus) -> OutgoingMessage:
        consent_id = self._consent_id_for(ctx)
        if consent_id is None:
            return OutgoingMessage(text="Couldn't process that -- no stable message reference was available.")
        self.lock_state_admin.report_status(
            user_id=ctx.user.id, status=status, reported_via_consent_id=consent_id, now=ctx.now,
        )
        word = "locked" if status == LockReportStatus.LOCKED_USER_REPORTED else "unlocked"
        return OutgoingMessage(text=f"Recorded: you reported yourself as {word}.")

    # -------------------------------------------------------------------
    # Task Runtime commands (First Testable Keyholder Milestone, Slice C)
    # -------------------------------------------------------------------

    def _handle_task_invalid(self, ctx: RequestContext) -> OutgoingMessage:
        return OutgoingMessage(
            text="I didn't catch that. Ask me what your task is, or tell me if you'd like a new one (`task request`)."
        )

    def _handle_chaster_connect(self, ctx: RequestContext) -> OutgoingMessage:
        if self.chaster_oauth_client is None or self.chaster_connections is None:
            return OutgoingMessage(text="Chaster integration is not configured on this bot.")

        existing = self.chaster_connections.get(ctx.user.id)

        # Replaces (never accumulates) any earlier still-pending state
        # for this user -- the chaster_oauth_states table's own
        # UNIQUE(user_id) constraint enforces this at the database
        # level. An older, still-open authorization link becomes
        # immediately dead the moment this runs again.
        state = self.chaster_states.create_or_replace(user_id=ctx.user.id, now=ctx.now)
        url = self.chaster_oauth_client.build_authorization_url(state=state)
        # A user who is already connected (typically because the
        # connection expired or was revoked) is never blocked: they get
        # a fresh link. The existing connection is left untouched until
        # the callback succeeds and atomically replaces it, so an
        # abandoned link cannot destroy a working connection.
        intro = (
            f"You already have a Chaster connection (status: {existing.connection_status.value}). "
            f"Use this fresh link to re-link your account: {url}"
            if existing is not None else f"Connect your Chaster account: {url}"
        )
        return OutgoingMessage(
            text=f"{intro}\n\n"
                 f"This link is single-use and expires in 10 minutes. If you run `chaster connect` "
                 f"again before finishing, this link will stop working.\n\n{TERMS_NOTICE}"
        )

    def _chaster_chat_context(self, text: str, ctx: RequestContext) -> str | None:
        """Verified Chaster lock data for an ORDINARY conversation turn.

        Loaded when the message mentions anything lock-related
        (`mentions_lock_topic`) OR the user has an active Chaster
        connection. It is supporting context only: any problem (not
        connected, reconnect needed, outage, unexpected error) yields
        None and the chat simply continues without it -- context loading
        can never break or delay a reply beyond one bounded read-only
        call."""
        if self.chaster_token_manager is None:
            return None
        try:
            if not (self._chaster_connection_is_active(ctx.user.id) or mentions_lock_topic(text)):
                return None
            outcome = self._read_chaster_outcome(ctx)
        except Exception:
            logger.warning("Chaster chat context could not be loaded -- continuing without it", exc_info=True)
            return None
        if outcome is None or outcome.kind != ChasterToolOutcomeKind.LOCKS:
            return None
        return build_chaster_chat_context(outcome.locks, now=ctx.now)

    def _chaster_connection_is_active(self, user_id: str) -> bool:
        connections = self.chaster_connections
        if connections is None:
            return False
        connection = connections.get(user_id)
        return connection is not None and connection.connection_status == ConnectionStatus.ACTIVE

    def _answer_chaster_status_question(self, ctx: RequestContext) -> OutgoingMessage:
        """A natural-language lock-status question gets the same deterministic reply as `chaster status`: the verified data itself, with no model in between. A model used to
        phrase it, and in live use it garbled verified data ("I don't know the current state of your lock"), so a fact about the lock no longer passes through one. Not configured,
        not connected, reconnect required and outages all get their own fixed reply here too, so a model can never improvise manual commands for them."""
        outcome = self._read_chaster_outcome(ctx)
        if outcome is None:
            return self._handle_chaster_status(ctx)
        return self._chaster_message(outcome.kind, now=ctx.now, detail=outcome.detail, locks=outcome.locks)

    def _read_chaster_outcome(self, ctx: RequestContext) -> ChasterToolOutcome | None:
        """One read-only Chaster call through the self-healing token
        manager. None means Chaster is not configured on this bot."""
        if self.chaster_token_manager is None:
            return None
        try:
            locks = self.chaster_token_manager.call_with_token(
                ctx.user.id, now=ctx.now,
                operation=lambda token: self.chaster_lock_client.list_active_locks(access_token=token),
            )
        except ChasterNotConnectedError:
            return ChasterToolOutcome(kind=ChasterToolOutcomeKind.NOT_CONNECTED)
        except ChasterReauthorizationRequiredError:
            return ChasterToolOutcome(kind=ChasterToolOutcomeKind.RECONNECT_REQUIRED)
        except ChasterRefreshUnavailableError:
            return ChasterToolOutcome(kind=ChasterToolOutcomeKind.TEMPORARILY_UNAVAILABLE)
        except ChasterLockApiError as exc:
            return ChasterToolOutcome(kind=ChasterToolOutcomeKind.API_ERROR, detail=str(exc))
        return ChasterToolOutcome(kind=ChasterToolOutcomeKind.LOCKS, locks=tuple(locks))

    def _handle_chaster_status(self, ctx: RequestContext) -> OutgoingMessage:
        """Deterministic -- never reaches ConversationEngine. Read-only:
        makes exactly one real Chaster call (list_active_locks), never
        a write/unlock endpoint. `chaster_lock_id`/status/title values
        are the real, confirmed LockForWearer top-level fields (see
        docs/architecture/chaster_integration_technical_design.md's own
        diagnostic history) -- shown as-is, never interpreted beyond
        display (e.g. `status`'s raw string is shown directly, never
        mapped to any kind of pass/fail decision -- this handler only
        displays what Chaster reports, it never gates or decides
        anything from it)."""
        if self.chaster_token_manager is None:
            return OutgoingMessage(
                text="Chaster integration is not configured on this bot.",
                embed=EmbedSpec(
                    title="Chaster Not Configured",
                    description="Chaster integration is not configured on this bot.",
                    color="red",
                ),
            )

        outcome = self._read_chaster_outcome(ctx)
        assert outcome is not None  # guarded above
        return self._chaster_message(outcome.kind, now=ctx.now, locks=outcome.locks, detail=outcome.detail)

    @staticmethod
    def _chaster_message(
        kind: ChasterToolOutcomeKind, *, now: datetime, locks: tuple[dict, ...] = (), detail: str | None = None,
    ) -> OutgoingMessage:
        """The single place Chaster state becomes a text + embed reply.
        Used by `chaster status` (the whole reply) and by the LLM tool
        path (only the embed is used there; the text is the model's)."""
        if kind == ChasterToolOutcomeKind.NOT_CONNECTED:
            return OutgoingMessage(
                text="Chaster Account Not Connected. Use `chaster connect` to link your account.",
                embed=EmbedSpec(
                    title="Chaster Account Not Connected",
                    description="Use `chaster connect` to link your Chaster account.",
                    color="red",
                ),
            )
        if kind == ChasterToolOutcomeKind.RECONNECT_REQUIRED:
            return OutgoingMessage(
                text="Chaster Reconnection Required. Your Chaster authorization has expired or was revoked. "
                     "Run `chaster connect` to get a fresh link.",
                embed=EmbedSpec(
                    title="Chaster Reconnection Required",
                    description="Your Chaster authorization has expired or was revoked and could not be "
                                "renewed automatically. Run `chaster connect` to get a fresh link.",
                    color="red",
                ),
            )
        if kind == ChasterToolOutcomeKind.TEMPORARILY_UNAVAILABLE:
            return OutgoingMessage(
                text="Chaster Temporarily Unavailable. Your connection is intact; please try again shortly.",
                embed=EmbedSpec(
                    title="Chaster Temporarily Unavailable",
                    description="Chaster could not be reached to renew your access. Your connection is "
                                "intact; please try again shortly.",
                    color="red",
                ),
            )
        if kind == ChasterToolOutcomeKind.API_ERROR:
            message = detail or "Chaster returned an error."
            return OutgoingMessage(
                text=f"Chaster API Error: {message}",
                embed=EmbedSpec(title="Chaster API Error", description=message, color="red"),
            )

        if not locks:
            return OutgoingMessage(
                text="You currently have no active locks.",
                embed=EmbedSpec(title="\U0001f512 Chaster Status", description="You currently have no active locks.", color="blue"),
            )
        fields = tuple(
            EmbedField(
                name=f"\U0001f512 {' '.join(lock_title(lock).split())[:200]}",   # a title is free text, and Discord refuses a field name over 256 characters
                value=f"**Status:** {lock_status_text(lock)}\n**Time Remaining:** {lock_time_remaining(lock, now=now)}",
            )
            for lock in locks
        )
        return OutgoingMessage(
            text=f"Active Locks ({len(locks)})",
            embed=EmbedSpec(title=f"\U0001f512 Active Locks ({len(locks)})", color="green", fields=fields),
        )

    def _embed_from_tool_outputs(self, tool_outputs: tuple, *, now: datetime) -> EmbedSpec | None:
        """Builds a deterministic embed from the REAL read-only tool
        output (never from model-written text), so a card can only show
        what Chaster actually reported. Only the first Chaster outcome
        is used; anything else yields no embed."""
        for output in tool_outputs:
            data = getattr(output, "data", None)
            if isinstance(data, ChasterToolOutcome):
                return self._chaster_message(data.kind, now=now, locks=data.locks, detail=data.detail).embed
        return None

    @staticmethod
    def _chaster_lock_title(lock: dict) -> str:
        return lock_title(lock)

    @staticmethod
    def _chaster_lock_status_text(lock: dict) -> str:
        return lock_status_text(lock)

    @staticmethod
    def _chaster_lock_time_remaining(lock: dict, *, now: datetime) -> str:
        return lock_time_remaining(lock, now=now)

    def _handle_chaster_disconnect(self, ctx: RequestContext) -> OutgoingMessage:
        """Deterministic local disconnect. Removes this bot's stored
        Chaster tokens and observation history for the user. It does
        NOT call Chaster: no token-revocation endpoint is confirmed to
        exist, so none is guessed at, and the reply says so plainly."""
        if self.chaster_connections is None:
            return OutgoingMessage(text="Chaster integration is not configured on this bot.")

        if not self.chaster_connections.delete(ctx.user.id):
            return OutgoingMessage(
                text="You have no Chaster connection to remove.",
                embed=EmbedSpec(
                    title="Nothing to Disconnect",
                    description="You have no Chaster connection. Use `chaster connect` to link your account.",
                    color="blue",
                ),
            )
        return OutgoingMessage(
            text="Chaster Disconnected. Your stored Chaster tokens were removed from this bot.",
            embed=EmbedSpec(
                title="Chaster Disconnected",
                description="Your stored Chaster tokens and fetched lock history were removed from this bot. "
                            "This does not notify Chaster. Use `chaster connect` to link again at any time.",
                color="green",
            ),
        )

    # Phase F2 -- Coaching Memory and Progressive Conditioning (the user's own control) ---------------------

    def _handle_memory(self, ctx: RequestContext) -> OutgoingMessage:
        if self._coaching_memory is None:
            return OutgoingMessage(text="Coaching Memory is not available on this bot.")
        entries = self._coaching_memory.entries(ctx.user.id)
        if not entries:
            return OutgoingMessage(text="Your Coaching Memory is empty.")
        lines = [f"`{e.short_id}` [{e.category}] {e.content}" for e in entries]
        return OutgoingMessage(text="Your Coaching Memory:\n" + "\n".join(lines)
                               + "\n\nErase one with `forget <id>` or everything with `forget all`.")

    def _handle_forget_all(self, ctx: RequestContext) -> OutgoingMessage:
        """Step one of two (Phase G6): a warning; `_confirm_pending_forget` erases on `yes`."""
        if self._coaching_memory is None and self._telemetry is None and self._research is None and self._coach is None:
            return OutgoingMessage(text="Coaching Memory is not available on this bot.")
        self._pending_forget[ctx.user.id] = ctx.now + FORGET_CONFIRM_WINDOW
        return OutgoingMessage(text=FORGET_ALL_WARNING)

    def _erase_everything(self, ctx: RequestContext) -> OutgoingMessage:
        parts = []
        if self._coaching_memory is not None:
            count = self._coaching_memory.forget_all(ctx.user.id)
            parts.append(f"Erased {count} entr{'y' if count == 1 else 'ies'}. Your Coaching Memory is empty.")
        if self._research is not None and self._research.revoke(ctx.user.id, now=ctx.now, reason="forget all"):
            parts.append("Research sharing is off, and the research server will be asked to erase everything sent under your pseudonym.")
        if self._telemetry is not None:
            erased = self._telemetry.forget_all(ctx.user.id)
            parts.append(f"Erased {erased} behavioral telemetry record{'' if erased == 1 else 's'}.")
        if self._coach is not None:
            erased = self._coach.forget_all(ctx.user.id)
            parts.append(f"Erased {erased} coach proposal{'' if erased == 1 else 's'} and commitment{'' if erased == 1 else 's'}. Your goals stay.")
        return OutgoingMessage(text=" ".join(parts))

    # Phase G6 -- the Iron Contract guard, the two-step `forget all` and vacations ------------------------------------------------------

    def _iron_refusal(self, ctx: RequestContext, *, always: bool = False) -> OutgoingMessage | None:
        if self._iron_contract is None:
            return None
        text = self._iron_contract.refusal(ctx.user.id, always=always)
        return None if text is None else OutgoingMessage(text=text)

    def _confirm_pending_forget(self, text: str, ctx: RequestContext) -> OutgoingMessage | None:
        """The second step of `forget all`: `yes` within FORGET_CONFIRM_WINDOW erases; any other message cancels the request (and is handled normally)."""
        expires = self._pending_forget.pop(ctx.user.id, None)
        if expires is None:
            return None
        if (text or "").strip().lower().rstrip(".!") == "yes" and ctx.now <= expires:
            return self._erase_everything(ctx)
        if (text or "").strip().lower().rstrip(".!") == "yes":
            return OutgoingMessage(text="That confirmation came too late; nothing was erased. Send `forget all` again if you still want it.")
        return None

    def _vacation_schedule(self, text: str, ctx: RequestContext) -> OutgoingMessage | None:
        """`vacation schedule <first day> <last day>` (YYYY-MM-DD, local dates, inclusive). Only while unlocked."""
        parts = (text or "").strip().split()
        if len(parts) < 2 or parts[0].lower() != "vacation" or parts[1].lower() != "schedule":
            return None
        if self._vacation is None:
            return OutgoingMessage(text="Vacations are not available on this bot.")
        if len(parts) != 4:
            return OutgoingMessage(text="Use `vacation schedule <first day> <last day>`, for example `vacation schedule 2026-12-24 2026-12-28`.")
        refusal = self._iron_refusal(ctx, always=True)
        if refusal is not None:
            return refusal
        try:
            start, end = date.fromisoformat(parts[2]), date.fromisoformat(parts[3])
        except ValueError:
            return OutgoingMessage(text="Dates must look like 2026-12-24.")
        outcome, vacation = self._vacation.schedule(ctx.user.id, start, end, now=ctx.now)
        texts = {
            "invalid": "The last day must not be before the first day.",
            "in_the_past": "A vacation cannot start in the past.",
            "too_long": f"A vacation can last at most {VACATION_MAX_DAYS} days.",
            "too_far_ahead": f"A vacation can start at most {VACATION_MAX_LEAD_DAYS} days ahead.",
            "already_planned": "You already have a vacation planned or running. Send `vacation` to see it.",
        }
        if vacation is None:
            return OutgoingMessage(text=texts.get(outcome.value, "That vacation could not be scheduled."))
        return OutgoingMessage(text=f"Vacation scheduled: {vacation.start_date.isoformat()} to {vacation.end_date.isoformat()}. "
                                    "During it no check-ins are sent and fully paused days are not evaluated; Progressive Conditioning does not rise. "
                                    "Penalties, tokens and your rules stay as they are. You can end it early at any time with `vacation end`.")

    def _handle_vacation_status(self, ctx: RequestContext) -> OutgoingMessage:
        if self._vacation is None:
            return OutgoingMessage(text="Vacations are not available on this bot.")
        v = self._vacation.current_or_next(ctx.user.id, ctx.now)
        if v is None:
            return OutgoingMessage(text="No vacation is planned. While you are unlocked you can schedule one with "
                                        "`vacation schedule <first day> <last day>` (at most "
                                        f"{VACATION_MAX_DAYS} days).")
        when = "running now" if v.running(ctx.now) else "planned"
        return OutgoingMessage(text=f"Vacation {when}: {v.start_date.isoformat()} to {v.end_date.isoformat()}. "
                                    + ("End it early with `vacation end`." if v.running(ctx.now) else "Cancel it with `vacation cancel` (only while unlocked)."))

    @requires_unlocked(always=True)
    def _handle_vacation_cancel(self, ctx: RequestContext) -> OutgoingMessage:
        if self._vacation is None:
            return OutgoingMessage(text="Vacations are not available on this bot.")
        if self._vacation.cancel_upcoming(ctx.user.id, now=ctx.now):
            return OutgoingMessage(text="Your planned vacation is cancelled.")
        return OutgoingMessage(text="There is no planned vacation that has not started. A running one can be ended with `vacation end`.")

    def _handle_vacation_end(self, ctx: RequestContext) -> OutgoingMessage:
        if self._vacation is None:
            return OutgoingMessage(text="Vacations are not available on this bot.")
        if self._vacation.end_now(ctx.user.id, now=ctx.now):
            return OutgoingMessage(text="Your vacation has ended. Everything runs normally again from now on.")
        return OutgoingMessage(text="No vacation is running right now.")

    def _handle_research_status(self, ctx: RequestContext) -> OutgoingMessage:
        if self._research is None:
            return OutgoingMessage(text="Research sharing is not available on this bot.")
        state = "on" if self._research.is_enabled(ctx.user.id) else "off"
        owner = "" if self._research.export_active else " The bot owner has not enabled the research export, so nothing is sent at the moment."
        return OutgoingMessage(text=f"Research sharing is {state} for you.{owner}\n{self._research.explanation}\nUse `research on` or `research off`.")

    def _handle_research_on(self, ctx: RequestContext) -> OutgoingMessage:
        if self._research is None:
            return OutgoingMessage(text="Research sharing is not available on this bot.")
        if not self._research.enable(ctx.user.id, now=ctx.now):
            return OutgoingMessage(text="Research sharing is already on.")
        owner = "" if self._research.export_active else " (The bot owner has not enabled the research export yet; nothing is sent until they do.)"
        return OutgoingMessage(text=f"Research sharing is on.{owner}\n{self._research.explanation}")

    def _handle_research_off(self, ctx: RequestContext) -> OutgoingMessage:
        if self._research is None:
            return OutgoingMessage(text="Research sharing is not available on this bot.")
        if not self._research.revoke(ctx.user.id, now=ctx.now):
            return OutgoingMessage(text="Research sharing is already off.")
        return OutgoingMessage(text="Research sharing is off. Nothing more is sent, and the research server will be asked to erase everything sent "
                                    "under your pseudonym.")

    def _handle_telemetry(self, ctx: RequestContext) -> OutgoingMessage:
        """Categories and counts only, never a metric value (the conditioning factor stays hidden, philosophy 4.2)."""
        if self._telemetry is None:
            return OutgoingMessage(text="Behavioral telemetry is not available on this bot.")
        summary = self._telemetry.summary(ctx.user.id)
        head = ("Behavioral telemetry is recorded for you: numeric facts derived from your check-ins, chat response times, token use and daily results, kept for 180 days "
                "and sent nowhere unless you turn on `research on`." if summary.recording else
                "Behavioral telemetry is not recorded for you (the invitation you accepted did not include it).")
        if not summary.counts:
            body = "There are no records."
        else:
            body = "Records: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(summary.counts.items()))
            body += f" (oldest {summary.oldest.date().isoformat()})." if summary.oldest else "."
        return OutgoingMessage(text=f"{head}\n{body}\nErase it with `forget all` (this also erases your Coaching Memory).")

    def _forget_one(self, text: str, ctx: RequestContext) -> OutgoingMessage | None:
        """`forget <id>` (the router only matches exact commands, so the argument is parsed here)."""
        parts = (text or "").strip().split()
        if len(parts) != 2 or parts[0].lower() != "forget" or parts[1].lower() == "all":
            return None
        if self._coaching_memory is None:
            return OutgoingMessage(text="Coaching Memory is not available on this bot.")
        if self._coaching_memory.forget(ctx.user.id, parts[1]):
            return OutgoingMessage(text="Erased.")
        return OutgoingMessage(text="No single entry matches that id. Use `memory` to see the ids.")

    def _handle_conditioning_status(self, ctx: RequestContext) -> OutgoingMessage:
        if self._conditioning is None:
            return OutgoingMessage(text="Progressive Conditioning is not available on this bot.")
        on = self._conditioning.is_enabled(ctx.user.id)
        return OutgoingMessage(text="Progressive Conditioning is " + ("on." if on else "off; it stays at its current level.")
                               + " Use `conditioning on` or `conditioning off`.")

    def _handle_conditioning_off(self, ctx: RequestContext) -> OutgoingMessage:
        if self._conditioning is None:
            return OutgoingMessage(text="Progressive Conditioning is not available on this bot.")
        self._conditioning.set_enabled(ctx.user.id, False, now=ctx.now)
        return OutgoingMessage(text="Progressive Conditioning is off. It stays at its current level and will not increase.")

    def _handle_conditioning_on(self, ctx: RequestContext) -> OutgoingMessage:
        if self._conditioning is None:
            return OutgoingMessage(text="Progressive Conditioning is not available on this bot.")
        self._conditioning.set_enabled(ctx.user.id, True, now=ctx.now)
        return OutgoingMessage(text="Progressive Conditioning is on again.")

    def _handle_channel_status(self, ctx: RequestContext) -> OutgoingMessage:
        if self._channel_service is None:
            return OutgoingMessage(text="Messages go to Discord. (Chaster chat is not available on this bot.)")
        return OutgoingMessage(text=f"Messages currently go to: {self._channel_service.current_channel(ctx.user.id).value}.")

    def _handle_channel_discord(self, ctx: RequestContext) -> OutgoingMessage:
        if self._channel_service is not None:
            self._channel_service.opt_out(ctx.user.id)
        return OutgoingMessage(text="Messages go to Discord.")

    def _handle_channel_chaster(self, ctx: RequestContext) -> OutgoingMessage:
        if self._channel_service is None:
            return OutgoingMessage(text="Chaster chat is not available on this bot.")
        from messaging.channel_service import OptInStatus
        status = self._channel_service.opt_in_chaster(ctx.user.id, ctx.now).status
        replies = {
            OptInStatus.ENABLED: "Done. I will now message you in your Chaster chat. Send `channel discord` here at any time to switch back.",
            OptInStatus.DISABLED_BY_OWNER: "Chaster chat is switched off on this bot.",
            OptInStatus.NOT_CONNECTED: "Connect your Chaster account first with `chaster connect`.",
            OptInStatus.BOT_NOT_READY: "Chaster chat is not set up on this bot yet.",
            OptInStatus.NO_CONVERSATION: "I could not find a Chaster chat with you. Send my Chaster account a message there first, then try again.",
            OptInStatus.UNAVAILABLE: "Chaster did not answer. Nothing was changed; try again later.",
        }
        return OutgoingMessage(text=replies[status])

    def _handle_channel_invalid(self, ctx: RequestContext) -> OutgoingMessage:
        return OutgoingMessage(text="Try `channel status`, `channel discord` or `channel chaster`.")

    def _handle_chaster_invalid(self, ctx: RequestContext) -> OutgoingMessage:
        return OutgoingMessage(
            text="That's not a recognized `chaster` command. Try `chaster connect`, `chaster status` or `chaster disconnect`."
        )

    def _present_title(self, title: str | None) -> str:
        """
        Deterministic Task Command Human-Readable Content: the exact
        same "never fabricate, render an explicit marker" convention
        Candidate B already established at the conversational
        prompt-rendering layer (conversation_engine/prompt_builder.py's
        own `_NOT_RECORDED`) -- reused here, not reinvented, so a
        legacy row with no recorded title reads identically whether
        the user sees it through free conversation or through a
        deterministic command reply.
        """
        return title if title is not None else "(not recorded)"

    def _handle_task_active(self, ctx: RequestContext) -> OutgoingMessage:
        assignment = self.task_runtime.get_active_assignment(ctx.user.id)
        if assignment is None:
            return OutgoingMessage(text="No active task. Send `task request` to get one.")
        # Resolves the assignment's own exact pinned (template_id, template_version) --
        # never current_version -- same invariant ActiveTaskContextProvider already
        # enforces for the conversational path (task_catalog's own composite FK
        # guarantees this row exists; a None here would be a genuine, unexpected
        # inconsistency, not a normal state -- handled the same way a missing
        # title is: an explicit "(not recorded)" marker, never a crash).
        template = self.task_catalog.get_template(assignment.template_id, assignment.template_version)
        title = self._present_title(template.title if template is not None else None)
        return OutgoingMessage(
            text=f"Active: {title} ({assignment.template_id}, assigned {assignment.assigned_at.isoformat()})."
        )

    def _handle_task_request(self, ctx: RequestContext) -> OutgoingMessage:
        consent_id = self._consent_id_for(ctx)
        if consent_id is None:
            return OutgoingMessage(text="Couldn't process that -- no stable message reference was available.")

        existing = self.task_runtime.get_active_assignment(ctx.user.id)
        if existing is not None:
            existing_template = self.task_catalog.get_template(existing.template_id, existing.template_version)
            existing_title = self._present_title(existing_template.title if existing_template is not None else None)
            return OutgoingMessage(
                text=f"You already have an active task: {existing_title}. "
                     f"Send `task complete` or `task cancel` first."
            )

        lock_knowledge_state = self.lock_state.get_current_knowledge_state(ctx.user.id)
        eligible = self.task_runtime.get_eligible_templates(
            role=TaskInstanceRole.PRIMARY, operating_mode=self.advanced_mode.get_current_mode().current_mode.value,
            lock_knowledge_state=lock_knowledge_state,
        )
        chosen = select_eligible_template(eligible)
        if chosen is None:
            return OutgoingMessage(text="No eligible task available right now.")

        try:
            assignment = self.task_runtime_admin.assign_task(
                user_id=ctx.user.id, template_id=chosen.template_id, lock_knowledge_state=lock_knowledge_state,
                assigned_via_consent_id=consent_id, now=ctx.now,
            )
        except TaskNotEligibleError:
            return OutgoingMessage(text="That task is no longer eligible. Try `task request` again.")
        except TaskAssignmentConcurrencyError:
            return OutgoingMessage(text="You already have an active task. Send `task complete` or `task cancel` first.")
        except TaskAssignmentReferentialIntegrityError:
            return OutgoingMessage(text="Something went wrong assigning that task. It's been logged.")
        except TaskTemplateNotFoundForAssignmentError:
            return OutgoingMessage(text="That task is no longer available. Try `task request` again.")

        # `chosen` is the exact TaskTemplateVersion already selected for this
        # assignment -- reused directly, never a second lookup.
        return OutgoingMessage(text=f"Assigned: {self._present_title(chosen.title)}.")

    def _handle_task_complete(self, ctx: RequestContext) -> OutgoingMessage:
        return self._resolve_task(ctx, resolve=self.task_runtime_admin.complete_task, resolved_prefix="Completed")

    def _handle_task_cancel(self, ctx: RequestContext) -> OutgoingMessage:
        return self._resolve_task(ctx, resolve=self.task_runtime_admin.cancel_task, resolved_prefix="Cancelled")

    def _resolve_task(self, ctx: RequestContext, *, resolve, resolved_prefix: str) -> OutgoingMessage:
        consent_id = self._consent_id_for(ctx)
        if consent_id is None:
            return OutgoingMessage(text="Couldn't process that -- no stable message reference was available.")

        assignment = self.task_runtime.get_active_assignment(ctx.user.id)
        if assignment is None:
            return OutgoingMessage(text="No active task to resolve. Send `task request` to get one.")

        try:
            resolve(assignment_id=assignment.id, resolved_via_consent_id=consent_id, now=ctx.now)
        except TaskAssignmentTransitionError:
            return OutgoingMessage(text="That task isn't active anymore. Send `task active` to check.")
        except TaskAssignmentNotFoundError:
            return OutgoingMessage(text="Something went wrong resolving that task. It's been logged.")

        # Title-aware task complete/cancel confirmation: resolves the
        # assignment's own exact pinned (template_id, template_version) --
        # never current_version -- the same invariant `_handle_task_active`/
        # `_handle_task_request` already enforce. `assignment` was read
        # BEFORE resolve() transitioned it, so its own pinned version is
        # unaffected either way; the catalog lookup is independent of the
        # assignment's own status.
        template = self.task_catalog.get_template(assignment.template_id, assignment.template_version)
        title = self._present_title(template.title if template is not None else None)
        return OutgoingMessage(text=f"{resolved_prefix}: {title}.")
