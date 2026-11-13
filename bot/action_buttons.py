"""
bot/action_buttons.py

Phase J1: turns channel-agnostic `ActionSpec`s (application/models.py) into Discord buttons, and the persistent handler for the
community review consent buttons (`review_consent:<accept|decline|revoke>:<lock id>`).

- `actions_view(actions)` builds a `discord.ui.View` (no timeout) whose buttons carry exactly the given custom ids. A click is routed by
  discord.py to the `DynamicItem` whose template matches that id, so these buttons keep working after a restart; an id without a
  registered template is simply not offered (dropped), never shown as a dead button.
- `ReviewConsentButton` hands the click to `system.review_consent_dm.ReviewConsentDm.decide()` (owner's Discord account only)
  and replaces the buttons with the outcome.
- `CoachButton` (Phase J3, `coach:<accept|morning|afternoon|evening|decline|kept|partly|missed|later>:<id>`) hands the click to `system.coach_dm.CoachDm.press()`, which resolves the
  presser and refuses anyone but the proposal's own user. Only the buttons of that one card are removed: a reply can carry two.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord

from application.models import ActionSpec
from peer_review.discord_buttons import CUSTOM_ID_TEMPLATE as PEER_REVIEW_TEMPLATE
from peer_review.discord_buttons import PeerReviewDecisionButton

logger = logging.getLogger("ai_coach_keyholder.bot.action_buttons")

__all__ = ["REVIEW_CONSENT_TEMPLATE", "COACH_TEMPLATE", "DM_ACTION_TEMPLATE", "ReviewConsentButton", "CoachButton", "DmActionButton", "actions_view",
           "REGISTERED_TEMPLATES", "DM_ACTIONS_ATTRIBUTE"]

REVIEW_CONSENT_TEMPLATE = r"review_consent:(?P<action>accept|decline|revoke):(?P<lock>[A-Za-z0-9_-]{1,64})"
COACH_TEMPLATE = r"coach:(?P<action>accept|morning|afternoon|evening|decline|kept|partly|missed|later):(?P<ident>[0-9a-f-]{36})"
DM_ACTION_TEMPLATE = r"(?P<scope>exemption|infraction|hygiene|lock_report):(?P<action>[a-z0-9_]{1,20}):(?P<ident>[0-9a-f-]{36})"
DM_ACTIONS_ATTRIBUTE = "dm_actions"              # the bot attribute: {scope: facade with press(discord_user_id=, action=, ident=) -> OutgoingMessage}
HANDLER_ATTRIBUTE = "review_consent_dm"          # the bot attribute holding the ReviewConsentDm facade
COACH_ATTRIBUTE = "coach_dm"                     # the bot attribute holding the CoachDm facade
_STYLES = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
           "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}


class ReviewConsentButton(discord.ui.DynamicItem[discord.ui.Button], template=REVIEW_CONSENT_TEMPLATE):
    def __init__(self, action: str, lock_id: str, *, label: str | None = None, style: str = "secondary") -> None:
        super().__init__(discord.ui.Button(label=label or action.capitalize(), style=_STYLES.get(style, discord.ButtonStyle.secondary),
                                           custom_id=f"review_consent:{action}:{lock_id}"))
        self.action = action
        self.lock_id = lock_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"], match["lock"], label=item.label, style="secondary")

    async def callback(self, interaction: discord.Interaction) -> None:
        facade = getattr(interaction.client, HANDLER_ATTRIBUTE, None)
        if facade is None:
            await interaction.response.send_message("Consent changes are not available right now; use the CLI on the bot's machine.",
                                                    ephemeral=True)
            return
        if not facade.is_owner_discord(str(interaction.user.id)):
            await interaction.response.send_message("Only the keyholder can decide this.", ephemeral=True)
            return
        await interaction.response.defer()                     # reading Chaster (and a revert write) can take longer than 3 s
        try:
            text = await asyncio.to_thread(facade.decide, discord_user_id=str(interaction.user.id), lock_id=self.lock_id,
                                           action=self.action)
        except Exception:
            logger.exception("The consent button failed for lock %s.", self.lock_id)
            text = "Something went wrong; nothing may have been recorded. Please check with the CLI."
        await interaction.followup.send(text)
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=None)        # one decision per prompt; ask again for a fresh overview
            except Exception:
                logger.warning("Could not remove the consent buttons from the message.")


class CoachButton(discord.ui.DynamicItem[discord.ui.Button], template=COACH_TEMPLATE):
    def __init__(self, action: str, ident: str, *, label: str | None = None, style: str = "secondary") -> None:
        super().__init__(discord.ui.Button(label=label or action.capitalize(), style=_STYLES.get(style, discord.ButtonStyle.secondary),
                                           custom_id=f"coach:{action}:{ident}"))
        self.action = action
        self.ident = ident

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"], match["ident"], label=item.label, style=item.style.name)

    async def callback(self, interaction: discord.Interaction) -> None:
        facade = getattr(interaction.client, COACH_ATTRIBUTE, None)
        if facade is None:
            await interaction.response.send_message("The coach is switched off right now.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            text = await asyncio.to_thread(facade.press, discord_user_id=str(interaction.user.id), action=self.action, ident=self.ident)
        except Exception:
            logger.exception("A coach button failed.")
            text = "Something went wrong; please try again."
        await interaction.followup.send(text)
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=actions_view(remaining_actions(interaction.message, self.ident)))
            except Exception:
                logger.warning("Could not update the coach buttons on the message.")


class DmActionButton(discord.ui.DynamicItem[discord.ui.Button], template=DM_ACTION_TEMPLATE):
    """One persistent button for the exemption, detector and lock report cards. The facade checks who pressed; the click is that person's explicit decision. The
    answer may carry the next step's buttons. The card's own buttons go away, so a decision cannot be pressed twice."""

    def __init__(self, scope: str, action: str, ident: str, *, label: str | None = None, style: str = "secondary") -> None:
        super().__init__(discord.ui.Button(label=label or action.capitalize(), style=_STYLES.get(style, discord.ButtonStyle.secondary),
                                           custom_id=f"{scope}:{action}:{ident}"))
        self.scope, self.action, self.ident = scope, action, ident

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["scope"], match["action"], match["ident"], label=item.label, style=item.style.name)

    async def callback(self, interaction: discord.Interaction) -> None:
        facade = (getattr(interaction.client, DM_ACTIONS_ATTRIBUTE, None) or {}).get(self.scope)
        if facade is None:
            await interaction.response.send_message("This isn't available right now.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            reply = await asyncio.to_thread(facade.press, discord_user_id=str(interaction.user.id), action=self.action, ident=self.ident)
        except Exception:
            logger.exception("A DM button failed (%s).", self.scope)
            await interaction.followup.send("Something went wrong; please try again.")
            return
        view = actions_view(reply.actions) if reply.actions else None
        await interaction.followup.send(reply.text, **({"view": view} if view is not None else {}))
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=None)
            except Exception:
                logger.warning("Could not remove the buttons from the message.")


def remaining_actions(message: discord.Message, ident: str) -> tuple[ActionSpec, ...]:
    """The message's buttons without the card that was just decided."""
    return tuple(ActionSpec(action_id=button.custom_id, label=button.label or "", style=button.style.name)
                 for row in message.components for button in row.children
                 if button.custom_id and not button.custom_id.endswith(ident))


