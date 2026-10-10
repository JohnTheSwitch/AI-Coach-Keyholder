"""
system/composition.py

Composition of the Rules Engine (Phase 2) and the Chaster execution plane
(Phase 3). Lives in `system/` (a wiring layer) so that neither the bot nor any
message handler constructs the executor directly. Everything is OFF unless
explicitly enabled; the execution plane additionally requires a confirmed real
transport, both Chaster identities and a clear runtime halt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from chaster_execution.control import ExecutionControl
from chaster_execution.executor import ChasterExecutor
from chaster_execution.hydration import make_snapshot_provider
from chaster_execution.models import ExecStatus
from chaster_execution.transport import build_keyholder_transport
from infrastructure.clock import Clock, SystemClock
from rules_engine.repository import RulesEngineService
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.system.composition")

__all__ = ["RulesExecutionWiring", "build_rules_and_execution"]

_LIVE_LIMIT_STATUSES = (
    ExecStatus.PENDING.value, ExecStatus.EXECUTING.value, ExecStatus.EXECUTED.value, ExecStatus.NEEDS_REVIEW.value,
)


@dataclass
class RulesExecutionWiring:
    rules_engine: RulesEngineService | None = None
    executor: ChasterExecutor | None = None
    notes: list[str] = field(default_factory=list)


def build_rules_and_execution(
    config, core, *, connections=None, oauth_client=None, lock_client=None, token_manager=None,
    transport=None, clock: Clock | None = None,
) -> RulesExecutionWiring:
    """`transport` is injectable for tests; production passes None and gets the real
    ChasterHttpTransport bound to the KEYHOLDER connection (chaster_execution/transport.py)."""
    out = RulesExecutionWiring()
    if not config.enable_rules_engine:
        out.notes.append("Rules Engine disabled (ENABLE_RULES_ENGINE=false).")
        return out

    trust = TrustManager(config.db_path, core=core)
    observations = recorder = None
    wearer_id = getattr(config, "chaster_execution_wearer_user_id", None)
    keyholder_id = getattr(config, "chaster_execution_keyholder_user_id", None)
    if connections is not None and oauth_client is not None and lock_client is not None and wearer_id:
        from chaster.provider_observation_repository import (
            ChasterProviderObservationRecorder,
            ChasterProviderObservations,
        )
        observations = ChasterProviderObservations(config.db_path, core=core)
        recorder = ChasterProviderObservationRecorder(config.db_path, core=core)

    snapshot_provider = None
    if observations is not None:
        snapshot_provider = make_snapshot_provider(lambda: observations.get_latest(wearer_id))

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
            from chaster.provider_observation_service import ChasterProviderObservationService

            service = ChasterProviderObservationService(
                connections=connections, oauth_client=oauth_client, lock_client=lock_client,
                observations=observations, recorder=recorder, token_manager=token_manager,
            )

            def granted_scopes():
                conn = connections.get(keyholder_id)
                if conn is None or conn.connection_status.value != "active":
                    return None
                return conn.granted_scopes

            out.executor = ChasterExecutor(
                config.db_path, core=core, enabled=True, control=ExecutionControl(config.db_path, core=core),
                transport=transport, get_latest_observation=lambda: observations.get_latest(wearer_id),
                refresh_observation=lambda now: service.fetch_and_record(connection_id=wearer_id, now=now),
                get_granted_scopes=granted_scopes,
            )
            out.notes.append("Chaster execution plane active (time adjustments only; subject to the runtime halt).")

    out.rules_engine = RulesEngineService(
        config.db_path, core=core, get_trust_state=trust.get_domain_state, get_lock_snapshot=snapshot_provider,
        limit_history_statuses=_LIVE_LIMIT_STATUSES if out.executor is not None else None,
    )
    out.notes.append(
        "Rules Engine enabled (" + ("LIVE execution plane" if out.executor is not None else "dry run only") + ")."
    )
    return out
