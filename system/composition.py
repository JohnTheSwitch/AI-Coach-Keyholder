"""
system/composition.py

Composition of the Rules Engine (Phase 2), the Chaster execution plane (Phase 3) and the
v0.39 release / supervision / check-in machinery. Lives in `system/` (a wiring layer) so
that neither the bot nor any message handler constructs these directly. Everything is OFF
unless explicitly enabled; the execution plane additionally requires a real transport, both
Chaster identities and a clear runtime halt; the release engine additionally requires the
Rules Engine and an observed wearer lock.
"""

from __future__ import annotations

import hashlib
import logging
from uuid import uuid4
from dataclasses import dataclass, field
from datetime import timedelta

from chaster_execution.control import ExecutionControl
from chaster_execution.executor import ChasterExecutor
from chaster_execution.extension_guard import ExtensionGuard, extension_guard_loop
from chaster_execution.hydration import make_snapshot_provider
from chaster_execution.models import ExecStatus
from chaster_execution.penalty_barrier import PenaltyBarrierExecutor
from chaster_execution.transport import ChasterHttpTransport, build_keyholder_transport
from infrastructure.clock import Clock, SystemClock
from release_engine.service import ReleaseEngine
from rules_engine.penalty_hold import PenaltyHold
from rules_engine.repository import RulesEngineService
from schedule_windows.windows import WindowConfig, WindowKind, classify
from supervision.checkin_evaluator import evaluate_check_in
from supervision.checkins import CheckInSender, CheckInService, SendOutcome
from supervision.escalation import RULE_GROUP_ID as ESCALATION_RULE_GROUP
from supervision.escalation import CheckInEscalation
from supervision.repository import SupervisionService
from system.daily_evaluation import DailyEvaluationTask
from system.release_notice import ReleaseNotifier
from system.release_tick import ReleaseEnginePeriodicTask
from trust_manager.models import (
    BreachDirectness,
    EvidenceConfidenceLevel,
    ImpactLevel,
    IncidentEvidence,
    IntentAssessment,
    RepetitionEvidence,
)
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.system.composition")

__all__ = ["RulesExecutionWiring", "build_rules_and_execution", "window_config_from"]

# A repeated-non-compliance Incident is always LOW severity: low impact, unclear intent, partial breach,
# no repetition weighting (severity points 0 + 0 + 1 + 0 -> minor).
_LOW_SEVERITY_EVIDENCE = IncidentEvidence(
    actual_or_potential_impact=ImpactLevel.LOW, rule_breach_directness=BreachDirectness.PARTIAL,
    repetition=RepetitionEvidence(same_rule_confirmed_count=0, evaluation_window_days=2),
    evidence_confidence=EvidenceConfidenceLevel.HIGH, intentionality=IntentAssessment.UNCLEAR,
)

_LIVE_LIMIT_STATUSES = (
    ExecStatus.PENDING.value, ExecStatus.EXECUTING.value, ExecStatus.EXECUTED.value, ExecStatus.NEEDS_REVIEW.value,
)


class _WithGlobalHalt:
    """The tenant's own ExecutionControl plus an optional platform-wide halt: halted when EITHER is set (both fail closed)."""

    def __init__(self, control, global_halt) -> None:
        self._control = control
        self._global = global_halt

    def is_halted(self) -> bool:
        return bool(self._control.is_halted()) or (self._global is not None and bool(self._global.is_halted()))

    def reason(self):
        return self._control.reason()

    def set_halted(self, halted, *, reason, now):
        self._control.set_halted(halted, reason=reason, now=now)


