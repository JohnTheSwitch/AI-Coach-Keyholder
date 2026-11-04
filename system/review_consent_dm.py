"""
system/review_consent_dm.py

DM-native consent management for community photo review (v0.39 Phase J1, slice 2). The owner asks in plain language ("who can see my
photos?", "stop community review") and gets the I3 notice with the current state of each locked lock and buttons (Accept / Decline /
Revoke). A click records exactly what `python -m cli review-consent` records, through the same `ExtensionReviewService.decide_consent()`.

This module is the ONLY bot-side door to the extension-review service, and it is deliberately narrow:
- it exposes showing the state and the three consent decisions, never the forward write (`apply` / `preview`);
- a decline or revocation of a lock that currently has community review runs the I3 automatic REVERT inside `decide_consent()` (visibility
  back to keyholder only; it only reduces exposure, passes the runtime halt by the owner's I3 decision, and is audited);
- only the owner's verified Discord account can decide (constant-time compare); the decision is recorded for the owner's internal user id;
- a lock the connected account holds as keyholder can be decided only when the owner has already attested once, on the CLI, that its
  wearer account is the owner's own (`--wearer-is-my-account`); the attestation is carried forward and written into the new record.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from application.models import ActionSpec, EmbedField, EmbedSpec, OutgoingMessage
from chaster_execution.extension_review import ConsentOutcome, ExtensionReviewOutcome, ExtensionReviewService
from chaster_execution.review_consent import NOTICE_TEXT, ConsentDecision, ConsentState, ReviewConsentLedger

logger = logging.getLogger("ai_coach_keyholder.system.review_consent_dm")

__all__ = ["ACTION_PREFIX", "LockConsentView", "ReviewConsentDm", "render_overview", "action_id", "build_review_consent_dm"]

ACTION_PREFIX = "review_consent"
_DECISIONS = {"accept": ConsentDecision.APPROVED, "decline": ConsentDecision.REJECTED, "revoke": ConsentDecision.REVOKED}


def action_id(action: str, lock_id: str) -> str:
    return f"{ACTION_PREFIX}:{action}:{lock_id}"


@dataclass(frozen=True, kw_only=True)
class LockConsentView:
    lock_id: str
    role: str
    state: ConsentState | None          # None when the lock could not be read
    exposed: bool | None


class ReviewConsentDm:
    def __init__(self, service: ExtensionReviewService, ledger: ReviewConsentLedger, *, owner_user_id: str | None,
                 owner_discord_id: str | None, list_lock_ids: Callable[[datetime], list[str]], now: Callable[[], datetime]) -> None:
        self._service = service
        self._ledger = ledger
        self._owner_user_id = owner_user_id
        self._owner_discord_id = owner_discord_id
        self._list_lock_ids = list_lock_ids
        self._now = now

    def is_owner_discord(self, discord_user_id: str) -> bool:
        return bool(self._owner_discord_id) and isinstance(discord_user_id, str) and hmac.compare_digest(
            discord_user_id.encode("utf-8"), self._owner_discord_id.encode("utf-8"))

    def overview(self) -> list[LockConsentView]:
        views = []
        for lock_id in self._list_lock_ids(self._now()):
            shown = self._service.decide_consent(actor=self._owner_user_id or "", lock_id=lock_id, decision=None, confirmed=False)
            if shown.outcome is ConsentOutcome.SHOWN and shown.config is not None:
                role = shown.config.role.value if shown.config.role else "unknown"
                views.append(LockConsentView(lock_id=lock_id, role=role, state=shown.consent.state, exposed=shown.exposed))
            else:
                views.append(LockConsentView(lock_id=lock_id, role="unknown", state=None, exposed=None))
        return views

    def decide(self, *, discord_user_id: str, lock_id: str, action: str) -> str:
        if not self.is_owner_discord(discord_user_id) or not self._owner_user_id:
            return "Only the keyholder can decide this."
        decision = _DECISIONS.get(action)
        if decision is None:
            return "Unknown choice; nothing was recorded."
        attest = self._ledger.owner_attested(lock_id)
        result = self._service.decide_consent(actor=self._owner_user_id, lock_id=lock_id, decision=decision, confirmed=True,
                                              attest_own_account=attest)
        logger.info("Community review consent for lock %s via Discord: %s -> %s.", lock_id, action, result.outcome.value)
        if result.outcome is ConsentOutcome.WEARER_IS_ANOTHER_ACCOUNT:
            return ("Another account wears this lock, so only that wearer can decide. If that account is your own, record it once on "
                    "the bot's machine with `python -m cli review-consent <lock> --owner-id <id> --accept --yes --wearer-is-my-account`.")
        if result.outcome is not ConsentOutcome.RECORDED:
            return "The lock could not be read right now, so nothing was recorded. Please try again shortly."
        text = {ConsentDecision.APPROVED: "Thank you: your consent to community review is recorded for this lock.",
                ConsentDecision.REJECTED: "Recorded: you declined community review for this lock.",
                ConsentDecision.REVOKED: "Recorded: you revoked community review for this lock."}[decision]
        if result.revert is not None:
            if result.revert.outcome is ExtensionReviewOutcome.APPLIED:
                text += " Chaster no longer asks for verification pictures or task photos on this lock."
            elif result.revert.outcome is ExtensionReviewOutcome.NOTHING_TO_REVERT:
                text += " Chaster was already not asking for photos on this lock."
            else:
                text += (" Switching the photo requirements off did NOT complete "
                         f"({result.revert.outcome.value}); please check the lock on Chaster.")
        elif decision is ConsentDecision.APPROVED and not result.exposed:
            text += " Community review itself is switched on by your keyholder separately."
        return text


_STATE_TEXT = {ConsentState.APPROVED: "you agreed", ConsentState.DECLINED: "you declined", ConsentState.REVOKED: "you revoked it",
               ConsentState.NONE: "covered by this notice"}


def render_overview(views: list[LockConsentView]) -> OutgoingMessage:
    """The notice, each locked lock's state and the buttons that make sense for it."""
    if not views:
        return OutgoingMessage(text="You have no locked lock right now, so there is nothing to decide about community review.")
    fields, actions = [], []
    for v in views:
        short = v.lock_id[:8]
        if v.state is None:
            fields.append(EmbedField(name=f"Lock {short}…", value="Could not be read right now."))
            continue
        exposure = "community review is ON" if v.exposed else "only the keyholder reviews"
        fields.append(EmbedField(name=f"Lock {short}… ({v.role})", value=f"Consent: {_STATE_TEXT[v.state]}; {exposure}."))
        if v.state in (ConsentState.DECLINED, ConsentState.REVOKED):         # only a refusal has anything to undo
            actions.append(ActionSpec(action_id=action_id("accept", v.lock_id), label=f"Allow again · {short}", style="success"))
        else:                                                                # nothing to accept: only the way out
            actions.append(ActionSpec(action_id=action_id("revoke", v.lock_id), label=f"Revoke · {short}", style="danger"))
    text = NOTICE_TEXT + "\n\n" + "\n".join(f"{f.name}: {f.value}" for f in fields)
    return OutgoingMessage(text=text, embed=EmbedSpec(title="Community photo review", description=NOTICE_TEXT, color="blue",
                                                      fields=tuple(fields)),
                           actions=tuple(actions))


