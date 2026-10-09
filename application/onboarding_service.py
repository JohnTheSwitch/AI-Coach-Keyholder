"""
application/onboarding_service.py

The onboarding state machine (docs/architecture/user_onboarding_technical_design.md).
Channel-agnostic -- called from ApplicationService.handle_message(),
knows nothing about Discord. Every prompt this module produces is
plain English text for this slice (see the design doc's own Section 5
for why -- no localization mechanism exists yet in this codebase to
route `language`-preference-driven prompt text through; building one
is explicitly out of this slice's scope).

State transitions are always: read current state -> validate the
incoming text against THAT state's own valid answers -> if valid, an
atomic conditional UPDATE (`WHERE onboarding_step = <the step this
code believes is current>`) -> if 0 rows changed, something else
already advanced this user past that step (a duplicate/stale message)
-- re-read the actual current state and respond to THAT, never blindly
overwrite. If the incoming text is not valid for the current step, no
write happens at all; the same step's prompt is re-shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ai.identity_catalog import IDENTITY_CATALOG, IdentityGroup, get_identity
from application.models import OnboardingStep, OutgoingMessage, UserPreferences
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

__all__ = ["OnboardingService"]

_LANGUAGE_CHOICES: dict[str, str] = {
    "1": "en", "english": "en", "en": "en",
}

_AI_GENDER_CHOICES: dict[str, IdentityGroup] = {
    "1": IdentityGroup.FEMALE, "female": IdentityGroup.FEMALE,
    "2": IdentityGroup.MALE, "male": IdentityGroup.MALE,
    "3": IdentityGroup.NEUTRAL, "neutral": IdentityGroup.NEUTRAL,
}


def _row_to_preferences(row) -> UserPreferences:
    return UserPreferences(
        user_id=row["user_id"], onboarding_step=OnboardingStep(row["onboarding_step"]),
        language=row["language"], ai_gender=row["ai_gender"], identity_id=row["identity_id"],
        created_at=_parse_iso(row["created_at"]), updated_at=_parse_iso(row["updated_at"]),
    )


@dataclass(frozen=True, kw_only=True)
class OnboardingResult:
    """What process_message() returns -- the preferences row as it now
    stands (whether or not this call actually changed it) and the
    message to send back."""
    preferences: UserPreferences
    reply: OutgoingMessage


class OnboardingService:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    # -------------------------------------------------------------------
    # Reading
    # -------------------------------------------------------------------

    def get_or_create_preferences(self, user_id: str, *, now: datetime) -> tuple[UserPreferences, bool]:
        """
        Mirrors UserService.get_or_create_user()'s own get-or-create
        shape. A user's first-ever message creates this row at
        onboarding_step=LANGUAGE -- there is no separate "start
        onboarding" action; sending any message at all starts it.

        Returns `(preferences, was_created)`. `was_created=True` means
        this user has never been prompted for anything yet -- the
        caller should show `prompt_for(preferences)` directly, never
        pass this message's own text into `process_message()` (it
        wasn't an answer to any question; treating it as one would
        produce a confusing "I didn't recognize that" as literally the
        bot's first-ever reply to a new user).
        """
        def write(tx: Transaction, _state: object) -> tuple[UserPreferences, bool]:
            row = tx.fetch_one("SELECT * FROM user_preferences WHERE user_id = ?", (user_id,))
            if row is not None:
                return _row_to_preferences(row), False
            tx.execute(
                "INSERT INTO user_preferences (user_id, onboarding_step, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, OnboardingStep.LANGUAGE.value, _iso(now), _iso(now)),
            )
            return UserPreferences(user_id=user_id, onboarding_step=OnboardingStep.LANGUAGE, created_at=now, updated_at=now), True

        return apply_transition(self._core, write=write)

    def is_complete(self, preferences: UserPreferences) -> bool:
        return preferences.onboarding_step == OnboardingStep.COMPLETE

    # -------------------------------------------------------------------
    # The state machine's single entry point
    # -------------------------------------------------------------------

    def process_message(self, preferences: UserPreferences, text: str, *, now: datetime) -> OnboardingResult:
        """Never raises for an invalid answer -- returns a re-prompt
        instead. Assumes `preferences.onboarding_step != COMPLETE`
        (ApplicationService only calls this for an incomplete user)."""
        step = preferences.onboarding_step
        normalized = text.strip().lower()

        if step == OnboardingStep.LANGUAGE:
            return self._handle_language(preferences, normalized, now=now)
        if step == OnboardingStep.AI_GENDER:
            return self._handle_ai_gender(preferences, normalized, now=now)
        if step == OnboardingStep.PERSONALITY:
            return self._handle_personality(preferences, normalized, now=now)
        # step == COMPLETE should never reach here (ApplicationService's
        # own responsibility to route completed users elsewhere) --
        # handled defensively rather than assumed, since a caller bug
        # here should never crash.
        return OnboardingResult(preferences=preferences, reply=OutgoingMessage(text=self._welcome_back_text(preferences)))

    # -------------------------------------------------------------------
    # Prompts (also used by ApplicationService for a brand-new user's
    # very first message, before any answer has been given at all)
    # -------------------------------------------------------------------

    def prompt_for(self, preferences: UserPreferences) -> OutgoingMessage:
        step = preferences.onboarding_step
        if step == OnboardingStep.LANGUAGE:
            return OutgoingMessage(text=self._language_prompt_text())
        if step == OnboardingStep.AI_GENDER:
            return OutgoingMessage(text=self._ai_gender_prompt_text())
        if step == OnboardingStep.PERSONALITY:
            return OutgoingMessage(text=self._personality_prompt_text(preferences))
        return OutgoingMessage(text=self._welcome_back_text(preferences))

    # -------------------------------------------------------------------
    # LANGUAGE
    # -------------------------------------------------------------------

    def _language_prompt_text(self) -> str:
        return (
            "Welcome! Let's get you set up — this only takes a moment.\n\n"
            "What language would you like to use?\n"
            "1) English"
        )

    def _handle_language(self, preferences: UserPreferences, normalized: str, *, now: datetime) -> OnboardingResult:
        choice = _LANGUAGE_CHOICES.get(normalized)
        if choice is None:
            return OnboardingResult(
                preferences=preferences,
                reply=OutgoingMessage(text="I didn't recognize that. " + self._language_prompt_text()),
            )
        updated = self._advance(
            preferences, from_step=OnboardingStep.LANGUAGE, to_step=OnboardingStep.AI_GENDER,
            column="language", value=choice, now=now,
        )
        return OnboardingResult(preferences=updated, reply=self.prompt_for(updated))

    # -------------------------------------------------------------------
    # AI_GENDER
    # -------------------------------------------------------------------

    def _ai_gender_prompt_text(self) -> str:
        return (
            "Which voice would you prefer for your AI?\n"
            "1) Female\n"
            "2) Male\n"
            "3) Neutral"
        )

    def _handle_ai_gender(self, preferences: UserPreferences, normalized: str, *, now: datetime) -> OnboardingResult:
        choice = _AI_GENDER_CHOICES.get(normalized)
        if choice is None:
            return OnboardingResult(
                preferences=preferences,
                reply=OutgoingMessage(text="I didn't recognize that. " + self._ai_gender_prompt_text()),
            )
        updated = self._advance(
            preferences, from_step=OnboardingStep.AI_GENDER, to_step=OnboardingStep.PERSONALITY,
            column="ai_gender", value=choice.value, now=now,
        )
        return OnboardingResult(preferences=updated, reply=self.prompt_for(updated))

    # -------------------------------------------------------------------
    # PERSONALITY
    # -------------------------------------------------------------------

    def _identities_for(self, preferences: UserPreferences) -> list:
        group = IdentityGroup(preferences.ai_gender) if preferences.ai_gender else None
        return [e for e in IDENTITY_CATALOG if group is None or e.group == group]

    def _personality_prompt_text(self, preferences: UserPreferences) -> str:
        language = preferences.language or "en"
        lines = ["Which personality would you like? Reply with a number or a name."]
        for i, entry in enumerate(self._identities_for(preferences), start=1):
            lines.append(f"{i}) {entry.display_name(language)} — {entry.archetype}")
        return "\n".join(lines)

    def _handle_personality(self, preferences: UserPreferences, normalized: str, *, now: datetime) -> OnboardingResult:
        candidates = self._identities_for(preferences)
        chosen = None
        if normalized.isdigit():
            index = int(normalized) - 1
            if 0 <= index < len(candidates):
                chosen = candidates[index]
        else:
            for entry in candidates:
                names = {entry.identity_id, entry.default_name.lower(), entry.display_name(preferences.language or "en").lower()}
                if normalized in names:
                    chosen = entry
                    break

        if chosen is None:
            return OnboardingResult(
                preferences=preferences,
                reply=OutgoingMessage(text="I didn't recognize that. " + self._personality_prompt_text(preferences)),
            )

        updated = self._advance(
            preferences, from_step=OnboardingStep.PERSONALITY, to_step=OnboardingStep.COMPLETE,
            column="identity_id", value=chosen.identity_id, now=now,
        )
        return OnboardingResult(preferences=updated, reply=OutgoingMessage(text=self._completion_text(updated)))

    # -------------------------------------------------------------------
    # Shared: the atomic, stale-safe transition
    # -------------------------------------------------------------------

    def _advance(
        self, preferences: UserPreferences, *, from_step: OnboardingStep, to_step: OnboardingStep,
        column: str, value: str, now: datetime,
    ) -> UserPreferences:
        """
        The DB write always happens BEFORE any message is sent back
        (ApplicationService/the adapter sends the reply only after this
        returns) -- if sending later fails, the persisted state is
        already correct and the user's next message simply sees the
        new current step, self-healing without any special recovery
        logic.

        Conditional on `onboarding_step = from_step` -- if 0 rows are
        affected, something else (a duplicate/redelivered message)
        already advanced this user past `from_step`; re-reads and
        returns the actual current row instead of trusting the
        `from_step`/`to_step` this call assumed, so a stale write is
        never silently forced through.
        """
        def write(tx: Transaction, _state: object) -> UserPreferences:
            cursor = tx.execute(
                f"UPDATE user_preferences SET {column} = ?, onboarding_step = ?, updated_at = ? "
                f"WHERE user_id = ? AND onboarding_step = ?",
                (value, to_step.value, _iso(now), preferences.user_id, from_step.value),
            )
            if cursor.rowcount == 0:
                row = tx.fetch_one("SELECT * FROM user_preferences WHERE user_id = ?", (preferences.user_id,))
                return _row_to_preferences(row)
            row = tx.fetch_one("SELECT * FROM user_preferences WHERE user_id = ?", (preferences.user_id,))
            return _row_to_preferences(row)

        return apply_transition(self._core, write=write)

    # -------------------------------------------------------------------
    # Completion / already-complete text
    # -------------------------------------------------------------------

    def _completion_text(self, preferences: UserPreferences) -> str:
        entry = get_identity(preferences.identity_id) if preferences.identity_id else None
        name = entry.display_name(preferences.language or "en") if entry else "your AI"
        return (
            f"All set! You're paired with {name}. Send `help` to see what I can do."
        )

    def _welcome_back_text(self, preferences: UserPreferences) -> str:
        return "You're all set up already. Send `help` to see what I can do."
