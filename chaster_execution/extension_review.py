"""
chaster_execution/extension_review.py

ExtensionReviewService (v0.39 Phase I2): the guarded, OPERATOR-ONLY way to set community review on one lock's `tasks` and
`verification-picture` extensions (peer verification on, visibility `all_members`, no Chaster punishments). It is reached only from the
local CLI (`python -m cli extension-review`); no model, conversation, Discord or periodic path can call it (import-guarded in tests).

`preview()` is the DRY RUN: it reads the lock and builds the complete body (chaster/extension_plan.py) and reports the exact field changes,
the plan id and every reason a real write would be refused. It sends nothing, writes nothing and works while the runtime is halted.

`apply()` is the live write. Checks, in this order (the first failure refuses, is audited, and nothing is sent):
  1. owner        the given identity equals OWNER_USER_ID (constant-time compare);
  2. confirmation the explicit --yes;
  3. input        a plain lock id and a 16-hex plan id;
  4. runtime halt the execution control is not halted (unreadable counts as halted); the owner clears it with scripts.execution_control;
  5. account      the connected Chaster account is configured and the lock can be read through it right now;
  6. lock         the lock is `locked` and the account's role on it holds `lock.extensions.edit`;
  7. plan         the body could be rebuilt faithfully, there is something to change, and its plan id equals the one from the dry run
                  (so exactly what was reviewed is sent, and a lock that changed since the dry run is refused).
  8. consent      (Phase I3, community direction only) the latest consent record for this lock approves the CURRENT notice
                  (chaster_execution/review_consent.py); without it nothing that widens who sees the photos is sent.
A real attempt is audited FIRST as `attempting` (migration 042), then sent once (never retried), then the lock is read again: `applied`
only when the review settings now match and the same extensions are still on the lock, otherwise `applied_unverified`.

The REVERT direction (`revert=True`, Phase I3) sets visibility back to `keyholder` (chaster/extension_plan.py `plan_review_revert`). It only
reduces who sees the photos, so it needs no consent and no plan id from a dry run (a given one must still match); every other check,
the runtime halt included, is the same.

`decide_consent()` records the OWNER's own decision about a lock the owner wears (the connected account's role on it is `wearer`). A
lock worn by another account is refused: that consent can only come from that wearer. A decline or a revocation immediately tries the
revert write when the lock currently has community review; the runtime halt still applies to that write.
"""

from __future__ import annotations

import hmac
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from chaster.extension_config import LockConfiguration, LockConfigurationReader, community_exposed, review_gaps
from chaster.extension_plan import ReviewPlan, plan_review_revert, plan_review_update
from chaster_execution.control import ExecutionControl
from chaster_execution.models import TransportOutcome
from chaster_execution.review_consent import ConsentDecision, ReviewConsent, ReviewConsentLedger
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso

logger = logging.getLogger("ai_coach_keyholder.chaster_execution.extension_review")

__all__ = ["ExtensionReviewService", "ExtensionReviewOutcome", "ExtensionReviewResult", "ConsentOutcome", "ConsentResult"]

_LOCK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLAN_ID = re.compile(r"^[0-9a-f]{16}$")
_MAX_ACTOR = 64


class ExtensionReviewOutcome(StrEnum):
    PLANNED = "planned"                        # dry run: nothing sent
    ALREADY_ON_TARGET = "already_on_target"
    NOTHING_TO_REVERT = "nothing_to_revert"    # revert: no review extension has community review
    APPLIED = "applied"                        # sent, and a fresh read shows the target with the same extensions
    APPLIED_UNVERIFIED = "applied_unverified"  # Chaster accepted, but the re-read failed or differs: check the lock on Chaster
    ATTEMPTING = "attempting"                  # only ever visible if the process died during the call
    # refusals (nothing was sent)
    OWNER_NOT_CONFIGURED = "owner_not_configured"
    NOT_OWNER = "not_owner"
    NOT_CONFIRMED = "not_confirmed"
    INVALID_INPUT = "invalid_input"
    HALTED = "halted"
    ACCOUNT_NOT_CONFIGURED = "account_not_configured"
    READ_FAILED = "read_failed"
    NOT_LOCKED = "not_locked"
    NOT_PERMITTED = "not_permitted"
    PLAN_REFUSED = "plan_refused"
    PLAN_MISMATCH = "plan_mismatch"
    NO_CONSENT = "no_consent"                  # Phase I3: the wearer has not approved the current notice for this lock
    # attempts that did not apply
    NOT_SENT = "not_sent"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_ELIGIBLE = "not_eligible"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"                        # ambiguous: the lock may or may not have changed; check on Chaster


