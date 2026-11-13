"""
system/release_tick.py

ReleaseEnginePeriodicTask (v0.39 Phase 2): the periodic driver of the Single Barrier
release. It lives in `system/` (the wiring layer) because it composes several modules
through injected, read-only callables; the domain rules stay in `release_engine/`.

One pass (`run`):
  1. sync provider states from the execution plane (decision status -> release state);
  2. if a release is in flight, stop;
  3. hard vetoes (penalty window, supervision) cancel any plan and stop;
  4. requirement met (balance >= 1) and the barrier confirmed up -> ensure the secret plan;
  5. plan due -> the user is told first and any incomplete check-in is requested (system/release_notice.py) ->
     commit_authorization() (spends the token) -> record an UNFREEZE decision
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
from infrastructure.decision_log import release_decision
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
# Live, 2026-11-13: the system refroze the lock on 11 Nov 04:28, but the stored reading was the one from just before ("not frozen"). While a veto
# (supervision) held releases nothing refreshed it, and the backstop, which pre-checked that stored reading, would never have fired: the
# maximum-hold guarantee was gone. The backstop now looks at the lock itself at least every _BACKSTOP_RECHECK, and after a failed refresh
# tries again at most every _BACKSTOP_RETRY (no Chaster call every minute).
_BACKSTOP_RECHECK = timedelta(hours=6)
_BACKSTOP_RETRY = timedelta(hours=1)
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
        self._halted = is_halted
        self._can_execute = can_execute
        self._max_hold = max_hold
        self._backstop_refreshed_at: datetime | None = None
        # Phase F4: release_engine.conditioning.ProgressiveConditioning while ENABLE_SLAVE_TRAINING is on; the consumer of
        # `release.provider_confirmed` (system/startup.py) uses it. None = Progressive Conditioning is off.
        self.conditioning = None
        # Phase J2: system.release_notice.ReleaseNotifier when messages can be sent: the user is told before a release, never left wondering.
        self.notifier = None
        self._last_logged: tuple[str, ...] = ()

    @property
    def engine(self) -> ReleaseEngine:
        return self._engine

    @property
    def user_id(self) -> str:
        return self._user

    # ------------------------------------------------------------------ public

    def run(self, now: datetime) -> TickReport:
        report = self._run(now)
        notes = tuple(report.notes)
        if notes and notes != self._last_logged:          # the decision log (owner, 2026-11-10): each change of outcome once, not every minute
            release_decision("Release tick: " + ", ".join(notes) + ".", **self._facts(now))
        self._last_logged = notes
        return report

    def _facts(self, now: datetime) -> dict:
        """The reasoning behind a tick's outcome, for the decision log only."""
        facts: dict = {}
        try:
            obs = self._get_obs()
            facts.update(
                token_balance=self._engine.get_balance(self._user), plan_waiting=self._engine.has_planned_release(self._user),
                execution_enabled=self._can_execute, halted=bool(self._halted()), penalty_window_active=bool(self._penalty_active()),
                supervision_active=bool(self._supervision_active()),
            )
            if obs is not None:
                facts.update(lock_status=obs.status.value, lock_frozen=obs.is_frozen, frozen_since=obs.frozen_at, lock_read_at=obs.fetched_at)
                # Live, 2026-11-12: a 42-hour-old reading ("not frozen") read like the current state. Decisions refresh it when they need it and
                # count an old one as unknown; the log now says how old the reading is.
                age_hours = round((now - obs.fetched_at).total_seconds() / 3600, 1)
                facts["lock_reading_age_hours"] = age_hours
                if now - obs.fetched_at > LOCK_DATA_MAX_AGE:
                    facts["lock_reading"] = "stale: the last stored reading, not the lock now; nothing was decided from it"
        except Exception:
            logger.debug("Could not gather the facts for the decision log.", exc_info=True)
        return facts

    def _run(self, now: datetime) -> TickReport:
        report = TickReport()
        self.sync(now, report)
        if self._engine.open_authorizations(self._user):
            report.add("release_in_flight")
            return report

        if self._maybe_refreeze(now, report):
            return report

        if self._maybe_owner_test(now, report):
            return report

        veto = ReleaseGates(
            lock_barrier_confirmed=True, penalty_window_active=bool(self._penalty_active()),
            supervision_active=bool(self._supervision_active()),
        ).hard_veto()
        if veto is not None:
            if self._engine.cancel_plan(user_id=self._user, reason=veto, now=now):
                report.add(f"plan_cancelled:{veto}")
            elif self._engine.get_balance(self._user) >= 1:
                report.add(f"release_held:{veto}")          # a token waits, and the log says why (live, 2026-11-12: two silent days)
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

        if planned and (not self._can_execute or self._halted()) and self._engine.plan_is_due(self._user, now):
            report.add("plan_due_but_execution_off" if not self._can_execute else "plan_due_but_halted")
        if planned and self._can_execute and not self._halted() and self._engine.plan_is_due(self._user, now):
            obs = self._fresh_observation(now)
            gates = self._gates(obs, now)
            if (self.notifier is not None and self._engine.release_ready(self._user, gates, now)
                    and not self.notifier.clear_to_release(now)):
                report.add("release_waiting_for_check_in")       # told, not silent; nothing spent
                self._maybe_backstop(now, report)
                return report
            outcome = self._engine.commit_authorization(user_id=self._user, gates=gates, now=now)
            report.add(f"commit:{outcome.status.value}")
            if outcome.status is CommitStatus.AUTHORIZED:
                self._record_unfreeze(outcome.authorization_id, "Release authorized by the release engine", now)
                return report
            if outcome.status is CommitStatus.RETARGETED and self.notifier is not None:
                self.notifier.window_closed(now)

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

    def _maybe_owner_test(self, now: datetime, report: TickReport) -> bool:
        """The owner's one-time test release (`cli release-test-override`, owner 2026-11-13): spends a token despite the vetoes, with every other
        gate intact. Returns True when it released."""
        if not self._can_execute or self._halted() or not self._engine.has_owner_test(self._user, now):
            return False
        obs = self._fresh_observation(now)
        outcome = self._engine.commit_owner_test(user_id=self._user, gates=self._gates(obs, now), now=now)
        report.add(f"owner_test:{outcome.status.value}")
        if outcome.status is CommitStatus.AUTHORIZED:
            self._record_unfreeze(outcome.authorization_id, "Owner test release", now)
            return True
        return False

    def _maybe_backstop(self, now: datetime, report: TickReport) -> None:
        if not self._can_execute or self._halted() or self._engine.open_authorizations(self._user):
            return
        latest = self._get_obs()
        if (latest is None or now - latest.fetched_at > _BACKSTOP_RECHECK) and (
                self._backstop_refreshed_at is None or now - self._backstop_refreshed_at >= _BACKSTOP_RETRY):
            self._backstop_refreshed_at = now
            try:
                self._refresh(now)
            except Exception:
                logger.exception("Observation refresh failed for the maximum-hold backstop.")
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
        """A reading from before the latest confirmed release says nothing about the lock now: right after the backstop unfroze a lock, the
        15-minute-old reading still said "frozen since 6 days" and the backstop fired again every minute (live, 2026-11-10; the cooldown blocked
        each one before Chaster). Such a reading is refreshed, and if no newer one comes it counts as unknown."""
        obs = self._get_obs()
        last = self._engine.last_confirmed_release(self._user)
        released_at = last[1] if last is not None else None
        if obs is None or now - obs.fetched_at > LOCK_DATA_MAX_AGE or (released_at is not None and obs.fetched_at <= released_at):
            try:
                self._refresh(now)
            except Exception:
                logger.exception("Observation refresh failed in the release tick.")
            obs = self._get_obs()
        if obs is not None and released_at is not None and obs.fetched_at <= released_at:
            return None
        return obs

    def _requested(self, auth_id: str, state: ProviderState, now: datetime) -> None:
        if state is ProviderState.AUTHORIZED:
            self._engine.mark_provider_requested(authorization_id=auth_id, now=now)

    @staticmethod
    def _note(report: TickReport | None, note: str) -> None:
        if report is not None:
            report.add(note)