@dataclass
class RulesExecutionWiring:
    rules_engine: RulesEngineService | None = None
    executor: ChasterExecutor | None = None
    supervision: SupervisionService | None = None
    release_engine: ReleaseEngine | None = None
    release_task: ReleaseEnginePeriodicTask | None = None
    daily: DailyEvaluationTask | None = None
    check_ins: CheckInService | None = None
    release_user_id: str | None = None
    telemetry: object | None = None                 # behavioral_telemetry.collector.TelemetryCollector (Phase G1, always built)
    telemetry_store: object | None = None           # its store: the wearer's `telemetry` view and `forget all`
    telemetry_buffer: object | None = None          # behavioral_telemetry.runtime.RuntimeTelemetryBuffer: the engine's runtime_sink (Phase G2)
    research_preferences: object | None = None      # behavioral_telemetry.research.ResearchPreferences: `research on|off|status` (G4, always built)
    research_exporter: object | None = None         # behavioral_telemetry.research.ResearchExporter of this database, when the link is configured
    iron_contract: object | None = None             # iron_contract.guard.IronContract (Phase G6, always built)
    vacation: object | None = None                  # iron_contract.vacation.VacationService (Phase G6, always built)
    penalty_hold: object | None = None              # rules_engine.penalty_hold.PenaltyHold (Phase J2, always built): the redemption hold
    penalty_barrier: object | None = None           # chaster_execution.penalty_barrier.PenaltyBarrierExecutor (Phase J2, owner's system only)
    notes: list[str] = field(default_factory=list)

    def release_phase(self, user_id: str) -> str | None:
        """The coarse, time-free release status for the model (None when the engine is off or the
        user is not the observed wearer)."""
        if self.release_engine is None or user_id != self.release_user_id:
            return None
        return self.release_engine.coarse_phase(user_id)


def research_link_configured(config) -> bool:
    """Phase G4: endpoint (https), token and a key of at least 32 characters are all set."""
    from behavioral_telemetry.research import valid_endpoint
    key = getattr(config, "telemetry_hmac_key", None)
    return bool(valid_endpoint(getattr(config, "telemetry_endpoint", None)) and getattr(config, "telemetry_token", None) and key and len(key) >= 32)


def research_export_loop_for(config, exporters, clock, *, interval_seconds: float = 60.0):
    """Phase G4: the background export coroutine (one link for the owner's database and every tenant's), or None when the link is not configured.
    `exporters()` returns the current ResearchExporter of every live database."""
    if not research_link_configured(config):
        return None
    from behavioral_telemetry.research import TelemetryLink, research_export_loop
    link = TelemetryLink(endpoint=config.telemetry_endpoint, token=config.telemetry_token, key=config.telemetry_hmac_key)
    return research_export_loop(link, exporters, clock, interval_seconds=interval_seconds)


def window_config_from(config) -> WindowConfig:
    """Fail-fast: raises ValueError for an invalid layout / timezone."""
    return WindowConfig.build(
        timezone_name=config.app_timezone, quiet_start=config.quiet_hours_start, quiet_end=config.quiet_hours_end,
        work_start=config.workday_start, work_end=config.workday_end, holiday_country=config.holiday_country,
        weekend_release_start=config.weekend_release_start,
    )


def build_extension_guard(config, core, *, notifications, token_manager, lock_client, connections, halt_source=None, clock: Clock | None = None):
    """The extension guard for the owner's own account, or None. A tenant has none: its invitation never disclosed one."""
    account = getattr(config, "chaster_execution_keyholder_user_id", None)
    owner = getattr(config, "owner_user_id", None)
    if not getattr(config, "enable_extension_guard", True) or not account or token_manager is None or lock_client is None or connections is None:
        return None
    from chaster.extension_config import LockConfigurationReader
    from chaster.lock_directory import LockDirectory

    source = clock or SystemClock()
    directory = LockDirectory(token_manager=token_manager, lock_client=lock_client, connections=connections)

    def list_lock_ids(now):
        listing = directory.list_account_locks(user_id=account, now=now)
        return list(dict.fromkeys(l.lock_id for l in listing.wearer_locks + listing.keyholder_locks if l.status == "locked")), list(listing.problems)

    def notify(key, text, now):
        if owner:
            notifications.enqueue(user_id=owner, kind="extension_guard", dedupe_key=f"extension_guard:{key}", text=text, now=now)

    return ExtensionGuard(config.db_path, core=core, control=_WithGlobalHalt(ExecutionControl(config.db_path, core=core), halt_source),
                          reader=LockConfigurationReader(token_manager=token_manager, lock_client=lock_client),
                          transport=ChasterHttpTransport(token_manager=token_manager, keyholder_user_id=account, clock=source),
                          account_user_id=account, list_lock_ids=list_lock_ids, notify=notify)


