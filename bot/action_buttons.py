"""
bot/action_buttons.py

Phase J1: turns channel-agnostic `ActionSpec`s (application/models.py) into Discord buttons, and the persistent handler for the
community review consent buttons (`review_consent:<accept|decline|revoke>:<lock id>`).

- `actions_view(actions)` builds a `discord.ui.View` (no timeout) whose buttons carry exactly the given custom ids. A click is routed by
  discord.py to the `DynamicItem` whose template matches that id, so these buttons keep working after a restart; an id without a
  registered template is simply not offered (dropped), never shown as a dead button.
- `ReviewConsentButton` hands the click to `system.review_consent_dm.ReviewConsentDm.decide()` (owner's Discord account only)
  and replaces the buttons with the outcome.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord

from application.models import ActionSpec

logger = logging.getLogger("ai_coach_keyholder.bot.action_buttons")

__all__ = ["REVIEW_CONSENT_TEMPLATE", "ReviewConsentButton", "actions_view", "REGISTERED_TEMPLATES"]

REVIEW_CONSENT_TEMPLATE = r"review_consent:(?P<action>accept|decline|revoke):(?P<lock>[A-Za-z0-9_-]{1,64})"
HANDLER_ATTRIBUTE = "review_consent_dm"          # the bot attribute holding the ReviewConsentDm facade
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


# (compiled template, factory(match, spec) -> DynamicItem): every flow that offers buttons registers here.
REGISTERED_TEMPLATES = (
    (re.compile(REVIEW_CONSENT_TEMPLATE), lambda m, spec: ReviewConsentButton(m["action"], m["lock"], label=spec.label[:80], style=spec.style)),
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
