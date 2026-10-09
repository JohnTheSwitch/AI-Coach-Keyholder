"""
application/models.py

Channel-agnostic data structures for the application layer. Nothing
here imports `discord` or any other channel-specific package -- an
adapter (bot/discord_bot.py today, something else later) is responsible
for translating its own message type into IncomingMessage and
OutgoingMessage.text back into whatever it sends.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, kw_only=True)
class IncomingMessage:
    """What an adapter hands to ApplicationService.handle_message() --
    everything the application layer needs, and nothing specific to
    Discord (or any other channel)."""
    channel: str            # 'discord' today; a plain string, not a channel-specific enum, deliberately extensible
    external_user_id: str    # the channel's own identifier for the sender (e.g. Discord user id, as a string)
    text: str
    received_at: datetime
    external_message_id: str | None = None
    """The channel's own stable identifier for THIS message (e.g.
    Discord's message.id, as a string) -- None for any caller that
    doesn't have one available. Added specifically so a mutable-state
    command (advanced_mode's two-stage transition, needing two
    independently-auditable consent references) can build one from the
    actual message that triggered it, without a general-purpose
    consent module or table -- see application/service.py's own
    `_consent_id_for()`."""


@dataclass(frozen=True, kw_only=True)
class EmbedField:
    """One name/value field within an EmbedSpec. Channel-agnostic --
    maps to whatever rich-field concept (if any) an adapter's own
    channel supports."""
    name: str
    value: str


@dataclass(frozen=True, kw_only=True)
class EmbedSpec:
    """A plain, channel-agnostic description of a rich/structured
    reply -- NOT a `discord.Embed` (this module imports no channel
    package, deliberately, per its own header docstring). `color` is
    one of a small fixed set of semantic names ("red", "blue",
    "green") rather than a raw hex code or a Discord-specific color
    object -- an adapter maps the name to whatever its own channel
    expects (bot/discord_bot.py maps these to `discord.Color.red()`
    etc.). A channel with no rich-message concept at all can simply
    ignore this and fall back to `OutgoingMessage.text`."""
    title: str
    description: str | None = None
    color: str = "blue"
    fields: tuple[EmbedField, ...] = ()


@dataclass(frozen=True, kw_only=True)
class OutgoingMessage:
    """What ApplicationService.handle_message() returns -- an adapter
    sends `.text` however its channel sends text. `embed`, when
    present, is an optional, richer, channel-agnostic alternative
    rendering an adapter MAY use instead of (or alongside) `.text` --
    `.text` is always populated too (never embed-only), so an adapter
    with no embed support can always fall back to it safely."""
    text: str
    embed: EmbedSpec | None = None


@dataclass(kw_only=True)
class UserAccount:
    """
    The application layer's own identity record -- NOT a domain-module
    concept. No table in trust_manager/penalty_engine/recovery_plan/
    goal_management references this id; those modules remain
    single-user and unscoped, exactly as they were before this layer
    existed. See application/README.md for why.
    """
    id: str = field(default_factory=new_id)
    created_at: datetime
    last_seen_at: datetime


class OnboardingStep(StrEnum):
    """
    docs/architecture/user_onboarding_technical_design.md's own state
    machine. Persisted (never held only in memory) so a bot restart
    resumes correctly from whatever a user's `user_preferences` row
    already says -- no separate "resume" logic exists, or needs to,
    because the next incoming message simply re-reads this same
    persisted state.
    """
    LANGUAGE = "language"
    AI_GENDER = "ai_gender"
    PERSONALITY = "personality"
    COMPLETE = "complete"


@dataclass(kw_only=True)
class UserPreferences:
    """
    One row per UserAccount (migration 013). `language`/`ai_gender`/
    `identity_id` are `None` until that step of onboarding is actually
    answered -- reaching `onboarding_step=COMPLETE` is what guarantees
    all three are set, not a NOT NULL constraint (see the migration's
    own comment for why validation lives in code, not the schema).

    Explicitly NOT read by anything communication-related yet --
    `identity_id` is a stored preference, not a wired input to any
    message-phrasing pipeline (ai_identity_technical_design.md's own
    communication layer remains unapproved for implementation).
    """
    user_id: str
    onboarding_step: OnboardingStep
    language: str | None = None
    ai_gender: str | None = None
    identity_id: str | None = None
    created_at: datetime
    updated_at: datetime