def _warning_notifier(notifications, sender, user_id: str, clock: Clock | None):
    """`notify(text) -> accepted`. With the queue, "accepted" means durably queued, and the queue owns the retries; a tenant has no queue and sends directly."""
    if notifications is None:
        return lambda text: sender.send(user_id=user_id, text=text) is SendOutcome.DELIVERED
    source = clock or SystemClock()
    return lambda text: notifications.enqueue(user_id=user_id, kind="check_in_warning", dedupe_key=f"check_in_warning:{uuid4()}", text=text, now=source.now()) or True


def _release_sender(notifications, sender, user_id: str, clock: Clock | None):
    """A release notice is what the user is waiting for, so it skips quiet hours. The notifier keeps its own once-a-day ledger; the key only guards a retry."""
    if notifications is None:
        return lambda text: sender.send(user_id=user_id, text=text)
    source = clock or SystemClock()

    def send(text: str) -> None:
        now = source.now()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        notifications.enqueue(user_id=user_id, kind="release", dedupe_key=f"release:{now.date().isoformat()}:{digest}", text=text, now=now, immediate=True)

    return send


def build_rules_and_execution(
    config, core, *, connections=None, oauth_client=None, lock_client=None, token_manager=None,
    transport=None, clock: Clock | None = None, check_in_sender: CheckInSender | None = None,
    check_in_complete=None, keyholder_connections=None, keyholder_token_manager=None, halt_source=None,
    observation_source=None, telemetry_consent_target: str | None = None, telemetry_tenant_id: str = "tenant_0", notifications=None,
) -> RulesExecutionWiring:
    """`transport` / `check_in_sender` / `check_in_complete` are injectable (tests; the bot supplies the real
    Discord sender and Ollama completion). Production passes transport=None and gets the real
    ChasterHttpTransport bound to the KEYHOLDER connection.

    Multi-wearer (Phase E2): a tenant has its OWN wearer `connections` / `token_manager` (its database) but uses the platform's single
    keyholder connection, passed as `keyholder_connections` / `keyholder_token_manager` (default: the same objects, the single-wearer case).
    `halt_source` (any object with is_halted()) is the platform-wide halt; execution stops when it OR the tenant's own halt is set.
    `telemetry_consent_target` (Phase G1): in a tenant database, the consent target of the accepted invitation; telemetry is recorded only when it disclosed it."""
    out = RulesExecutionWiring()
    from behavioral_telemetry.collector import TelemetryCollector
    from behavioral_telemetry.runtime import RuntimeTelemetryBuffer
    from behavioral_telemetry.store import TelemetryStore
    out.telemetry_buffer = RuntimeTelemetryBuffer()
    out.telemetry_store = TelemetryStore(config.db_path, core=core, consent_target=telemetry_consent_target, buffer=out.telemetry_buffer)
    out.telemetry = TelemetryCollector(out.telemetry_store, buffer=out.telemetry_buffer)
    from behavioral_telemetry.research import ResearchExporter, ResearchPreferences
    export_on = bool(getattr(config, "enable_research_telemetry", False))
    out.research_preferences = ResearchPreferences(config.db_path, core=core, export_active=export_on and research_link_configured(config))
    if research_link_configured(config):
        out.research_exporter = ResearchExporter(core, tenant_id=telemetry_tenant_id, key=config.telemetry_hmac_key, export_records=export_on)
    elif export_on:
        out.notes.append("ENABLE_RESEARCH_TELEMETRY is true but the research export is NOT active: it needs an https:// TELEMETRY_ENDPOINT, "
                         "TELEMETRY_TOKEN and TELEMETRY_HMAC_KEY (at least 32 characters).")
    kh_connections = keyholder_connections if keyholder_connections is not None else connections
    kh_tokens = keyholder_token_manager if keyholder_token_manager is not None else token_manager
    trust = TrustManager(config.db_path, core=core)
    wearer_id = getattr(config, "chaster_execution_wearer_user_id", None)
    from iron_contract.guard import IronContract
    from iron_contract.vacation import VacationService
    try:
        vacation = VacationService(config.db_path, core=core, timezone_name=getattr(config, "app_timezone", None) or "Europe/Prague")
    except (KeyError, ValueError):                      # an invalid APP_TIMEZONE is reported below; vacations then use UTC dates
        vacation = VacationService(config.db_path, core=core, timezone_name="UTC")
        out.notes.append("Vacations use UTC dates: APP_TIMEZONE is not a valid time zone.")
    out.vacation = vacation
    out.iron_contract = IronContract(config.db_path, core=core, consent_target=telemetry_consent_target, wearer_user_id=wearer_id)
    keyholder_id = getattr(config, "chaster_execution_keyholder_user_id", None)
    want_release = bool(getattr(config, "enable_release_engine", False))
    want_check_ins = bool(getattr(config, "enable_check_ins", False))

    window_cfg = None
    if want_release or want_check_ins:
        try:
            window_cfg = window_config_from(config)
        except ValueError as exc:
            out.notes.append(f"NOT active: invalid scheduling configuration ({exc}).")
            want_release = want_check_ins = False

    if getattr(config, "enable_supervision", False):
        out.supervision = SupervisionService(config.db_path, core=core)
        out.notes.append("Proactive supervision state enabled (derived from Trust recalculations).")

    if want_check_ins:
        user = getattr(config, "check_in_user_id", None) or wearer_id or getattr(config, "owner_user_id", None)
        domain = getattr(config, "check_in_trust_domain", None)
        if not user or not domain or check_in_sender is None:
            out.notes.append("ENABLE_CHECK_INS is true but check-ins are NOT active: they need CHECK_IN_USER_ID "
                             "(or CHASTER_EXECUTION_WEARER_USER_ID, or OWNER_USER_ID), CHECK_IN_TRUST_DOMAIN and a Discord sender.")
        else:
            def score():
                state = trust.get_domain_state(domain)
                return None if state is None else state.score

            supervision = out.supervision
            minutes = lambda name, default: timedelta(minutes=int(getattr(config, name, default)))  # noqa: E731
            out.check_ins = CheckInService(
                config.db_path, core=core, window_config=window_cfg, user_id=user, trust_domain=domain,
                get_trust_score=score, sender=check_in_sender,
                evaluate=lambda prompt, reply: evaluate_check_in(prompt, reply, check_in_complete),
                # Strict Supervision Mode (Trust < 0.40) shortens the deadlines.
                is_strict=(lambda: supervision.is_active(domain)) if supervision is not None else (lambda: False),
                response_timeout=minutes("checkin_response_timeout_minutes", 60),
                follow_up_window=minutes("checkin_follow_up_minutes", 30),
                strict_response_timeout=minutes("checkin_strict_response_timeout_minutes", 30),
                strict_follow_up_window=minutes("checkin_strict_follow_up_minutes", 15),
                paused=lambda now, u=user: vacation.is_paused(u, now),
                escalation=CheckInEscalation(
                    config.db_path, core=core, user_id=user,
                    register_incident=lambda description, now: trust.register_incident_report(
                        rule_group_id=ESCALATION_RULE_GROUP, trust_domain=domain, description=description,
                        evidence=_LOW_SEVERITY_EVIDENCE, now=now).id,
                    notify=_warning_notifier(notifications, check_in_sender, user, clock),
                ),
            )
            out.notes.append("Proactive check-ins enabled (discretionary window only).")

    out.penalty_hold = PenaltyHold(core)
    if observation_source is None:           # the owner's own system only: a tenant has no penalty barrier
        barrier_on = bool(getattr(config, "enable_penalty_barrier", False))
        barrier_lock = getattr(config, "penalty_freeze_lock_id", None)
        barrier_ready = bool(barrier_lock and keyholder_id and kh_tokens is not None and lock_client is not None)
        if barrier_on and not barrier_ready:
            out.notes.append("ENABLE_PENALTY_BARRIER is true but the penalty barrier is NOT active: it needs PENALTY_FREEZE_LOCK_ID, "
                             "Chaster fully configured and CHASTER_EXECUTION_KEYHOLDER_USER_ID. Intents stay queued.")
        barrier_clock = clock or SystemClock()
        barrier_transport = transport
        if barrier_on and barrier_ready and barrier_transport is None:
            barrier_transport = ChasterHttpTransport(token_manager=kh_tokens, keyholder_user_id=keyholder_id, clock=barrier_clock)
        out.penalty_barrier = PenaltyBarrierExecutor(
            config.db_path, core=core, enabled=barrier_on and barrier_ready,
            control=_WithGlobalHalt(ExecutionControl(config.db_path, core=core), halt_source), transport=barrier_transport,
            read_lock=(lambda lock_id: kh_tokens.call_with_token(keyholder_id, now=barrier_clock.now(), operation=lambda token: lock_client.get_lock(
                access_token=token, lock_id=lock_id))) if barrier_ready else None,
            lock_id=barrier_lock)
        if barrier_on and barrier_ready:
            out.notes.append("Penalty barrier enabled: a Penalty Window freezes the configured keyholder lock (subject to the runtime halt).")

    if not config.enable_rules_engine:
        out.notes.append("Rules Engine disabled (ENABLE_RULES_ENGINE=false).")
        if want_release:
            out.notes.append("ENABLE_RELEASE_ENGINE is true but the release engine is NOT active: it needs the Rules Engine.")
        return out

    # Where lock observations come from: a tenant passes its managed-lock source (keyholder search, Phase E4); the single-wearer
    # system reads the wearer's own connection as before.
    get_obs = refresh_obs = None
    observations = recorder = obs_service = None
    if observation_source is not None and wearer_id:
        get_obs, refresh_obs = observation_source.get_latest, observation_source.refresh
    elif connections is not None and oauth_client is not None and lock_client is not None and wearer_id:
        from chaster.provider_observation_repository import (
            ChasterProviderObservationRecorder,
            ChasterProviderObservations,
        )
        from chaster.provider_observation_service import ChasterProviderObservationService

        observations = ChasterProviderObservations(config.db_path, core=core)
        recorder = ChasterProviderObservationRecorder(config.db_path, core=core)
        obs_service = ChasterProviderObservationService(
            connections=connections, oauth_client=oauth_client, lock_client=lock_client,
            observations=observations, recorder=recorder, token_manager=token_manager,
        )

        get_obs = lambda: observations.get_latest(wearer_id)  # noqa: E731
        refresh_obs = lambda now: obs_service.fetch_and_record(connection_id=wearer_id, now=now)  # noqa: E731

    out.iron_contract.get_observation = get_obs
    snapshot_provider = None
    if get_obs is not None:
        snapshot_provider = make_snapshot_provider(get_obs)

    control = _WithGlobalHalt(ExecutionControl(config.db_path, core=core), halt_source)
    if config.enable_chaster_execution:
        problem = None
        if get_obs is None or not keyholder_id or kh_tokens is None or kh_connections is None:
            problem = ("it needs Chaster fully configured plus CHASTER_EXECUTION_WEARER_USER_ID and "
                       "CHASTER_EXECUTION_KEYHOLDER_USER_ID")
        else:
            if transport is None:
                transport = build_keyholder_transport(
                    token_manager=kh_tokens, keyholder_user_id=keyholder_id, clock=clock or SystemClock(),
                )
            if transport is None:
                problem = "no Chaster transport is available"
        if problem is not None:
            out.notes.append(f"ENABLE_CHASTER_EXECUTION is true but execution is NOT active: {problem}.")
        else:
            def granted_scopes():
                conn = kh_connections.get(keyholder_id)
                if conn is None or conn.connection_status.value != "active":
                    return None
                return conn.granted_scopes

            out.executor = ChasterExecutor(
                config.db_path, core=core, enabled=True, control=control,
                transport=transport, get_latest_observation=get_obs, refresh_observation=refresh_obs,
                get_granted_scopes=granted_scopes,
                allow_barrier_actions=want_release,
                window_check=(lambda now: classify(window_cfg, now) is WindowKind.DISCRETIONARY) if window_cfg else None,
            )
            out.notes.append("Chaster execution plane active (time adjustments"
                             + (" and barrier release" if want_release else "") + "; subject to the runtime halt).")

    out.rules_engine = RulesEngineService(
        config.db_path, core=core, get_trust_state=trust.get_domain_state, get_lock_snapshot=snapshot_provider,
        limit_history_statuses=_LIVE_LIMIT_STATUSES if out.executor is not None else None,
    )
    out.notes.append(
        "Rules Engine enabled (" + ("LIVE execution plane" if out.executor is not None else "dry run only") + ")."
    )

    if want_release:
        if get_obs is None or not wearer_id:
            out.notes.append("ENABLE_RELEASE_ENGINE is true but the release engine is NOT active: it needs Chaster fully "
                             "configured and CHASTER_EXECUTION_WEARER_USER_ID.")
        else:
            from penalty_engine.repository import PenaltyEngine

            try:
                engine = ReleaseEngine(
                    config.db_path, core=core, window_config=window_cfg,
                    min_offset_minutes=config.unlock_window_min_offset_minutes,
                    max_offset_minutes=config.unlock_window_max_offset_minutes,
                    max_hold_hours=config.max_barrier_hold_hours,
                )
            except ValueError as exc:
                out.notes.append(f"ENABLE_RELEASE_ENGINE is true but the release engine is NOT active: {exc}.")
            else:
                penalties = PenaltyEngine(config.db_path, core=core)      # read-only use (window state)
                supervision = out.supervision
                out.release_engine = engine
                out.release_user_id = wearer_id
                out.release_task = ReleaseEnginePeriodicTask(
                    engine=engine, rules=out.rules_engine, user_id=wearer_id,
                    get_observation=get_obs, refresh_observation=refresh_obs,
                    penalty_window_active=lambda: penalties.get_active_or_frozen_penalty_window() is not None or out.penalty_hold.is_active(),
                    supervision_active=(lambda: supervision.is_active()) if supervision is not None else (lambda: False),
                    is_halted=control.is_halted, can_execute=out.executor is not None,
                    max_hold=timedelta(hours=config.max_barrier_hold_hours),
                )
                if check_in_sender is not None:
                    out.release_task.notifier = ReleaseNotifier(
                        core, user_id=wearer_id, send=_release_sender(notifications, check_in_sender, wearer_id, clock),
                        today=lambda now: window_cfg.local(now).date(), check_ins=out.check_ins)
                conditioning = None
                if getattr(config, "enable_slave_training", False):
                    from release_engine.conditioning import ProgressiveConditioning
                    conditioning = ProgressiveConditioning(config.db_path, core=core, vacation_in=vacation.running_id_in)
                    out.release_task.conditioning = conditioning
                    out.notes.append("Progressive Conditioning enabled (the per-database owner switch and the wearer's consent still apply).")
                daily_domain = getattr(config, "daily_evaluation_trust_domain", None) or "routine"
                out.daily = DailyEvaluationTask(
                    config.db_path, core=core, window_config=window_cfg, user_id=wearer_id, trust_domain=daily_domain,
                    engine=engine, trust=trust, conditioning=conditioning,
                    paused_between=lambda start, end, u=wearer_id: vacation.covers(u, start, end),
                )
                out.notes.append(f"Daily evaluation enabled at local midnight (token pool and clean-day streak; Trust domain {daily_domain!r}).")
                out.notes.append("Release engine enabled ("
                                 + ("live barrier release" if out.executor is not None else "planning only, nothing is spent")
                                 + ").")
    return out
