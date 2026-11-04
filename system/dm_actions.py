"""
system/dm_actions.py

The doors behind the persistent DM buttons of the exemption and detector workflows (`bot/action_buttons.DmActionButton`). Each one resolves the Discord account that
pressed to the internal user FIRST, so a forwarded or shared card can never act for someone else. Free of Discord types, so tested directly.

A press is a person's explicit decision. No model reaches any of this: the language model sees neither these ids nor these methods.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from application.models import OutgoingMessage
from exemptions.service import ExemptionDecision, ExemptionService
from infrastructure.database import Database as CoreDatabase
from infractions.hygiene import HygieneWatch
from infractions.signals import Decision, InfractionSignals

logger = logging.getLogger("ai_coach_keyholder.system.dm_actions")

__all__ = ["ExemptionDm", "InfractionDm", "HygieneDm", "discord_user_resolver"]

_NOT_YOURS = OutgoingMessage(text="This isn't yours to answer.")


def discord_user_resolver(core: CoreDatabase) -> Callable[[str], str | None]:
    def resolve(discord_user_id: str) -> str | None:
        with core.transaction() as tx:
            row = tx.fetch_one("SELECT user_account_id FROM user_channel_identities WHERE channel = 'discord' AND external_id = ?", (discord_user_id,))
        return None if row is None else row["user_account_id"]

    return resolve


class ExemptionDm:
    def __init__(self, service: ExemptionService, *, resolve_user: Callable[[str], str | None], now: Callable[[], datetime]) -> None:
        self.service = service
        self._resolve = resolve_user
        self._now = now

    def press(self, *, discord_user_id: str, action: str, ident: str) -> OutgoingMessage:
        user = self._resolve(discord_user_id)
        if user is None:
            return _NOT_YOURS
        now = self._now()
        if action in ("approve", "decline"):
            outcome = self.service.decide(actor=user, exemption_id=ident, approve=action == "approve", now=now)
            return OutgoingMessage(text={ExemptionDecision.APPROVED: "Approved. They've been told.", ExemptionDecision.DECLINED: "Declined. They've been told.",
                                         ExemptionDecision.NOT_OWNER: "Only the keyholder can decide this.",
                                         ExemptionDecision.NOT_PENDING: "This request was already decided."}[outcome])
        if action == "end":
            return self.service.end_early(user, ident, now)
        if action.startswith("cat_"):
            return self.service.choose_category(user, ident, action[4:], now)
        if action.startswith("h") and action[1:].isdigit():
            return self.service.choose_hours(user, ident, int(action[1:]), now)
        return OutgoingMessage(text="That isn't one of the options.")


class InfractionDm:
    def __init__(self, signals: InfractionSignals, *, resolve_user: Callable[[str], str | None], now: Callable[[], datetime]) -> None:
        self._signals = signals
        self._resolve = resolve_user
        self._now = now

    def press(self, *, discord_user_id: str, action: str, ident: str) -> OutgoingMessage:
        user = self._resolve(discord_user_id)
        if user is None:
            return _NOT_YOURS
        if action == "health":
            cleared = self._signals.clear_as_health(user_id=user, signal_id=ident, now=self._now())
            return OutgoingMessage(text="Thank you for telling me. It doesn't count, and that's the end of it." if cleared
                                   else "Your keyholder has already decided this one, so please talk to them.")
        if action in ("confirm", "dismiss"):
            outcome = self._signals.decide(actor=user, signal_id=ident, confirm=action == "confirm", now=self._now())
            return OutgoingMessage(text={Decision.CONFIRMED: "Confirmed. The Penalty Engine decides what follows.", Decision.DISMISSED: "Dismissed. It never counts.",
                                         Decision.NOT_OWNER: "Only the keyholder can decide this.", Decision.NOT_OPEN: "This was already settled.",
                                         Decision.FAILED: "That didn't go through and nothing was recorded. Please try again."}[outcome])
        return OutgoingMessage(text="That isn't one of the options.")


class HygieneDm:
    def __init__(self, watch: HygieneWatch, *, resolve_user: Callable[[str], str | None], now: Callable[[], datetime]) -> None:
        self._watch = watch
        self._resolve = resolve_user
        self._now = now

    def press(self, *, discord_user_id: str, action: str, ident: str) -> OutgoingMessage:
        user = self._resolve(discord_user_id)
        if user is None or action not in ("planned", "health"):
            return _NOT_YOURS
        recorded = self._watch.classify(user_id=user, opening_id=ident, health=action == "health", now=self._now())
        if not recorded:
            return OutgoingMessage(text="Already noted, thank you.")
        return OutgoingMessage(text="Noted. Take care of yourself; this never counts against anything." if action == "health" else "Noted, thank you.")
