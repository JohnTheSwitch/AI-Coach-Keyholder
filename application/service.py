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

import logging
from datetime import datetime, timedelta
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
from application.models import EmbedField, EmbedSpec, IncomingMessage, OutgoingMessage
from application.onboarding_service import OnboardingService
from application.router import CommandRouter, RequestContext
from conversation_engine.engine import ConversationEngine
from conversation_engine.fallback import FallbackReason, render_fallback
from conversation_engine.models import UnknownIdentityError
from application.user_service import UserService
from chaster.repository import ChasterConnectionRepository, ChasterOAuthStateRepository
from chaster.oauth_client import ChasterOAuthClient
from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.token_encryptor import TokenDecryptionError
from goal_management.repository import GoalManager
from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import parse_iso
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


class ApplicationService:
    def __init__(
        self, db_path: str | Path, *, core: CoreDatabase | None = None,
        conversation_engine: ConversationEngine | None = None,
        lock_state: LockState | None = None, task_runtime: TaskRuntime | None = None,
        task_catalog: TaskCatalog | None = None,
        chaster_connections: ChasterConnectionRepository | None = None,
        chaster_oauth_client: ChasterOAuthClient | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._conversation_engine = conversation_engine  # DI only -- never constructed here

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
            result = self.router.route(incoming.text, context)
            if result.matched:
                return result.outgoing

            if self._conversation_engine is None:
                return OutgoingMessage(text=self.router.unrecognized_text())

            if preferences.identity_id is None:
                return OutgoingMessage(text=render_fallback(FallbackReason.MISSING_REQUIRED_CONTEXT).text)

            try:
                response = self._conversation_engine.generate_response(
                    subject_key=user.id, current_user_message=incoming.text,
                    language=preferences.language or "en", identity_id=preferences.identity_id,
                    now=incoming.received_at,
                )
            except UnknownIdentityError:
                # A stored identity_id that no longer matches any catalog
                # entry -- same deterministic fallback as identity_id is
                # None, not the generic top-level error path; this is a
                # "missing required context" situation, not a bug crash.
                return OutgoingMessage(text=render_fallback(FallbackReason.MISSING_REQUIRED_CONTEXT).text)
            return OutgoingMessage(text=response.text)
        except Exception:
            logger.exception("ApplicationService.handle_message failed for channel=%r", incoming.channel)
            return OutgoingMessage(text="Something went wrong handling that. It's been logged.")

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
        self.router.register_family("chaster", invalid_handler=self._handle_chaster_invalid)

    def _handle_help(self, ctx: RequestContext) -> OutgoingMessage:
        return OutgoingMessage(text=self.router.help_text())

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
            text="That's not a recognized `lock` command. Try `lock status`, `lock report locked`, "
                 "or `lock report unlocked`."
        )

    def _handle_lock_status(self, ctx: RequestContext) -> OutgoingMessage:
        state = self.lock_state.get_current_knowledge_state(ctx.user.id)
        if state == LockKnowledgeState.UNKNOWN:
            return OutgoingMessage(text="No lock report yet. Send `lock report locked` or `lock report unlocked`.")
        if state == LockKnowledgeState.LOCKED_USER_REPORTED:
            return OutgoingMessage(text="Locked (as you reported).")
        return OutgoingMessage(text="Unlocked (as you reported).")

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
            text="That's not a recognized `task` command. Try `task request`, `task active`, "
                 "`task complete`, or `task cancel`."
        )

    def _handle_chaster_connect(self, ctx: RequestContext) -> OutgoingMessage:
        if self.chaster_oauth_client is None or self.chaster_connections is None:
            return OutgoingMessage(text="Chaster integration is not configured on this bot.")

        existing = self.chaster_connections.get(ctx.user.id)
        if existing is not None:
            return OutgoingMessage(
                text=f"You already have a Chaster connection (status: {existing.connection_status.value}). "
                     f"Reconnecting is not yet supported in this slice."
            )

        # Replaces (never accumulates) any earlier still-pending state
        # for this user -- the chaster_oauth_states table's own
        # UNIQUE(user_id) constraint enforces this at the database
        # level. An older, still-open authorization link becomes
        # immediately dead the moment this runs again.
        state = self.chaster_states.create_or_replace(user_id=ctx.user.id, now=ctx.now)
        url = self.chaster_oauth_client.build_authorization_url(state=state)
        return OutgoingMessage(
            text=f"Connect your Chaster account: {url}\n\n"
                 f"This link is single-use and expires in 10 minutes. If you run `chaster connect` "
                 f"again before finishing, this link will stop working."
        )

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
        if self.chaster_connections is None:
            return OutgoingMessage(text="Chaster integration is not configured on this bot.")

        try:
            access_token = self.chaster_connections.get_decrypted_access_token(ctx.user.id, now=ctx.now)
        except (LookupError, TokenDecryptionError):
            return OutgoingMessage(
                text="Chaster Account Not Connected. Use `chaster connect` to link your account.",
                embed=EmbedSpec(
                    title="Chaster Account Not Connected",
                    description="Use `chaster connect` to link your Chaster account.",
                    color="red",
                ),
            )

        try:
            locks = self.chaster_lock_client.list_active_locks(access_token=access_token)
        except ChasterLockApiError as exc:
            return OutgoingMessage(
                text=f"Chaster API Error: {exc}",
                embed=EmbedSpec(title="Chaster API Error", description=str(exc), color="red"),
            )

        if not locks:
            return OutgoingMessage(
                text="You currently have no active locks.",
                embed=EmbedSpec(title="\U0001f512 Chaster Status", description="You currently have no active locks.", color="blue"),
            )

        fields = tuple(
            EmbedField(
                name=f"\U0001f512 {self._chaster_lock_title(lock)}",
                value=f"**Status:** {self._chaster_lock_status_text(lock)}\n**Time Remaining:** {self._chaster_lock_time_remaining(lock, now=ctx.now)}",
            )
            for lock in locks
        )
        return OutgoingMessage(
            text=f"Active Locks ({len(locks)})",
            embed=EmbedSpec(title=f"\U0001f512 Active Locks ({len(locks)})", color="green", fields=fields),
        )

    @staticmethod
    def _chaster_lock_title(lock: dict) -> str:
        title = lock.get("title")
        return title if isinstance(title, str) and title.strip() else "(untitled)"

    @staticmethod
    def _chaster_lock_status_text(lock: dict) -> str:
        """Shows the real, raw status string exactly as Chaster
        reported it -- never interpreted/mapped to an enum. This
        project has confirmed `status`'s real field NAME and TYPE
        (str) from a real response, but never confirmed every real
        VALUE always matches a specific documented set -- displaying
        the raw string as-is sidesteps that gap entirely, since no
        decision is being made from it here, only a display."""
        status = lock.get("status")
        return status if isinstance(status, str) and status.strip() else "unknown"

    @staticmethod
    def _chaster_lock_time_remaining(lock: dict, *, now: datetime) -> str:
        """`endDate` is the real, confirmed top-level field this
        project has evidence for (NOT `endAt`, which has never been
        confirmed present in a real response -- not used here).
        Missing, non-string, or unparseable values all fall back to
        "Unlimited / Permanent" rather than raising -- a formatting
        detail is not a reason to fail the whole status reply."""
        raw_end_date = lock.get("endDate")
        if not isinstance(raw_end_date, str) or not raw_end_date.strip():
            return "Unlimited / Permanent"
        try:
            end_date = parse_iso(raw_end_date)
        except ValueError:
            return "Unlimited / Permanent"

        remaining = end_date - now
        if remaining.total_seconds() <= 0:
            return "Expired"

        total_minutes = int(remaining.total_seconds() // 60)
        days, rem_minutes = divmod(total_minutes, 24 * 60)
        hours, minutes = divmod(rem_minutes, 60)
        return f"{days}d {hours}h {minutes}m"

    def _handle_chaster_invalid(self, ctx: RequestContext) -> OutgoingMessage:
        return OutgoingMessage(text="That's not a recognized `chaster` command. Try `chaster connect`.")

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
