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
from infractions.signals import InfractionSignals

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
        if action == "anyway":
            return self.service.take_anyway(user, ident, now)
        if action in ("hopen", "hask", "hhealth", "hcancel"):
            return self.service.press_hygiene(user, ident, action, now)
        if action.startswith("cat_"):
            return self.service.choose_category(user, ident, action[4:], now)
        if action.startswith("h") and action[1:].isdigit():
            return self.service.choose_hours(user, ident, int(action[1:]), now)
        return OutgoingMessage(text="That isn't one of the options.")


class InfractionDm:
    """The wearer's buttons on a detector's question: Explain, Appeal, and "it was a health or safety need". Nobody else decides (owner, 2026-11-10)."""

    def __init__(self, signals: InfractionSignals, *, resolve_user: Callable[[str], str | None], now: Callable[[], datetime]) -> None:
        self._signals = signals
        self._resolve = resolve_user
        self._now = now

    def press(self, *, discord_user_id: str, action: str, ident: str) -> OutgoingMessage:
        user = self._resolve(discord_user_id)
        if user is None:
            return _NOT_YOURS
        if action in ("health", "explain", "appeal", "own", "disown"):
            return self._signals.press(user_id=user, signal_id=ident, action=action, now=self._now())
        if action in ("confirm", "dismiss"):
            return OutgoingMessage(text="Nobody decides this by button any more: I ask, judge the answer and decide myself.")
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


class ProofDm:
    """The wearer's two buttons on a spot check (owner, 2026-11-13): "I can't show it now" and "I didn't actually do it"."""

    def __init__(self, checks, *, resolve_user: Callable[[str], str | None], now: Callable[[], datetime]) -> None:
        self._checks = checks
        self._resolve = resolve_user
        self._now = now

    def press(self, *, discord_user_id: str, action: str, ident: str) -> OutgoingMessage:
        user = self._resolve(discord_user_id)
        if user is None:
            return _NOT_YOURS
        if action not in ("pass", "retract"):
            return OutgoingMessage(text="That isn't one of the options.")
        return OutgoingMessage(text=self._checks.press(user_id=user, check_id=ident, action=action, now=self._now()))
