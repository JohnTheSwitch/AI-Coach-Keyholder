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

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from chaster_execution.control import ExecutionControl
from chaster_execution.executor import ChasterExecutor
from chaster_execution.hydration import make_snapshot_provider
from chaster_execution.models import ExecStatus
from chaster_execution.transport import build_keyholder_transport
from infrastructure.clock import Clock, SystemClock
from release_engine.service import ReleaseEngine
from rules_engine.repository import RulesEngineService
from schedule_windows.windows import WindowConfig, WindowKind, classify
from supervision.checkin_evaluator import evaluate_check_in
from supervision.checkins import CheckInSender, CheckInService
from supervision.repository import SupervisionService
from system.release_tick import ReleaseEnginePeriodicTask
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.system.composition")

__all__ = ["RulesExecutionWiring", "build_rules_and_execution", "window_config_from"]

_LIVE_LIMIT_STATUSES = (
    ExecStatus.PENDING.value, ExecStatus.EXECUTING.value, ExecStatus.EXECUTED.value, ExecStatus.NEEDS_REVIEW.value,
)


@dataclass
class RulesExecutionWiring:
    rules_engine: RulesEngineService | None = None
    executor: ChasterExecutor | None = None
    supervision: SupervisionService | None = None
    release_engine: ReleaseEngine | None = None
    release_task: ReleaseEnginePeriodicTask | None = None
    check_ins: CheckInService | None = None
    release_user_id: str | None = None
    notes: list[str] = field(default_factory=list)

    def release_phase(self, user_id: str) -> str | None:
        """The coarse, time-free release status for the model (None when the engine is off or the
        user is not the observed wearer)."""
        if self.release_engine is None or user_id != self.release_user_id:
            return None
        return self.release_engine.coarse_phase(user_id)


def window_config_from(config) -> WindowConfig:
    """Fail-fast: raises ValueError for an invalid layout / timezone."""
    return WindowConfig.build(
        timezone_name=config.app_timezone, quiet_start=config.quiet_hours_start, quiet_end=config.quiet_hours_end,
        work_start=config.workday_start, work_end=config.workday_end, holiday_country=config.holiday_country,
        weekend_release_start=config.weekend_release_start,
    )


def build_rules_and_execution(
    config, core, *, connections=None, oauth_client=None, lock_client=None, token_manager=None,
    transport=None, clock: Clock | None = None, check_in_sender: CheckInSender | None = None,
    check_in_complete=None,
) -> RulesExecutionWiring:
    """`transport` / `check_in_sender` / `check_in_complete` are injectable (tests; the bot supplies the real
    Discord sender and Ollama completion). Production passes transport=None and gets the real
    ChasterHttpTransport bound to the KEYHOLDER connection."""
    out = RulesExecutionWiring()
    trust = TrustManager(config.db_path, core=core)
    wearer_id = getattr(config, "chaster_execution_wearer_user_id", None)
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
        user = getattr(config, "check_in_user_id", None) or wearer_id
        domain = getattr(config, "check_in_trust_domain", None)
        if not user or not domain or check_in_sender is None:
            out.notes.append("ENABLE_CHECK_INS is true but check-ins are NOT active: they need CHECK_IN_USER_ID "
                             "(or CHASTER_EXECUTION_WEARER_USER_ID), CHECK_IN_TRUST_DOMAIN and a Discord sender.")
        else:
            def score():
                state = trust.get_domain_state(domain)
                return None if state is None else state.score

            out.check_ins = CheckInService(
                config.db_path, core=core, window_config=window_cfg, user_id=user, trust_domain=domain,
                get_trust_score=score, sender=check_in_sender,
                evaluate=lambda prompt, reply: evaluate_check_in(prompt, reply, check_in_complete),
            )
            out.notes.append("Proactive check-ins enabled (discretionary window only).")

    if not config.enable_rules_engine:
        out.notes.append("Rules Engine disabled (ENABLE_RULES_ENGINE=false).")
        if want_release:
            out.notes.append("ENABLE_RELEASE_ENGINE is true but the release engine is NOT active: it needs the Rules Engine.")
        return out

    observations = recorder = obs_service = None
    if connections is not None and oauth_client is not None and lock_client is not None and wearer_id:
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

    snapshot_provider = None
    if observations is not None:
        snapshot_provider = make_snapshot_provider(lambda: observations.get_latest(wearer_id))

    control = ExecutionControl(config.db_path, core=core)
    if config.enable_chaster_execution:
        problem = None
        if observations is None or not keyholder_id or token_manager is None:
            problem = ("it needs Chaster fully configured plus CHASTER_EXECUTION_WEARER_USER_ID and "
                       "CHASTER_EXECUTION_KEYHOLDER_USER_ID")
        else:
            if transport is None:
                transport = build_keyholder_transport(
                    token_manager=token_manager, keyholder_user_id=keyholder_id, clock=clock or SystemClock(),
                )
            if transport is None:
                problem = "no Chaster transport is available"
        if problem is not None:
            out.notes.append(f"ENABLE_CHASTER_EXECUTION is true but execution is NOT active: {problem}.")
        else:
            def granted_scopes():
                conn = connections.get(keyholder_id)
                if conn is None or conn.connection_status.value != "active":
                    return None
                return conn.granted_scopes

            out.executor = ChasterExecutor(
                config.db_path, core=core, enabled=True, control=control,
                transport=transport, get_latest_observation=lambda: observations.get_latest(wearer_id),
                refresh_observation=lambda now: obs_service.fetch_and_record(connection_id=wearer_id, now=now),
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
        if observations is None or not wearer_id:
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
                penalties = PenaltyEngine(config.db_path, core=core)
                supervision = out.supervision
                check_ins = out.check_ins
                out.release_engine = engine
                out.release_user_id = wearer_id
                out.release_task = ReleaseEnginePeriodicTask(
                    engine=engine, rules=out.rules_engine, user_id=wearer_id,
                    get_observation=lambda: observations.get_latest(wearer_id),
                    refresh_observation=lambda now: obs_service.fetch_and_record(connection_id=wearer_id, now=now),
                    penalty_window_active=lambda: penalties.get_active_or_frozen_penalty_window() is not None,
                    supervision_active=(lambda: supervision.is_active()) if supervision is not None else (lambda: False),
                    last_check_in_missed=(lambda: check_ins.last_check_in_missed()) if check_ins is not None else (lambda: False),
                    is_halted=control.is_halted, can_execute=out.executor is not None,
                    max_hold=timedelta(hours=config.max_barrier_hold_hours),
                )
                out.notes.append("Release engine enabled ("
                                 + ("live barrier release" if out.executor is not None else "planning only, nothing is spent")
                                 + ").")
    return out