# (compiled template, factory(match, spec) -> DynamicItem): every flow that offers buttons registers here.
REGISTERED_TEMPLATES = (
    (re.compile(REVIEW_CONSENT_TEMPLATE), lambda m, spec: ReviewConsentButton(m["action"], m["lock"], label=spec.label[:80], style=spec.style)),
    (re.compile(COACH_TEMPLATE), lambda m, spec: CoachButton(m["action"], m["ident"], label=spec.label[:80], style=spec.style)),
    (re.compile(DM_ACTION_TEMPLATE), lambda m, spec: DmActionButton(m["scope"], m["action"], m["ident"], label=spec.label[:80], style=spec.style)),
    (re.compile(PEER_REVIEW_TEMPLATE), lambda m, spec: PeerReviewDecisionButton(m["action"], m["incident"])),
)


def actions_view(actions: tuple[ActionSpec, ...] | list[ActionSpec]) -> discord.ui.View | None:
    """Persistent buttons for the given actions, built as the registered DynamicItem (never a plain button, which would also be
    dispatched as a dead per-message item). Actions without a registered template are dropped."""
    view = discord.ui.View(timeout=None)
    for spec in list(actions)[:25]:
        for pattern, factory in REGISTERED_TEMPLATES:
            match = pattern.fullmatch(spec.action_id)
            if match:
                view.add_item(factory(match, spec))
                break
        else:
            logger.warning("Dropping a button without a registered handler.")
    return view if view.children else None
