"""
system/startup.py

The System Composition Layer's startup orchestrator
(system_state_machine.md Section 7; System State Machine Finding 4:
"startup orchestration was never really [any single module's]
responsibility to begin with"). Owns only sequencing and lifecycle
coordination -- no domain state, no business decisions
(philosophy.md 2.11 applied to orchestration itself, per
system_state_machine.md's own note).

Three domain modules exist today (Trust Manager, Penalty Engine,
Recovery Plan) -- this orchestrator calls only their recovery steps, in
the order system_state_machine.md Section 7 establishes, and skips
steps 3-5 of that section's full sequence (Activity Authorization,
Hygiene Privilege, Goal Management) with no placeholder calls -- those
modules do not exist yet, and adding a commented-out or no-op call for
each would be exactly the kind of speculative scaffolding this
project's own discipline avoids.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from infrastructure.clock import Clock
from infrastructure.consumer_registry import ConsumerRegistry, process_pending_events
from infrastructure.database import Database as CoreDatabase
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.startup_lease import (
    StartupLeaseNotAcquired,
    acquire_system_startup_lease,
    release_system_startup_lease,
)
from goal_management.repository import GoalManager
from penalty_engine.repository import PenaltyEngine
from recovery_plan.models import RecoveryPlanStatus
from recovery_plan.repository import RecoveryPlanManager
from rules_engine.repository import TRIGGER_EVENT_TYPE, RulesEngineService
from trust_manager.models import CooperationAssessment, SeverityTier
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.system.startup")

STARTUP_LEASE_DURATION = timedelta(minutes=5)  # parameter, with margin above the expected recovery duration


def _emit_recovery_plan_event(tx, event_type: str, plan, source_event) -> None:
    write_event(
        tx,
        DomainEvent(
            event_type=event_type, source_module="recovery_plan",
            payload={"recovery_plan_id": plan.id, "penalty_window_id": plan.penalty_window_id},
            occurred_at=source_event.occurred_at,
        ),
    )


def build_consumer_registry(
    trust_manager: TrustManager, penalty_engine: PenaltyEngine, recovery_plan: RecoveryPlanManager,
    rules_engine: RulesEngineService | None = None,
) -> ConsumerRegistry:
    """
    The real, working cross-module wiring this project's architecture
    has described since domain_events_catalog.md:

    - Penalty Engine reacts to Trust Manager's `incident.confirmation_changed`,
      filtered to `new_confirmation=CONFIRMED` (Finding 1).
    - Recovery Plan reacts to every one of Penalty Engine's own
      `penalty_window.*` lifecycle events (RP-6) -- a second,
      independent instance of the same consumer-handler discipline: every
      handler below reads only its triggering event's own payload and
      never calls another module's public API mid-transaction
      (implementation_conventions.md Section 3; system/README.md). No
      payload extension was needed for Recovery Plan's reactions --
      penalty_window_id/base_duration_hours/new_target_active_hours
      were already present on the existing events.
    """
    registry = ConsumerRegistry()

    def on_incident_confirmation_changed(tx, event) -> None:
        if event.payload.get("new_confirmation") != "confirmed":
            return  # this event fires for every transition, not only reaching CONFIRMED (Finding 1)

        cooperation = CooperationAssessment(
            self_disclosed=event.payload["cooperation_self_disclosed"],
            active_cooperation_in_resolution=event.payload["cooperation_active_cooperation_in_resolution"],
        )
        result = penalty_engine._consume_confirmed_incident_in_transaction(
            tx,
            event.payload["incident_id"],
            event.payload["trust_domain"],
            event.payload["rule_group_id"],
            SeverityTier(event.payload["intrinsic_severity"]),
            cooperation,
            event.occurred_at,
        )
        if result is not None:
            penalty_engine._emit_consumption_events(tx, result, event.occurred_at)

    registry.register("incident.confirmation_changed", "penalty_engine", on_incident_confirmation_changed)

    def on_goal_accountability_assessment_recorded(tx, event) -> None:
        trust_manager._record_goal_assessment_evidence_in_transaction(
            tx,
            assessment_id=event.payload["assessment_id"],
            trust_domain=event.payload["trust_domain"],
            relevant_to_trust=event.payload["relevant_to_trust"],
            direction=event.payload["direction"],
            rationale=event.payload["rationale"],
            now=event.occurred_at,
        )

    registry.register(
        "goal_accountability_assessment.recorded", "trust_manager", on_goal_accountability_assessment_recorded,
    )

    if rules_engine is not None:
        # Phase 2 (dry run): only RECORDS a trigger here; the evaluation runs
        # after the cascade has settled (RulesEngineService.evaluate_pending).
        registry.register(TRIGGER_EVENT_TYPE, "rules_engine", rules_engine.record_trigger_in_transaction)

    def on_penalty_window_started(tx, event) -> None:
        plan = recovery_plan._create_plan_in_transaction(
            tx, event.payload["penalty_window_id"], event.payload["base_duration_hours"], event.occurred_at,
        )
        _emit_recovery_plan_event(tx, "recovery_plan.created", plan, event)

    def on_penalty_window_frozen(tx, event) -> None:
        plan = recovery_plan._mirror_status_in_transaction(
            tx, event.payload["penalty_window_id"], RecoveryPlanStatus.FROZEN, event.occurred_at,
        )
        if plan is not None:
            _emit_recovery_plan_event(tx, "recovery_plan.frozen", plan, event)

    def on_penalty_window_resumed(tx, event) -> None:
        plan = recovery_plan._mirror_status_in_transaction(
            tx, event.payload["penalty_window_id"], RecoveryPlanStatus.ACTIVE, event.occurred_at,
        )
        if plan is not None:
            _emit_recovery_plan_event(tx, "recovery_plan.resumed", plan, event)

    def on_penalty_window_completed(tx, event) -> None:
        plan = recovery_plan._mirror_status_in_transaction(
            tx, event.payload["penalty_window_id"], RecoveryPlanStatus.COMPLETED, event.occurred_at,
        )
        if plan is not None:
            _emit_recovery_plan_event(tx, "recovery_plan.completed", plan, event)

    def on_penalty_window_target_duration_changed(tx, event) -> None:
        plan = recovery_plan._regenerate_in_transaction(
            tx, event.payload["penalty_window_id"], event.payload["new_target_active_hours"], event.occurred_at,
        )
        if plan is not None:
            write_event(
                tx,
                DomainEvent(
                    event_type="recovery_plan.regenerated", source_module="recovery_plan",
                    payload={
                        "recovery_plan_id": plan.id, "penalty_window_id": event.payload["penalty_window_id"],
                        "new_version": plan.current_version, "new_capacity_hours": plan.recovery_credit_capacity_hours,
                    },
                    occurred_at=event.occurred_at,
                ),
            )

    registry.register("penalty_window.started", "recovery_plan", on_penalty_window_started)
    registry.register("penalty_window.frozen", "recovery_plan", on_penalty_window_frozen)
    registry.register("penalty_window.resumed", "recovery_plan", on_penalty_window_resumed)
    registry.register("penalty_window.completed", "recovery_plan", on_penalty_window_completed)
    registry.register("penalty_window.target_duration_changed", "recovery_plan", on_penalty_window_target_duration_changed)

    def on_recovery_plan_task_completed(tx, event) -> None:
        decision = penalty_engine._record_recovery_credit_in_transaction(
            tx,
            event.payload["recovery_task_completion_id"],
            event.payload["penalty_window_id"],
            event.payload["credit_hours"],
            event.occurred_at,
        )
        penalty_engine._emit_recovery_credit_event(tx, decision, event.occurred_at)

    registry.register("recovery_plan.task_completed", "penalty_engine", on_recovery_plan_task_completed)

    return registry


def on_system_startup(
    core: CoreDatabase, process_id: str, clock: Clock, rules_engine: RulesEngineService | None = None,
) -> None:
    """
    THE definitive startup entry point (system_state_machine.md Section
    7), called BEFORE the Discord bot starts / before the first request
    is accepted. Raises StartupLeaseNotAcquired if another instance is
    already performing startup reconciliation -- the caller decides what
    to do about that (wait, or treat it as an accidentally launched
    second instance and exit).
    """
    now = clock.now()
    lease = acquire_system_startup_lease(core, process_id, now, STARTUP_LEASE_DURATION)
    if lease is None:
        raise StartupLeaseNotAcquired("Another instance is already performing startup reconciliation.")

    try:
        trust_manager = TrustManager(core.db_path, core=core)
        penalty_engine = PenaltyEngine(core.db_path, core=core)
        recovery_plan = RecoveryPlanManager(core.db_path, core=core)
        goal_management = GoalManager(core.db_path, core=core)

        # 1. Trust Manager -- must run first; Penalty Engine's own
        #    consumption flow reads Incident.assessment via
        #    get_incident_assessment(), and must never see an incomplete
        #    result left over from a crash between confirmation and
        #    assessment (trust_manager_technical_design.md 14.3).
        trust_manager.recover_trust_manager_state(now)

        # 2. Penalty Engine -- foundational; any future module that
        #    reads its state (Activity Authorization, Hygiene Privilege)
        #    depends on this running before it, once those modules exist.
        penalty_engine.recover_penalty_window_state(now)

        # 3-4. Activity Authorization, Hygiene Privilege -- not yet
        #      implemented. No placeholder calls; see this module's
        #      docstring.

        # 5. Goal Management -- independent of every other module here
        #    (Section 1: reads nothing from the Trust Manager for its
        #    own decisions), so its position in this sequence relative
        #    to steps 1-2/6 does not matter; placed here to match
        #    system_state_machine.md's own step numbering as closely as
        #    this project's actual module set allows.
        goal_management.recover_goal_management_state(now)

        # 6. Recovery Plan -- a consistency check dependent on (2),
        #    otherwise independent of the not-yet-implemented steps 3-5
        #    (recovery_plan_technical_design.md 8). Detects, does not
        #    silently repair, any ACTIVE/FROZEN window missing its
        #    RecoveryPlan -- the standard at-least-once outbox
        #    redelivery is what actually creates it.
        recovery_plan.recover_recovery_plan_state(now)

        # 7. Outbox publisher -- LAST, so events generated by steps 1-2
        #    above are delivered immediately rather than waiting for the
        #    next cycle.
        registry = build_consumer_registry(trust_manager, penalty_engine, recovery_plan, rules_engine)
        process_pending_events(core, registry, claimant=process_id, now=now)

        # 8. Rules Engine (Phase 2, DRY RUN, off unless enabled): evaluates the
        #    triggers recorded above. Advisory only -- a failure here must never
        #    abort startup.
        if rules_engine is not None:
            try:
                rules_engine.evaluate_pending(now=now)
            except Exception:
                logger.exception("Rules Engine evaluation failed (dry run; startup continues).")
    finally:
        release_system_startup_lease(core, lease)