REFUSALS = frozenset({
    ExtensionReviewOutcome.OWNER_NOT_CONFIGURED, ExtensionReviewOutcome.NOT_OWNER, ExtensionReviewOutcome.NOT_CONFIRMED,
    ExtensionReviewOutcome.INVALID_INPUT, ExtensionReviewOutcome.HALTED, ExtensionReviewOutcome.ACCOUNT_NOT_CONFIGURED,
    ExtensionReviewOutcome.READ_FAILED, ExtensionReviewOutcome.NOT_LOCKED, ExtensionReviewOutcome.NOT_PERMITTED,
    ExtensionReviewOutcome.PLAN_REFUSED, ExtensionReviewOutcome.PLAN_MISMATCH, ExtensionReviewOutcome.NO_CONSENT,
})

_SENT_OUTCOMES = {
    TransportOutcome.NOT_SENT: ExtensionReviewOutcome.NOT_SENT,
    TransportOutcome.UNAUTHORIZED: ExtensionReviewOutcome.UNAUTHORIZED,
    TransportOutcome.FORBIDDEN: ExtensionReviewOutcome.FORBIDDEN,
    TransportOutcome.NOT_ELIGIBLE: ExtensionReviewOutcome.NOT_ELIGIBLE,
    TransportOutcome.NOT_FOUND: ExtensionReviewOutcome.NOT_FOUND,
    TransportOutcome.UNKNOWN: ExtensionReviewOutcome.UNKNOWN,
}


@dataclass(frozen=True, kw_only=True)
class ExtensionReviewResult:
    outcome: ExtensionReviewOutcome
    audit_id: str | None = None                            # None for a dry run (nothing is audited)
    config: LockConfiguration | None = None
    plan: ReviewPlan | None = None
    halted: bool | None = None
    blockers: tuple[str, ...] = ()                          # dry run: why a real write would be refused right now
    detail: str | None = None                               # a short code (read problem, plan refusal)
    http_status: int | None = None
    remaining_gaps: tuple[str, ...] = field(default=())     # after a write: what the re-read still shows
    consent: ReviewConsent | None = None
    revert: bool = False

    @property
    def refused(self) -> bool:
        return self.outcome in REFUSALS


class ConsentOutcome(StrEnum):
    SHOWN = "shown"                                        # no decision given: the notice and the current state
    RECORDED = "recorded"
    OWNER_NOT_CONFIGURED = "owner_not_configured"
    NOT_OWNER = "not_owner"
    NOT_CONFIRMED = "not_confirmed"
    INVALID_INPUT = "invalid_input"
    ACCOUNT_NOT_CONFIGURED = "account_not_configured"
    READ_FAILED = "read_failed"
    WEARER_IS_ANOTHER_ACCOUNT = "wearer_is_another_account"   # only that wearer can decide; nothing recorded


@dataclass(frozen=True, kw_only=True)
class ConsentResult:
    outcome: ConsentOutcome
    consent: ReviewConsent | None = None                   # the state after the call
    config: LockConfiguration | None = None
    exposed: bool | None = None                            # the lock currently has community review
    detail: str | None = None
    revert: ExtensionReviewResult | None = None            # the automatic revert after a decline or revocation

    @property
    def done(self) -> bool:
        return self.outcome in (ConsentOutcome.SHOWN, ConsentOutcome.RECORDED)


