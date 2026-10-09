"""
tests/application/test_onboarding_service.py

Direct tests of the onboarding state machine
(application/onboarding_service.py), independent of the Discord
adapter -- see tests/bot/test_discord_bot.py for the end-to-end
Discord-facing scenarios (server messages ignored, bot's own messages
ignored, a send failure not corrupting state).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from application.models import OnboardingStep
from application.onboarding_service import OnboardingService
from infrastructure.database import Database as CoreDatabase

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _apply_migrations(core: CoreDatabase) -> None:
    migrations_dir = Path(__file__).parent.parent.parent / "database" / "migrations"
    with core.raw_connection() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    _apply_migrations(c)
    return c


@pytest.fixture
def service(core: CoreDatabase) -> OnboardingService:
    return OnboardingService(core.db_path, core=core)


def _seed_user(core: CoreDatabase, user_id: str, *, now: datetime = FIXED_TIME) -> None:
    with core.transaction() as tx:
        tx.execute(
            "INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES (?, ?, ?)",
            (user_id, now.isoformat(), now.isoformat()),
        )


class TestFirstContact:
    def test_a_brand_new_user_gets_created_at_the_language_step(self, service: OnboardingService, core: CoreDatabase) -> None:
        _seed_user(core, "u1")
        preferences, was_created = service.get_or_create_preferences("u1", now=FIXED_TIME)
        assert was_created is True
        assert preferences.onboarding_step == OnboardingStep.LANGUAGE
        assert preferences.language is None

    def test_a_second_call_for_the_same_user_does_not_recreate_the_row(self, service: OnboardingService, core: CoreDatabase) -> None:
        _seed_user(core, "u1")
        service.get_or_create_preferences("u1", now=FIXED_TIME)
        _preferences, was_created = service.get_or_create_preferences("u1", now=FIXED_TIME + timedelta(minutes=1))
        assert was_created is False
        with core.transaction() as tx:
            count = tx.fetch_one("SELECT COUNT(*) as n FROM user_preferences WHERE user_id = 'u1'")["n"]
        assert count == 1


class TestStepProgression:
    def test_completing_all_three_steps_in_order(self, service: OnboardingService, core: CoreDatabase) -> None:
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)

        result = service.process_message(preferences, "english", now=FIXED_TIME)
        assert result.preferences.onboarding_step == OnboardingStep.AI_GENDER
        assert result.preferences.language == "en"

        result = service.process_message(result.preferences, "female", now=FIXED_TIME)
        assert result.preferences.onboarding_step == OnboardingStep.PERSONALITY
        assert result.preferences.ai_gender == "female"

        result = service.process_message(result.preferences, "sophia", now=FIXED_TIME)
        assert result.preferences.onboarding_step == OnboardingStep.COMPLETE
        assert result.preferences.identity_id == "sophia"
        assert "sophia" in result.reply.text.lower() or "all set" in result.reply.text.lower()

    def test_personality_can_be_chosen_by_number(self, service: OnboardingService, core: CoreDatabase) -> None:
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)
        preferences = service.process_message(preferences, "en", now=FIXED_TIME).preferences
        preferences = service.process_message(preferences, "female", now=FIXED_TIME).preferences
        result = service.process_message(preferences, "1", now=FIXED_TIME)
        assert result.preferences.identity_id == "sophia"  # first Female entry in the catalog

    def test_localized_name_is_still_accepted_for_a_legacy_czech_preference(self, service: OnboardingService, core: CoreDatabase) -> None:
        # Czech is no longer offered during onboarding, but accounts that
        # stored "cs" earlier keep working: their localized name is accepted.
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)
        preferences = service._advance(
            preferences, from_step=OnboardingStep.LANGUAGE, to_step=OnboardingStep.AI_GENDER,
            column="language", value="cs", now=FIXED_TIME,
        )
        preferences = service.process_message(preferences, "female", now=FIXED_TIME).preferences
        result = service.process_message(preferences, "Sofie", now=FIXED_TIME)
        assert result.preferences.identity_id == "sophia"
        assert "Sofie" in result.reply.text  # localized name used in the confirmation, not "Sophia"


class TestOnlyEnglishIsOffered:
    def test_the_language_prompt_offers_english_only_and_czech_is_not_recognised(self, service: OnboardingService, core: CoreDatabase) -> None:
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)
        assert "English" in service.prompt_for(preferences).text and "2)" not in service.prompt_for(preferences).text
        result = service.process_message(preferences, "cs", now=FIXED_TIME)
        assert result.preferences.onboarding_step == OnboardingStep.LANGUAGE and result.preferences.language is None


class TestInvalidAnswers:
    def test_an_invalid_language_choice_does_not_advance_and_is_not_written(self, service: OnboardingService, core: CoreDatabase) -> None:
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)
        result = service.process_message(preferences, "banana", now=FIXED_TIME)
        assert result.preferences.onboarding_step == OnboardingStep.LANGUAGE
        assert result.preferences.language is None
        assert "didn't recognize" in result.reply.text.lower()

    def test_an_invalid_personality_index_out_of_range_is_rejected(self, service: OnboardingService, core: CoreDatabase) -> None:
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)
        preferences = service.process_message(preferences, "en", now=FIXED_TIME).preferences
        preferences = service.process_message(preferences, "female", now=FIXED_TIME).preferences
        result = service.process_message(preferences, "999", now=FIXED_TIME)
        assert result.preferences.onboarding_step == OnboardingStep.PERSONALITY
        assert result.preferences.identity_id is None

    def test_a_personality_from_the_wrong_gender_group_is_rejected(self, service: OnboardingService, core: CoreDatabase) -> None:
        """'marcus' is Male -- must not be selectable while the
        AI_GENDER answer was 'female'."""
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)
        preferences = service.process_message(preferences, "en", now=FIXED_TIME).preferences
        preferences = service.process_message(preferences, "female", now=FIXED_TIME).preferences
        result = service.process_message(preferences, "marcus", now=FIXED_TIME)
        assert result.preferences.identity_id is None


class TestResumeAfterRestart:
    def test_a_fresh_service_instance_resumes_from_the_persisted_step(self, core: CoreDatabase) -> None:
        """Simulates a process restart -- a brand-new OnboardingService
        (no in-memory state at all) reading the same DB must see
        exactly where a prior instance left off."""
        service_a = OnboardingService(core.db_path, core=core)
        _seed_user(core, "u1")
        preferences, _ = service_a.get_or_create_preferences("u1", now=FIXED_TIME)
        service_a.process_message(preferences, "en", now=FIXED_TIME)

        service_b = OnboardingService(core.db_path, core=core)  # fresh instance, "after restart"
        resumed, was_created = service_b.get_or_create_preferences("u1", now=FIXED_TIME + timedelta(hours=1))
        assert was_created is False
        assert resumed.onboarding_step == OnboardingStep.AI_GENDER
        assert resumed.language == "en"


class TestDuplicateMessages:
    def test_the_same_valid_answer_sent_twice_only_advances_once(self, service: OnboardingService, core: CoreDatabase) -> None:
        """Simulates a duplicated Discord dispatch: process_message()
        called twice with the identical (stale, already-answered)
        text. The second call must not silently reprocess it as if it
        were an answer to the LANGUAGE step again -- by the time it
        runs, the current step is AI_GENDER, so 'english' is simply
        not a valid AI_GENDER answer, and the row is left untouched."""
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)

        first = service.process_message(preferences, "english", now=FIXED_TIME)
        assert first.preferences.onboarding_step == OnboardingStep.AI_GENDER

        second = service.process_message(preferences, "english", now=FIXED_TIME)  # stale `preferences` snapshot, same text
        assert second.preferences.onboarding_step == OnboardingStep.AI_GENDER  # unchanged, not reset to LANGUAGE
        assert second.preferences.language == "en"  # first answer preserved

    def test_advance_is_a_no_op_when_the_row_already_moved_past_from_step(self, service: OnboardingService, core: CoreDatabase) -> None:
        """Directly exercises the atomic conditional UPDATE's stale
        path: manually advance the row past LANGUAGE out-of-band, then
        call _advance() with a stale `from_step=LANGUAGE` snapshot --
        it must return the row's REAL current state, not force the
        write through."""
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)
        with core.transaction() as tx:
            tx.execute(
                "UPDATE user_preferences SET onboarding_step = ?, language = ? WHERE user_id = 'u1'",
                (OnboardingStep.AI_GENDER.value, "en"),
            )

        result = service._advance(
            preferences, from_step=OnboardingStep.LANGUAGE, to_step=OnboardingStep.AI_GENDER,
            column="language", value="cs", now=FIXED_TIME,
        )
        assert result.language == "en"  # the out-of-band value, not "cs" -- the stale write never applied

    def test_two_truly_concurrent_advances_never_double_advance_or_corrupt_state(self, core: CoreDatabase) -> None:
        """The sequential test above proves the conditional UPDATE's
        logic; this proves it under REAL concurrency, not just Python
        code executing one line after another. Two separate threads,
        each with their OWN OnboardingService/sqlite3 connection
        (`infrastructure/database.py`'s `.transaction()` opens a fresh
        connection every call -- confirmed directly before writing this
        test, not assumed), both racing to advance the SAME user from
        LANGUAGE with a `threading.Barrier` to maximize the chance they
        genuinely overlap. SQLite's own file-level locking (this
        project's own `busy_timeout` config makes a second writer WAIT
        rather than immediately error) is what actually has to get this
        right -- this test exists to prove it does, not to assume it."""
        import threading

        _seed_user(core, "u1")
        service_main = OnboardingService(core.db_path, core=core)
        preferences, _ = service_main.get_or_create_preferences("u1", now=FIXED_TIME)
        assert preferences.onboarding_step == OnboardingStep.LANGUAGE

        barrier = threading.Barrier(2)
        results: list = []
        errors: list = []

        def race(value: str) -> None:
            try:
                # Each thread gets its OWN OnboardingService instance
                # (and therefore its own sqlite3 connections via
                # infrastructure/database.py's per-call .transaction())
                # -- a fair test of cross-connection locking, not
                # Python-level thread interleaving on a shared object.
                thread_service = OnboardingService(core.db_path, core=CoreDatabase(core.db_path))
                barrier.wait(timeout=5)  # both threads reach the UPDATE at roughly the same instant
                result = thread_service._advance(
                    preferences, from_step=OnboardingStep.LANGUAGE, to_step=OnboardingStep.AI_GENDER,
                    column="language", value=value, now=FIXED_TIME,
                )
                results.append(result)
            except Exception as exc:  # pragma: no cover -- failure path only
                errors.append(exc)

        t1 = threading.Thread(target=race, args=("en",))
        t2 = threading.Thread(target=race, args=("cs",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Unexpected exception(s) during concurrent advance: {errors}"
        assert len(results) == 2  # both calls returned (SQLite serialized them; neither crashed nor hung)

        with core.transaction() as tx:
            final_row = tx.fetch_one("SELECT * FROM user_preferences WHERE user_id = 'u1'")
        # Exactly one step forward -- never AI_GENDER->PERSONALITY (would mean a double-advance),
        # never back to LANGUAGE (would mean corruption), and the winning
        # thread's language value is one of the two attempted, never a
        # blend or garbage value.
        assert final_row["onboarding_step"] == "ai_gender"
        assert final_row["language"] in ("en", "cs")
        # Both threads' _advance() calls return a PREFERENCES REFLECTING
        # THE SAME FINAL ROW -- the "loser" thread's 0-rows-affected
        # branch re-reads and returns the winner's actual value, it
        # never silently reports its own (never-applied) value as if
        # it had won.
        assert results[0].language == results[1].language == final_row["language"]


class TestAlreadyOnboarded:
    def test_is_complete_reports_true_once_all_three_steps_are_done(self, service: OnboardingService, core: CoreDatabase) -> None:
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)
        preferences = service.process_message(preferences, "en", now=FIXED_TIME).preferences
        preferences = service.process_message(preferences, "male", now=FIXED_TIME).preferences
        preferences = service.process_message(preferences, "marcus", now=FIXED_TIME).preferences
        assert service.is_complete(preferences) is True

    def test_process_message_on_an_already_complete_user_does_not_crash(self, service: OnboardingService, core: CoreDatabase) -> None:
        """Defensive path -- ApplicationService is responsible for never
        calling this for a completed user, but this must not corrupt
        anything if it somehow does."""
        _seed_user(core, "u1")
        preferences, _ = service.get_or_create_preferences("u1", now=FIXED_TIME)
        preferences = service.process_message(preferences, "en", now=FIXED_TIME).preferences
        preferences = service.process_message(preferences, "male", now=FIXED_TIME).preferences
        preferences = service.process_message(preferences, "marcus", now=FIXED_TIME).preferences

        result = service.process_message(preferences, "anything else", now=FIXED_TIME)
        assert result.preferences.onboarding_step == OnboardingStep.COMPLETE
        assert result.preferences.identity_id == "marcus"  # unchanged
