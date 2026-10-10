"""
tests/database/test_database.py

Repository-level tests for database/database.py — confirms Phase 0
functionality still works after Phase 1.2's refactor onto
infrastructure.database.Database, and confirms the new atomic,
multi-table, Clock-injected behavior Phase 1.2 adds.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from database.database import Database
from database.models import (
    ApprovalStatus,
    CoachAssessment,
    ConsentAction,
    ConsentRecord,
    ConsentTargetType,
    ContextSnapshot,
    ConversationMessage,
    CreatedBy,
    DecisionResult,
    ImpactScore,
    KeyholderAssessment,
    MessageRole,
    ObservationRecord,
    ObservationType,
    ResolutionMethod,
    RewardState,
    RiskDirection,
    Rule,
    TrustState,
    new_id,
)

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db", backup_dir=tmp_path / "backups")
    database.migrate(now=FIXED_TIME)
    return database


class TestMigrate:
    def test_applies_the_initial_schema(self, db: Database) -> None:
        # An entity round trip is the real proof the schema exists and is usable.
        snap = ContextSnapshot(created_at=FIXED_TIME)
        snapshot_id = db.save_context_snapshot(snap)
        assert db.get_context_snapshot(snapshot_id) is not None

    def test_second_call_applies_nothing_new(self, tmp_path: Path) -> None:
        database = Database(tmp_path / "test.db", backup_dir=tmp_path / "backups")
        first = database.migrate(now=FIXED_TIME)
        second = database.migrate(now=FIXED_TIME)
        assert first == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30]  # 001..030
        assert second == []


class TestContextSnapshotRoundTrip:
    def test_save_and_get(self, db: Database) -> None:
        snap = ContextSnapshot(
            created_at=FIXED_TIME,
            overall_confidence=0.8,
            data_freshness_hours=2.5,
        )
        snapshot_id = db.save_context_snapshot(snap)
        loaded = db.get_context_snapshot(snapshot_id)
        assert loaded is not None
        assert loaded.id == snap.id
        assert loaded.created_at == FIXED_TIME
        assert loaded.overall_confidence == 0.8

    def test_get_missing_returns_none(self, db: Database) -> None:
        assert db.get_context_snapshot("does-not-exist") is None


class TestCoachAndKeyholderAssessmentRoundTrip:
    def test_coach_assessment_round_trip(self, db: Database) -> None:
        snapshot_id = db.save_context_snapshot(ContextSnapshot(created_at=FIXED_TIME))
        a = CoachAssessment(
            created_at=FIXED_TIME,
            context_snapshot_id=snapshot_id,
            recommendation="rest more",
            reasoning="elevated fatigue signals",
            confidence=0.7,
            risk_direction=RiskDirection.OVERLOAD,
        )
        assessment_id = db.save_coach_assessment(a)
        loaded = db.get_coach_assessment(assessment_id)
        assert loaded is not None
        assert loaded.risk_direction == RiskDirection.OVERLOAD

    def test_keyholder_assessment_round_trip(self, db: Database) -> None:
        snapshot_id = db.save_context_snapshot(ContextSnapshot(created_at=FIXED_TIME))
        a = KeyholderAssessment(
            created_at=FIXED_TIME,
            context_snapshot_id=snapshot_id,
            trust_state=TrustState(trust_score=0.6),
            reward_state=RewardState(eligible_rewards=["extra_token"]),
        )
        assessment_id = db.save_keyholder_assessment(a)
        loaded = db.get_keyholder_assessment(assessment_id)
        assert loaded is not None
        assert loaded.trust_state.trust_score == 0.6
        assert loaded.reward_state.eligible_rewards == ["extra_token"]


class TestDecisionResultRoundTrip:
    @staticmethod
    def _valid_fk_ids(db: Database) -> dict[str, str]:
        """DecisionResult has NOT NULL foreign keys to context_snapshots,
        coach_assessments, and keyholder_assessments -- a pre-existing
        Phase 0 constraint this new test suite is the first thing to
        actually exercise. Builds the minimum valid chain of referenced
        rows."""
        snapshot_id = db.save_context_snapshot(ContextSnapshot(created_at=FIXED_TIME))
        coach_id = db.save_coach_assessment(
            CoachAssessment(created_at=FIXED_TIME, context_snapshot_id=snapshot_id)
        )
        keyholder_id = db.save_keyholder_assessment(
            KeyholderAssessment(created_at=FIXED_TIME, context_snapshot_id=snapshot_id)
        )
        return {
            "context_snapshot_id": snapshot_id,
            "coach_assessment_id": coach_id,
            "keyholder_assessment_id": keyholder_id,
        }

    def test_save_get_and_update_approval_status(self, db: Database) -> None:
        fk = self._valid_fk_ids(db)
        d = DecisionResult(
            created_at=FIXED_TIME,
            **fk,
            final_decision="proceed",
            resolution_method=ResolutionMethod.RULE_BASED,
            impact_score=ImpactScore(value=0.3, is_significant=False),
            approval_status=ApprovalStatus.PENDING,
        )
        decision_id = db.save_decision_result(d)

        db.set_decision_approval_status(decision_id, ApprovalStatus.APPROVED)
        loaded = db.get_decision_result(decision_id)
        assert loaded is not None
        assert loaded.approval_status == ApprovalStatus.APPROVED

    def test_get_pending_approvals(self, db: Database) -> None:
        fk = self._valid_fk_ids(db)
        pending = DecisionResult(created_at=FIXED_TIME, **fk, approval_status=ApprovalStatus.PENDING)
        approved = DecisionResult(created_at=FIXED_TIME, **fk, approval_status=ApprovalStatus.APPROVED)
        db.save_decision_result(pending)
        db.save_decision_result(approved)

        results = db.get_pending_approvals()
        assert len(results) == 1
        assert results[0].id == pending.id


class TestObservationsAndClockInjection:
    def test_save_and_mark_reviewed_with_explicit_now(self, db: Database) -> None:
        """Confirms Clock injection into a time-dependent DB operation
        (requirement 8): mark_observation_reviewed takes `now` explicitly."""
        obs = ObservationRecord(
            created_at=FIXED_TIME,
            observation_type=ObservationType.UNEXPECTED_OUTCOME,
            description="something unexpected",
        )
        obs_id = db.save_observation(obs)

        reviewed_at = FIXED_TIME + timedelta(days=1)
        db.mark_observation_reviewed(obs_id, now=reviewed_at, notes="looked into it")

        unreviewed = db.get_unreviewed_observations()
        assert all(o.id != obs_id for o in unreviewed)


class TestTrustHistoryClockInjection:
    def test_record_trust_history_uses_the_given_now(self, db: Database) -> None:
        """Requirement 8, second call site: record_trust_history takes
        `now` explicitly rather than calling a global clock."""
        record_id = db.record_trust_history(trust_score=0.65, reason="consistent behavior", now=FIXED_TIME)
        assert record_id is not None


class TestRuleVersioningAndAtomicMultiTableWrite:
    def test_save_rule(self, db: Database) -> None:
        rule = Rule(created_at=FIXED_TIME, title="No skipping check-ins", created_by=CreatedBy.USER)
        db.save_rule(rule)
        active = db.get_active_rules()
        assert any(r.id == rule.id for r in active)

    def test_supersede_rule_deactivates_previous_version_atomically(self, db: Database) -> None:
        v1 = Rule(created_at=FIXED_TIME, title="3 workouts/week", version=1, created_by=CreatedBy.USER)
        db.save_rule(v1)

        v2 = Rule(
            created_at=FIXED_TIME + timedelta(days=1),
            rule_group_id=v1.rule_group_id,
            title="2 workouts/week",
            version=2,
            supersedes_id=v1.id,
            created_by=CreatedBy.AI_PROPOSAL,
        )
        db.supersede_rule(v2)

        history = db.get_rule_history(v1.rule_group_id)
        assert len(history) == 2
        by_id = {r.id: r for r in history}
        assert by_id[v1.id].is_active is False
        assert by_id[v2.id].is_active is True

    def test_record_rule_change_with_consent_writes_both_tables_atomically(self, db: Database) -> None:
        """
        The genuine cross-table apply_transition() example
        (implementation_conventions.md Section 3/4): a Rule version and
        its ConsentRecord are written together or not at all.
        """
        rule = Rule(created_at=FIXED_TIME, title="Sleep by 23:00", created_by=CreatedBy.USER)
        consent = ConsentRecord(
            created_at=FIXED_TIME,
            target_type=ConsentTargetType.RULE,
            target_id=rule.rule_group_id,
            action=ConsentAction.APPROVED,
        )

        rule_id, consent_id = db.record_rule_change_with_consent(rule, consent)

        assert any(r.id == rule_id for r in db.get_active_rules())
        history = db.get_consent_history(ConsentTargetType.RULE, rule.rule_group_id)
        assert any(c.id == consent_id for c in history)

    def test_record_rule_change_with_consent_writes_a_domain_event(self, db: Database) -> None:
        """Phase 1.4: confirms the events= slot actually fires and writes
        a real row to the shared outbox, not just in isolated infra tests."""
        rule = Rule(created_at=FIXED_TIME, title="Sleep by 23:00", created_by=CreatedBy.USER)
        consent = ConsentRecord(
            created_at=FIXED_TIME,
            target_type=ConsentTargetType.RULE,
            action=ConsentAction.APPROVED,
        )

        rule_id, consent_id = db.record_rule_change_with_consent(rule, consent)

        with db._core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT * FROM domain_events WHERE event_type = 'consent_log.rule_change_recorded'"
            )
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert payload == {"rule_id": rule_id, "consent_id": consent_id}
        assert row["source_module"] == "database"
        assert row["published_at"] is None  # not yet claimed/published by any outbox publisher

    def test_record_rule_change_with_consent_rolls_back_both_on_failure(self, db: Database) -> None:
        """Fault injection at the repository level: force the consent
        write to fail (duplicate id) and confirm the rule write -- which
        the database engine already executed successfully within the
        same transaction -- does not survive either."""
        rule = Rule(created_at=FIXED_TIME, title="Duplicate-consent test", created_by=CreatedBy.USER)
        duplicate_id = new_id()
        consent_1 = ConsentRecord(
            id=duplicate_id, created_at=FIXED_TIME,
            target_type=ConsentTargetType.RULE, action=ConsentAction.APPROVED,
        )
        consent_2 = ConsentRecord(
            id=duplicate_id, created_at=FIXED_TIME,  # SAME id -> UNIQUE violation on second insert
            target_type=ConsentTargetType.RULE, action=ConsentAction.APPROVED,
        )
        db.save_consent_record(consent_1)  # pre-existing row with duplicate_id

        rule_2 = Rule(created_at=FIXED_TIME, title="Should not survive", created_by=CreatedBy.USER)
        with pytest.raises(sqlite3.IntegrityError):
            db.record_rule_change_with_consent(rule_2, consent_2)

        assert not any(r.id == rule_2.id for r in db.get_active_rules()), (
            "the rule write must roll back when its paired consent write fails"
        )


class TestConversationMessages:
    def test_save_and_get_recent_messages_chronological(self, db: Database) -> None:
        first = ConversationMessage(
            created_at=FIXED_TIME, role=MessageRole.USER, content="hi",
            discord_channel_id="chan-1", discord_message_id="1",
        )
        second = ConversationMessage(
            created_at=FIXED_TIME + timedelta(seconds=1), role=MessageRole.ASSISTANT, content="hello",
            discord_channel_id="chan-1", discord_message_id="2",
        )
        db.save_conversation_message(first)
        db.save_conversation_message(second)

        recent = db.get_recent_messages("chan-1", limit=10)
        assert [m.content for m in recent] == ["hi", "hello"]


class TestBackupIntegrationWithExplicitNow:
    def test_create_backup_uses_the_given_now(self, db: Database, tmp_path: Path) -> None:
        path = db.create_backup(reason="manual", now=FIXED_TIME)
        assert path is not None
        assert "20260101" in path.name

    def test_ensure_daily_backup_is_idempotent_within_the_same_day(self, db: Database) -> None:
        first = db.ensure_daily_backup(now=FIXED_TIME)
        second = db.ensure_daily_backup(now=FIXED_TIME + timedelta(hours=2))
        assert first is not None
        assert second is None