def build_review_consent_dm(config, core, *, token_manager, lock_client, connections, owner_discord_id: str | None,
                            clock) -> ReviewConsentDm | None:
    """The real wiring (the bot calls only this). None when the account or the owner is not configured."""
    from chaster.extension_config import LockConfigurationReader
    from chaster.lock_directory import LockDirectory
    from chaster_execution.control import ExecutionControl
    from chaster_execution.transport import ChasterHttpTransport

    account = getattr(config, "chaster_execution_keyholder_user_id", None)
    owner = getattr(config, "owner_user_id", None)
    if not account or not owner or token_manager is None or connections is None:
        return None
    ledger = ReviewConsentLedger(config.db_path, core=core)
    service = ExtensionReviewService(
        config.db_path, core=core, control=ExecutionControl(config.db_path, core=core),
        reader=LockConfigurationReader(token_manager=token_manager, lock_client=lock_client),
        transport=ChasterHttpTransport(token_manager=token_manager, keyholder_user_id=account, clock=clock),
        owner_user_id=owner, account_user_id=account, clock=clock, consents=ledger)
    directory = LockDirectory(token_manager=token_manager, lock_client=lock_client, connections=connections)

    def locked_lock_ids(now: datetime) -> list[str]:
        listing = directory.list_account_locks(user_id=account, now=now)
        return list(dict.fromkeys(l.lock_id for l in listing.wearer_locks + listing.keyholder_locks if l.status == "locked"))

    return ReviewConsentDm(service, ledger, owner_user_id=owner, owner_discord_id=owner_discord_id, list_lock_ids=locked_lock_ids,
                           now=clock.now)
