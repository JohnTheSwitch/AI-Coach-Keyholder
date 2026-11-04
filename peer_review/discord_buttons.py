"""
peer_review/discord_buttons.py

One-click keyholder decisions in Discord (v0.39 Phase I4): the owner's DM about an UNCONFIRMED community review Incident carries two
buttons, `Confirm Penalty` and `Dismiss Incident`. They call the same `PeerReviewDecisions.decide()` as the CLI, which stays the fallback.

- The buttons are PERSISTENT (`discord.ui.DynamicItem`, custom id `peer_review:<confirm|dismiss>:<incident id>`, registered with
  `client.add_dynamic_items`), so they keep working after a bot restart.
- Only the owner may decide: the clicking Discord account must equal the owner's verified Discord id (constant-time compare). Anyone else
  gets an ephemeral refusal and nothing changes.
- A click IS the confirmation (the button is the explicit, specific act; the CLI needs `--yes` for the same reason). After a decision the
  buttons are removed from the message and the outcome is appended, so a second click is impossible; a stale button from an older copy of
  the message answers "already decided".
- Confirming makes the Incident CONFIRMED (source keyholder_review); from then on it counts like any confirmed Incident. Dismissing leaves
  it UNCONFIRMED for good. Nothing is sent to Chaster.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from datetime import datetime
from typing import Callable

import discord

from application.models import ActionSpec
from peer_review.watch import DecisionOutcome, PeerReviewDecisions

logger = logging.getLogger("ai_coach_keyholder.peer_review.discord_buttons")

__all__ = ["CUSTOM_ID_TEMPLATE", "PeerReviewButtonHandler", "PeerReviewDecisionButton", "decision_view", "decision_actions", "custom_id"]

CUSTOM_ID_TEMPLATE = r"peer_review:(?P<action>confirm|dismiss):(?P<incident>[0-9a-f-]{36})"
_INCIDENT_ID = re.compile(r"^[0-9a-f-]{36}$")
HANDLER_ATTRIBUTE = "peer_review_buttons"            # the bot attribute that holds the PeerReviewButtonHandler

_REPLY = {
    DecisionOutcome.CONFIRMED: "Confirmed by you: the incident is now CONFIRMED and counts like any confirmed incident.",
    DecisionOutcome.DISMISSED: "Dismissed by you: the incident stays unconfirmed and never counts.",
    DecisionOutcome.ALREADY_DECIDED: "This incident was already decided.",
    DecisionOutcome.NOT_FOUND: "This incident is not known (any more).",
    DecisionOutcome.FAILED: "The incident could not be confirmed; nothing was recorded. Try again or use the CLI.",
    DecisionOutcome.OWNER_NOT_CONFIGURED: "Decisions are unavailable: OWNER_USER_ID is not configured.",
    DecisionOutcome.NOT_OWNER: "Only the keyholder can decide this.",
    DecisionOutcome.NOT_CONFIRMED: "Not recorded.",
}
_FINAL = (DecisionOutcome.CONFIRMED, DecisionOutcome.DISMISSED, DecisionOutcome.ALREADY_DECIDED)


def custom_id(action: str, incident_id: str) -> str:
    return f"peer_review:{action}:{incident_id}"


class PeerReviewButtonHandler:
    """The decision logic behind the buttons, free of Discord types (tested directly)."""

    def __init__(self, decisions: PeerReviewDecisions, *, owner_user_id: str | None, owner_discord_id: str | None,
                 now: Callable[[], datetime]) -> None:
        self._decisions = decisions
        self._owner_user_id = owner_user_id
        self._owner_discord_id = owner_discord_id
        self._now = now

    def is_owner(self, discord_user_id: str) -> bool:
        return bool(self._owner_discord_id) and isinstance(discord_user_id, str) and hmac.compare_digest(
            discord_user_id.encode("utf-8"), self._owner_discord_id.encode("utf-8"))

    def decide(self, *, discord_user_id: str, incident_id: str, confirm: bool) -> DecisionOutcome:
        if not self.is_owner(discord_user_id) or not self._owner_user_id:
            return DecisionOutcome.NOT_OWNER
        if not isinstance(incident_id, str) or not _INCIDENT_ID.match(incident_id):
            return DecisionOutcome.NOT_FOUND
        outcome = self._decisions.decide(actor=self._owner_user_id, incident_id=incident_id, confirm=confirm, confirmed=True,
                                         now=self._now())
        logger.info("Peer review incident %s: button %s -> %s.", incident_id, "confirm" if confirm else "dismiss", outcome.value)
        return outcome

    @staticmethod
    def reply(outcome: DecisionOutcome) -> str:
        return _REPLY[outcome]


class PeerReviewDecisionButton(discord.ui.DynamicItem[discord.ui.Button], template=CUSTOM_ID_TEMPLATE):
    def __init__(self, action: str, incident_id: str) -> None:
        confirm = action == "confirm"
        super().__init__(discord.ui.Button(
            label="Confirm Penalty" if confirm else "Dismiss Incident",
            style=discord.ButtonStyle.danger if confirm else discord.ButtonStyle.secondary,
            custom_id=custom_id(action, incident_id)))
        self.action = action
        self.incident_id = incident_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"], match["incident"])

    async def callback(self, interaction: discord.Interaction) -> None:
        handler: PeerReviewButtonHandler | None = getattr(interaction.client, HANDLER_ATTRIBUTE, None)
        if handler is None:
            await interaction.response.send_message("Decisions are not available right now; use the CLI.", ephemeral=True)
            return
        try:
            outcome = await asyncio.to_thread(handler.decide, discord_user_id=str(interaction.user.id), incident_id=self.incident_id,
                                              confirm=self.action == "confirm")
        except Exception:
            logger.exception("The peer review button failed for incident %s.", self.incident_id)
            outcome = DecisionOutcome.FAILED
        text = handler.reply(outcome)
        if outcome in _FINAL and interaction.message is not None:
            await interaction.response.edit_message(content=f"{interaction.message.content}\n\n**{text}**", view=None)
        else:
            await interaction.response.send_message(text, ephemeral=True)


def decision_actions(incident_id: str) -> tuple[ActionSpec, ...]:
    """The same two buttons as neutral specs, for a card that waits in the notification queue."""
    return (ActionSpec(action_id=custom_id("confirm", incident_id), label="Confirm Penalty", style="danger"),
            ActionSpec(action_id=custom_id("dismiss", incident_id), label="Dismiss Incident"))


def decision_view(incident_id: str) -> discord.ui.View:
    """The two buttons for one Incident (no timeout: they are persistent through the dynamic item)."""
    view = discord.ui.View(timeout=None)
    view.add_item(PeerReviewDecisionButton("confirm", incident_id))
    view.add_item(PeerReviewDecisionButton("dismiss", incident_id))
    return view