class ExtensionReviewService:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, control: ExecutionControl,
                 reader: LockConfigurationReader, transport, owner_user_id: str | None, account_user_id: str | None, clock: Clock,
                 consents: ReviewConsentLedger | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._control = control
        self._reader = reader
        self._transport = transport
        self._owner = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None
        self._account = account_user_id.strip() if account_user_id and account_user_id.strip() else None
        self._clock = clock
        self._consents = consents if consents is not None else ReviewConsentLedger(self.db_path, core=self._core)

    # ------------------------------------------------------------------ dry run

    def preview(self, *, actor: str, lock_id: str, revert: bool = False) -> ExtensionReviewResult:
        refusal = self._identity(actor) or (None if isinstance(lock_id, str) and _LOCK_ID.match(lock_id) else ExtensionReviewOutcome.INVALID_INPUT)
        if refusal is None and self._account is None:
            refusal = ExtensionReviewOutcome.ACCOUNT_NOT_CONFIGURED
        if refusal is not None:
            return ExtensionReviewResult(outcome=refusal)
        halted = self._control.is_halted()
        config, problem = self._read(lock_id)
        if config is None:
            return ExtensionReviewResult(outcome=ExtensionReviewOutcome.READ_FAILED, halted=halted, detail=problem, revert=revert)
        plan = plan_review_revert(config) if revert else plan_review_update(config)
        consent = self._consents.state(lock_id)
        blockers = [b for b, on in (
            ("runtime halted (python -m scripts.execution_control clear \"reason\")", halted),
            (f"lock status is {config.status or 'unknown'}, not locked", config.status != "locked"),
            (f"the {config.role.value if config.role else 'unknown'} role may not edit extensions on this lock", not config.can_edit_extensions),
            (f"plan refused: {plan.refusal}", plan.refusal is not None),
            (f"the wearer's consent to community review is {consent.state.value} (python -m cli review-consent {lock_id} ...)",
             not revert and plan.has_changes and not consent.approved),
        ) if on]
        outcome = ExtensionReviewOutcome.PLANNED if plan.has_changes else (
            ExtensionReviewOutcome.NOTHING_TO_REVERT if revert else ExtensionReviewOutcome.ALREADY_ON_TARGET)
        return ExtensionReviewResult(outcome=outcome, config=config, plan=plan, halted=halted, blockers=tuple(blockers), consent=consent,
                                     revert=revert)

    # ------------------------------------------------------------------ live write

    def apply(self, *, actor: str, lock_id: str, plan_id: str | None, confirmed: bool, revert: bool = False) -> ExtensionReviewResult:
        actor_text, lock_text, plan_text = (actor or "")[:_MAX_ACTOR], (lock_id or "")[:_MAX_ACTOR], (plan_id or "")[:_MAX_ACTOR]

        def refuse(outcome, **kw) -> ExtensionReviewResult:
            changes = kw.get("plan").changes if kw.get("plan") else ()
            return ExtensionReviewResult(outcome=outcome, audit_id=self._audit(actor_text, lock_text, plan_text, changes, outcome),
                                         revert=revert, **kw)

        identity = self._identity(actor)
        if identity is not None:
            return refuse(identity)
        if confirmed is not True:
            return refuse(ExtensionReviewOutcome.NOT_CONFIRMED)
        plan_ok = (plan_id is None and revert) or (isinstance(plan_id, str) and _PLAN_ID.match(plan_id))
        if not isinstance(lock_id, str) or not _LOCK_ID.match(lock_id) or not plan_ok:
            return refuse(ExtensionReviewOutcome.INVALID_INPUT)
        if self._control.is_halted():
            return refuse(ExtensionReviewOutcome.HALTED, halted=True)
        if self._account is None:
            return refuse(ExtensionReviewOutcome.ACCOUNT_NOT_CONFIGURED)
        config, problem = self._read(lock_id)
        if config is None:
            return refuse(ExtensionReviewOutcome.READ_FAILED, detail=problem)
        if config.status != "locked":
            return refuse(ExtensionReviewOutcome.NOT_LOCKED, config=config)
        if not config.can_edit_extensions:
            return refuse(ExtensionReviewOutcome.NOT_PERMITTED, config=config)
        plan = plan_review_revert(config) if revert else plan_review_update(config)
        if plan.refusal is not None:
            return refuse(ExtensionReviewOutcome.PLAN_REFUSED, config=config, plan=plan, detail=plan.refusal)
        if not plan.has_changes:
            return refuse(ExtensionReviewOutcome.NOTHING_TO_REVERT if revert else ExtensionReviewOutcome.ALREADY_ON_TARGET, config=config, plan=plan)
        if plan_id is not None and not hmac.compare_digest(plan.plan_id, plan_id):
            return refuse(ExtensionReviewOutcome.PLAN_MISMATCH, config=config, plan=plan)
        consent = self._consents.state(lock_id)
        if not revert and not consent.approved:
            return refuse(ExtensionReviewOutcome.NO_CONSENT, config=config, plan=plan, consent=consent)

        audit_id = self._audit(actor_text, lock_text, plan.plan_id, plan.changes, ExtensionReviewOutcome.ATTEMPTING)  # claim BEFORE the call
        try:
            sent = self._transport.set_extensions(lock_id=lock_id, body=plan.body)
            http_status = sent.http_status
            outcome = None if sent.outcome is TransportOutcome.OK else _SENT_OUTCOMES.get(sent.outcome, ExtensionReviewOutcome.UNKNOWN)
        except Exception:
            logger.exception("The extension update call raised; the outcome is ambiguous.")
            outcome, http_status = ExtensionReviewOutcome.UNKNOWN, None
        gaps: tuple[str, ...] = ()
        if outcome is None:
            outcome, gaps = self._verify(lock_id, config, revert=revert)
        self._finish(audit_id, outcome, http_status)
        logger.info("Extension review %s on lock %s: %s (HTTP %s).", "revert" if revert else "update", lock_id, outcome.value, http_status)
        return ExtensionReviewResult(outcome=outcome, audit_id=audit_id, config=config, plan=plan, halted=False,
                                     http_status=http_status, remaining_gaps=gaps, consent=consent, revert=revert)

    # ------------------------------------------------------------------ consent (Phase I3)

    def decide_consent(self, *, actor: str, lock_id: str, decision: ConsentDecision | None, confirmed: bool) -> ConsentResult:
        """Show the notice and the state (decision None), or record the owner's decision about a lock the owner wears."""
        identity = self._identity(actor)
        if identity is not None:
            return ConsentResult(outcome=ConsentOutcome(identity.value))
        if not isinstance(lock_id, str) or not _LOCK_ID.match(lock_id):
            return ConsentResult(outcome=ConsentOutcome.INVALID_INPUT)
        if decision is not None and confirmed is not True:
            return ConsentResult(outcome=ConsentOutcome.NOT_CONFIRMED)
        if self._account is None:
            return ConsentResult(outcome=ConsentOutcome.ACCOUNT_NOT_CONFIGURED)
        config, problem = self._read(lock_id)
        if config is None:
            return ConsentResult(outcome=ConsentOutcome.READ_FAILED, consent=self._consents.state(lock_id), detail=problem)
        exposed = community_exposed(config)
        if decision is None:
            return ConsentResult(outcome=ConsentOutcome.SHOWN, consent=self._consents.state(lock_id), config=config, exposed=exposed)
        if config.role is None or config.role.value != "wearer":
            return ConsentResult(outcome=ConsentOutcome.WEARER_IS_ANOTHER_ACCOUNT, consent=self._consents.state(lock_id), config=config,
                                 exposed=exposed)
        self._consents.record(lock_id=lock_id, decision=decision, comment=f"owner cli: {decision.value}", now=self._clock.now())
        logger.info("Community review consent for lock %s recorded: %s.", lock_id, decision.value)
        reverted = None
        if decision is not ConsentDecision.APPROVED and exposed:
            reverted = self.apply(actor=actor, lock_id=lock_id, plan_id=None, confirmed=True, revert=True)
        return ConsentResult(outcome=ConsentOutcome.RECORDED, consent=self._consents.state(lock_id), config=config, exposed=exposed,
                             revert=reverted)

    def consent_states(self, lock_ids) -> dict[str, ReviewConsent]:
        """Read-only, for the `extension-config` report."""
        return {lock_id: self._consents.state(lock_id) for lock_id in lock_ids}

    # ------------------------------------------------------------------ internals

    def _identity(self, actor: str) -> ExtensionReviewOutcome | None:
        if self._owner is None:
            return ExtensionReviewOutcome.OWNER_NOT_CONFIGURED
        if not isinstance(actor, str) or not hmac.compare_digest(actor.encode("utf-8"), self._owner.encode("utf-8")):
            return ExtensionReviewOutcome.NOT_OWNER
        return None

    def _read(self, lock_id: str) -> tuple[LockConfiguration | None, str | None]:
        try:
            report = self._reader.read(user_id=self._account, lock_ids=[lock_id], now=self._clock.now())
        except Exception:
            logger.exception("Reading the lock configuration raised.")
            return None, "failed"
        if report.configurations:
            return report.configurations[0], None
        return None, (report.problems[0].split(": ", 1)[-1] if report.problems else "failed")

    def _verify(self, lock_id: str, before: LockConfiguration, *, revert: bool) -> tuple[ExtensionReviewOutcome, tuple[str, ...]]:
        after, _problem = self._read(lock_id)
        if after is None:
            return ExtensionReviewOutcome.APPLIED_UNVERIFIED, ("re-read failed",)
        if revert:
            gaps = ("community review is still on",) if community_exposed(after) else ()
        else:
            gaps = review_gaps(after)
        same = sorted(e.slug for e in after.extensions) == sorted(e.slug for e in before.extensions)
        if gaps or not same:
            return ExtensionReviewOutcome.APPLIED_UNVERIFIED, gaps + (() if same else ("the set of extensions changed",))
        return ExtensionReviewOutcome.APPLIED, ()

    def _audit(self, actor: str, lock_id: str, plan_id: str, changes, outcome: ExtensionReviewOutcome) -> str:
        audit_id = str(uuid.uuid4())
        now = self._clock.now()
        text = "; ".join(f"{c.slug} {c.field} {c.before} -> {c.after}" for c in changes)

        def write(tx: Transaction, _s: object) -> None:
            tx.execute(
                "INSERT INTO manual_extension_actions (id, created_at, actor, lock_id, plan_id, changes, outcome, http_status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (audit_id, _iso(now), actor, lock_id, plan_id, text, outcome.value, _iso(now)))

        apply_transition(self._core, write=write)
        return audit_id

    def _finish(self, audit_id: str, outcome: ExtensionReviewOutcome, http_status: int | None) -> None:
        now = self._clock.now()

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE manual_extension_actions SET outcome = ?, http_status = ?, updated_at = ? WHERE id = ? AND outcome = 'attempting'",
                       (outcome.value, http_status, _iso(now), audit_id))

        apply_transition(self._core, write=write)
