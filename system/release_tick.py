"""
system/release_tick.py

ReleaseEnginePeriodicTask (v0.39 Phase 2): the periodic driver of the Single Barrier
release. It lives in `system/` (the wiring layer) because it composes several modules
through injected, read-only callables; the domain rules stay in `release_engine/`.

One pass (`run`):
  1. sync provider states from the execution plane (decision status -> release state);
  2. if a release is in flight, stop;
  3. hard vetoes (penalty window, supervision, missed check-in) cancel any plan and stop;
  4. requirement met (balance >= 1) and the barrier confirmed up -> ensure the secret plan;
  5. plan due -> commit_authorization() (spends the token) -> record an UNFREEZE decision
     under rule `barrier.release` for the executor;
  2b. re-freeze: once a confirmed release session is over (its discretionary window ended, the day
     changed) and no token backs an open barrier, record a FREEZE decision (`barrier.freeze`);
  6. maximum-hold backstop (no token) when the barrier has been up for MAX_BARRIER_HOLD_HOURS.

Nothing is committed unless execution is actually possible (`can_execute`) and the runtime
halt is clear: a token is never spent for a release that cannot be carried out. The Chaster
API is only read, at most once per freshness window, and only when a decision depends on it.
No timestamp of a plan ever leaves release_engine; reports here carry counts and outcomes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from chaster.models import ChasterProviderObservation, ChasterProviderStatus
from release_engine.service import CommitStatus, ProviderState, ReleaseEngine, ReleaseGates
from rules_engine.limits import LOCK_DATA_MAX_AGE
from rules_engine.models import ActionType, DecisionStatus
from rules_engine.repository import RulesEngineService

logger = logging.getLogger("ai_coach_keyholder.system.release_tick")

__all__ = ["ReleaseEnginePeriodicTask", "TickReport", "BARRIER_RELEASE_RULE_ID", "BARRIER_FREEZE_RULE_ID"]

BARRIER_RELEASE_RULE_ID = "barrier.release"
BARRIER_FREEZE_RULE_ID = "barrier.freeze"
_REFREEZE_MAX_ATTEMPTS = 3
_REFREEZE_BACKOFF = timedelta(minutes=30)     # between attempts after a blocked / failed / expired decision
_REFREEZE_RECHECK = timedelta(hours=6)        # how long an observation of "frozen" is trusted while watching
_DECISION_GRACE = timedelta(minutes=16)      # executor expires a decision after 15 minutes


@dataclass
class TickReport:
    notes: list[str] = field(default_factory=list)       # short outcome codes only; never times

    def add(self, note: str) -> None:
        self.notes.append(note)


class ReleaseEnginePeriodicTask:
    def __init__(
        self,
        *,
        engine: ReleaseEngine,
        rules: RulesEngineService,
        user_id: str,
        get_observation: Callable[[], ChasterProviderObservation | None],
        refresh_observation: Callable[[datetime], None],
        penalty_window_active: Callable[[], bool],
        supervision_active: Callable[[], bool],
        last_check_in_missed: Callable[[], bool],
        is_halted: Callable[[], bool],
        can_execute: bool,
        max_hold: timedelta,
    ) -> None:
        self._engine = engine
        self._rules = rules
        self._user = user_id
        self._get_obs = get_observation
        self._refresh = refresh_observation
        self._penalty_active = penalty_window_active
        self._supervision_active = supervision_active
        self._missed = last_check_in_missed
        self._halted = is_halted
        self._can_execute = can_execute
        self._max_hold = max_hold
        # Phase F4: release_engine.conditioning.ProgressiveConditioning while ENABLE_SLAVE_TRAINING is on; the consumer of
        # `release.provider_confirmed` (system/startup.py) uses it. None = Progressive Conditioning is off.
        self.conditioning = None

    @property
    def engine(self) -> ReleaseEngine:
        return self._engine

    @property
    def user_id(self) -> str:
        return self._user

    # ------------------------------------------------------------------ public

    def run(self, now: datetime) -> TickReport:
        report = TickReport()
        self.sync(now, report)
        if self._engine.open_authorizations(self._user):
            report.add("release_in_flight")
            return report

        if self._maybe_refreeze(now, report):
            return report

        veto = ReleaseGates(
            lock_barrier_confirmed=True, penalty_window_active=bool(self._penalty_active()),
            supervision_active=bool(self._supervision_active()), last_check_in_missed=bool(self._missed()),
        ).hard_veto()
        if veto is not None:
            if self._engine.cancel_plan(user_id=self._user, reason=veto, now=now):
                report.add(f"plan_cancelled:{veto}")
            # The backstop deliberately ignores vetoes.
            self._maybe_backstop(now, report)
            return report

        balance = self._engine.get_balance(self._user)
        planned = self._engine.has_planned_release(self._user)

        if not planned and balance >= 1:
            obs = self._fresh_observation(now)
            if self._barrier_confirmed(obs, now) and self._engine.ensure_plan(user_id=self._user, now=now):
                report.add("plan_created")
                planned = True

        if planned and self._can_execute and not self._halted() and self._engine.plan_is_due(self._user, now):
            obs = self._fresh_observation(now)
            gates = self._gates(obs, now)
            outcome = self._engine.commit_authorization(user_id=self._user, gates=gates, now=now)
            report.add(f"commit:{outcome.status.value}")
            if outcome.status is CommitStatus.AUTHORIZED:
                self._record_unfreeze(outcome.authorization_id, "Release authorized by the release engine", now)
                return report

        self._maybe_backstop(now, report)
        return report

    def sync(self, now: datetime, report: TickReport | None = None) -> None:
        """Maps the execution plane's decision status onto the release state, step by step."""
        for auth_id, state in self._engine.open_authorizations(self._user):
            decision = self._rules.get_decision_for_trigger(auth_id)
            if decision is None:
                continue
            status = decision.status
            if status in (DecisionStatus.DRY_RUN, DecisionStatus.PENDING):
                if now - decision.created_at > _DECISION_GRACE:      # will never be executed: undo the spend
                    self._engine.mark_provider_failed(authorization_id=auth_id, detail="not executed in time", now=now)
                    self._note(report, "release_failed:not_executed")
                continue
            if status is DecisionStatus.EXECUTING:
                self._requested(auth_id, state, now)
            elif status is DecisionStatus.EXECUTED:
                self._requested(auth_id, state, now)
                self._engine.mark_provider_confirmed(authorization_id=auth_id, now=now)
                self._note(report, "release_confirmed")
            elif status in (DecisionStatus.FAILED, DecisionStatus.BLOCKED, DecisionStatus.EXPIRED):
                self._engine.mark_provider_failed(authorization_id=auth_id, detail=status.value, now=now)
                self._note(report, f"release_failed:{status.value}")
            elif status is DecisionStatus.NEEDS_REVIEW:
                self._requested(auth_id, state, now)
                self._engine.mark_physical_state_unknown(authorization_id=auth_id, detail="needs review", now=now)
                self._note(report, "release_needs_review")

    # ------------------------------------------------------------------ internals

    def _maybe_refreeze(self, now: datetime, report: TickReport) -> bool:
        """Raise the barrier again after a release session. Only for a barrier this system lowered (there
        is a confirmed release), never during the session, and only when a fresh observation shows a
        locked keyholder lock that is not frozen. One decision per release; failed attempts are retried
        with a back-off. Returns True when a decision was recorded."""
        if not self._can_execute or self._halted():
            return False
        last = self._engine.last_confirmed_release(self._user)
        if last is None:
            return False
        auth_id, confirmed_at = last
        if not self._engine.release_session_over(confirmed_at, now):
            return False                      # the token was spent for this very release: balance 0 is expected
        attempt, previous = 0, None
        while attempt < _REFREEZE_MAX_ATTEMPTS:
            existing = self._rules.get_decision_for_trigger(self._freeze_trigger(auth_id, attempt))
            if existing is None:
                break
            if existing.status not in (DecisionStatus.BLOCKED, DecisionStatus.FAILED, DecisionStatus.EXPIRED):
                return False                  # in flight, executed, or awaiting human review: never doubled
            previous, attempt = existing, attempt + 1
        else:
            return False
        if previous is not None and now - previous.created_at < _REFREEZE_BACKOFF:
            return False

        obs = self._get_obs()
        if (obs is None or obs.fetched_at <= confirmed_at or now - obs.fetched_at > _REFREEZE_RECHECK
                or (obs.is_frozen is not True and now - obs.fetched_at > LOCK_DATA_MAX_AGE)):
            obs = self._fresh_observation(now)
        if obs is None or not self._barrier_down(obs, now):
            return False
        no_token = self._engine.get_balance(self._user) < 1
        reason = "No token available for an open barrier" if no_token else "Release window ended"
        self._rules.record_barrier_decision(
            trigger_id=self._freeze_trigger(auth_id, attempt), action=ActionType.FREEZE,
            rule_id=BARRIER_FREEZE_RULE_ID, reason=reason, now=now,
        )
        report.add("barrier_refreeze:no_token" if no_token else "barrier_refreeze:window_ended")
        return True

    @staticmethod
    def _freeze_trigger(authorization_id: str, attempt: int) -> str:
        return f"barrier-freeze:{authorization_id}:{attempt}"

    @staticmethod
    def _barrier_down(obs: ChasterProviderObservation, now: datetime) -> bool:
        age = now - obs.fetched_at
        if age.total_seconds() < 0 or age > LOCK_DATA_MAX_AGE:
            return False
        return obs.status is ChasterProviderStatus.LOCKED and obs.has_keyholder is True and obs.is_frozen is False

    def _maybe_backstop(self, now: datetime, report: TickReport) -> None:
        if not self._can_execute or self._halted() or self._engine.open_authorizations(self._user):
            return
        latest = self._get_obs()
        if latest is None or latest.is_frozen is not True or latest.frozen_at is None:
            return
        if now - latest.frozen_at < self._max_hold:
            return
        obs = self._fresh_observation(now)
        if obs is None or obs.frozen_at is None or now - obs.frozen_at < self._max_hold:
            return
        out = self._engine.commit_backstop(
            user_id=self._user, barrier_since=obs.frozen_at,
            gates=ReleaseGates(lock_barrier_confirmed=self._barrier_confirmed(obs, now)), now=now,
        )
        report.add(f"backstop:{out.status.value}")
        if out.status is CommitStatus.AUTHORIZED:
            self._record_unfreeze(out.authorization_id, "Maximum barrier hold reached", now)

    def _record_unfreeze(self, authorization_id: str, reason: str, now: datetime) -> None:
        self._rules.record_barrier_decision(
            trigger_id=authorization_id, action=ActionType.UNFREEZE, rule_id=BARRIER_RELEASE_RULE_ID,
            reason=reason, now=now,
        )

    def _gates(self, obs: ChasterProviderObservation | None, now: datetime) -> ReleaseGates:
        return ReleaseGates(
            lock_barrier_confirmed=self._barrier_confirmed(obs, now),
            penalty_window_active=bool(self._penalty_active()), supervision_active=bool(self._supervision_active()),
            last_check_in_missed=bool(self._missed()),
        )

    @staticmethod
    def _barrier_confirmed(obs: ChasterProviderObservation | None, now: datetime) -> bool:
        """Fail closed: stale, unknown or ambiguous provider data never counts as 'barrier up'."""
        if obs is None:
            return False
        age = now - obs.fetched_at
        if age.total_seconds() < 0 or age > LOCK_DATA_MAX_AGE:
            return False
        return obs.status is ChasterProviderStatus.LOCKED and obs.has_keyholder is True and obs.is_frozen is True

    def _fresh_observation(self, now: datetime) -> ChasterProviderObservation | None:
        obs = self._get_obs()
        if obs is None or now - obs.fetched_at > LOCK_DATA_MAX_AGE:
            try:
                self._refresh(now)
            except Exception:
                logger.exception("Observation refresh failed in the release tick.")
            obs = self._get_obs()
        return obs

    def _requested(self, auth_id: str, state: ProviderState, now: datetime) -> None:
        if state is ProviderState.AUTHORIZED:
            self._engine.mark_provider_requested(authorization_id=auth_id, now=now)

    @staticmethod
    def _note(report: TickReport | None, note: str) -> None:
        if report is not None:
            report.add(note)
