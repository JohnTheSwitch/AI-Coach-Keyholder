"""
database/database.py

Access layer over SQLite. Responsible for:
  - applying migrations (sequential .sql files in database/migrations/),
  - converting between dataclass models (database/models.py) and DB
    rows, including (de)serialization of *_json columns.

Phase 1.2: this class no longer opens connections or manages
transaction boundaries (BEGIN/COMMIT/ROLLBACK) itself -- it delegates
to infrastructure.database.Database (see infrastructure/README.md).
This class is a repository built on top of the shared transaction
layer, not a parallel implementation of connection management. No
method here calls commit()/rollback() directly -- that remains
exclusively in infrastructure.database.Database.transaction().

Usage:
    from database.database import Database
    from infrastructure.clock import SystemClock

    clock = SystemClock()
    db = Database("data/coach_keyholder.db")
    db.migrate(now=clock.now())

    snapshot_id = db.save_context_snapshot(snapshot)
    snapshot = db.get_context_snapshot(snapshot_id)

We deliberately do NOT use an ORM (see the reasoning from the Phase 0
design discussion) -- the hybrid schema (normalized fields + JSON) is
more straightforward and more readable for audits with raw sqlite3 than
it would be behind an extra layer of abstraction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from database import backup as backup_module
from database.models import (
    ApprovalStatus,
    CoachAssessment,
    ConsentAction,
    ConsentRecord,
    ConsentTargetType,
    ContextFactor,
    ContextSnapshot,
    ConversationMessage,
    CreatedBy,
    DecisionResult,
    FactorSource,
    ImpactScore,
    KeyholderAssessment,
    MessageRole,
    ObservationRecord,
    ObservationType,
    RelevantPattern,
    ResolutionMethod,
    RewardState,
    RiskDirection,
    Rule,
    TrustState,
    iso,
    parse_iso,
)
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_BACKUP_RETENTION = 14


class Database:
    def __init__(
        self,
        db_path: str | Path,
        backup_dir: str | Path | None = None,
        backup_retention: int = DEFAULT_BACKUP_RETENTION,
        *,
        core: CoreDatabase | None = None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # `core` injectable for tests that want several Database (repository)
        # facades sharing one infrastructure.database.Database/connection
        # policy — normal production use just lets this construct its own.
        self._core = core if core is not None else CoreDatabase(self.db_path)

        # Backups default to living next to the database, in the same
        # (gitignored) data/ folder, so source-code updates never touch them
        # (see .gitignore).
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent / "backups"
        self.backup_retention = backup_retention

    # -------------------------------------------------------------------
    # Migrations
    # -------------------------------------------------------------------

    def migrate(self, now: datetime) -> list[int]:
        """
        Applies all migrations from database/migrations/ not yet applied,
        in order by the number in the filename (001_, 002_, ...).
        Returns the list of version numbers that were newly applied.

        Rule for migration authors: a migration may only add (CREATE
        TABLE IF NOT EXISTS, ALTER TABLE ADD COLUMN, new indexes). It
        must never contain DROP TABLE/COLUMN or any other destructive
        data operation -- updating the application must never require
        or cause the loss of user data (see database/migrations/README.md).

        If the database file already exists and there are new
        migrations to apply, a backup is created first
        (reason='pre_migration'), regardless of whether a daily backup
        has already run today -- a migration is a risky moment and
        deserves its own extra backup.

        `now` (timezone-aware UTC, from the injected Clock) is used only
        for the possible pre_migration backup -- using `raw_connection()`,
        not `transaction()`, is deliberate: `executescript()` has its
        own (implicit) commit behavior incompatible with our BEGIN
        IMMEDIATE (see infrastructure/database.py, `raw_connection()`
        docstring).
        """
        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

        with self._core.raw_connection() as conn:
            current_version = 0
            try:
                row = conn.execute(
                    "SELECT MAX(version) as v FROM schema_version"
                ).fetchone()
                if row and row["v"] is not None:
                    current_version = row["v"]
            except sqlite3.OperationalError:
                pass  # the schema_version table doesn't exist yet -> the first migration will create it

            pending = [
                path for path in migration_files
                if int(path.name.split("_")[0]) > current_version
            ]

        if not pending:
            return []

        # Backup before migrating -- only if the DB already had some
        # schema version applied before (current_version > 0). On the
        # very first run, SQLite already creates an empty file when we
        # connect above, but backing up an empty database with no data
        # is pointless, so we distinguish by current_version, not by mere
        # file existence.
        if current_version > 0:
            self.create_backup(reason="pre_migration", now=now)

        applied: list[int] = []
        with self._core.raw_connection() as conn:
            for path in pending:
                version = int(path.name.split("_")[0])
                sql = path.read_text(encoding="utf-8")
                conn.executescript(sql)
                applied.append(version)

        return applied

    # -------------------------------------------------------------------
    # Backups
    # -------------------------------------------------------------------

    def create_backup(self, reason: str, now: datetime) -> Path | None:
        """Creates a backup and immediately applies the rotation policy."""
        path = backup_module.create_backup(self.db_path, self.backup_dir, reason, now=now)
        backup_module.rotate_backups(self.backup_dir, keep=self.backup_retention)
        return path

    def ensure_daily_backup(self, now: datetime) -> Path | None:
        """
        Guarantees at most 1 automatic backup per day. Call at application
        startup (independent of whether migrations ran -- this also
        covers ordinary daily operation with no schema changes).
        """
        path = backup_module.ensure_daily_backup(self.db_path, self.backup_dir, now=now)
        if path is not None:
            backup_module.rotate_backups(self.backup_dir, keep=self.backup_retention)
        return path

    # -------------------------------------------------------------------
    # Context Snapshot
    # -------------------------------------------------------------------

    def save_context_snapshot(self, snap: ContextSnapshot) -> str:
        with self._core.transaction() as tx:
            tx.execute(
                """
                INSERT INTO context_snapshots
                    (id, created_at, engine_version, overall_confidence,
                     data_freshness_hours, context_factors_json, relevant_patterns_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap.id,
                    iso(snap.created_at),
                    snap.engine_version,
                    snap.overall_confidence,
                    snap.data_freshness_hours,
                    json.dumps([f.to_dict() for f in snap.context_factors]),
                    json.dumps([p.to_dict() for p in snap.relevant_patterns]),
                ),
            )
        return snap.id

    def get_context_snapshot(self, snapshot_id: str) -> ContextSnapshot | None:
        with self._core.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM context_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            return None
        return ContextSnapshot(
            id=row["id"],
            created_at=parse_iso(row["created_at"]),
            engine_version=row["engine_version"],
            overall_confidence=row["overall_confidence"],
            data_freshness_hours=row["data_freshness_hours"],
            context_factors=[
                ContextFactor.from_dict(d) for d in json.loads(row["context_factors_json"])
            ],
            relevant_patterns=[
                RelevantPattern.from_dict(d) for d in json.loads(row["relevant_patterns_json"])
            ],
        )

    # -------------------------------------------------------------------
    # Coach Assessment
    # -------------------------------------------------------------------

    def save_coach_assessment(self, a: CoachAssessment) -> str:
        with self._core.transaction() as tx:
            tx.execute(
                """
                INSERT INTO coach_assessments
                    (id, created_at, engine_version, context_snapshot_id,
                     recommendation, reasoning, confidence, risk_direction,
                     sustainability_score, supporting_factors_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    a.id,
                    iso(a.created_at),
                    a.engine_version,
                    a.context_snapshot_id,
                    a.recommendation,
                    a.reasoning,
                    a.confidence,
                    a.risk_direction.value,
                    a.sustainability_score,
                    json.dumps(a.supporting_factors),
                ),
            )
        return a.id

    def get_coach_assessment(self, assessment_id: str) -> CoachAssessment | None:
        with self._core.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM coach_assessments WHERE id = ?", (assessment_id,)
            ).fetchone()
        if row is None:
            return None
        return CoachAssessment(
            id=row["id"],
            created_at=parse_iso(row["created_at"]),
            engine_version=row["engine_version"],
            context_snapshot_id=row["context_snapshot_id"],
            recommendation=row["recommendation"],
            reasoning=row["reasoning"],
            confidence=row["confidence"],
            risk_direction=RiskDirection(row["risk_direction"]),
            sustainability_score=row["sustainability_score"],
            supporting_factors=json.loads(row["supporting_factors_json"]),
        )

    # -------------------------------------------------------------------
    # Keyholder Assessment
    # -------------------------------------------------------------------

    def save_keyholder_assessment(self, a: KeyholderAssessment) -> str:
        with self._core.transaction() as tx:
            tx.execute(
                """
                INSERT INTO keyholder_assessments
                    (id, created_at, engine_version, context_snapshot_id,
                     recommendation, reasoning, confidence, consistency_score,
                     trust_state_json, reward_state_json, rule_relevance_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    a.id,
                    iso(a.created_at),
                    a.engine_version,
                    a.context_snapshot_id,
                    a.recommendation,
                    a.reasoning,
                    a.confidence,
                    a.consistency_score,
                    json.dumps(a.trust_state.__dict__),
                    json.dumps(a.reward_state.__dict__),
                    json.dumps(a.rule_relevance),
                ),
            )
        return a.id

    def get_keyholder_assessment(self, assessment_id: str) -> KeyholderAssessment | None:
        with self._core.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM keyholder_assessments WHERE id = ?", (assessment_id,)
            ).fetchone()
        if row is None:
            return None
        return KeyholderAssessment(
            id=row["id"],
            created_at=parse_iso(row["created_at"]),
            engine_version=row["engine_version"],
            context_snapshot_id=row["context_snapshot_id"],
            recommendation=row["recommendation"],
            reasoning=row["reasoning"],
            confidence=row["confidence"],
            consistency_score=row["consistency_score"],
            trust_state=TrustState(**json.loads(row["trust_state_json"])),
            reward_state=RewardState(**json.loads(row["reward_state_json"])),
            rule_relevance=json.loads(row["rule_relevance_json"]),
        )

    def record_trust_history(
        self, trust_score: float, reason: str, now: datetime, keyholder_assessment_id: str | None = None
    ) -> str:
        from database.models import new_id

        record_id = new_id()
        with self._core.transaction() as tx:
            tx.execute(
                """
                INSERT INTO trust_history (id, recorded_at, trust_score, reason, keyholder_assessment_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record_id, iso(now), trust_score, reason, keyholder_assessment_id),
            )
        return record_id

    # -------------------------------------------------------------------
    # Decision Result
    # -------------------------------------------------------------------

    def save_decision_result(self, d: DecisionResult) -> str:
        with self._core.transaction() as tx:
            tx.execute(
                """
                INSERT INTO decision_results
                    (id, created_at, engine_version, context_snapshot_id,
                     coach_assessment_id, keyholder_assessment_id, final_decision,
                     resolution_method, impact_score, impact_is_significant,
                     impact_factors_json, is_critical_change, requires_user_approval,
                     safety_override, explanation, approval_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d.id,
                    iso(d.created_at),
                    d.engine_version,
                    d.context_snapshot_id,
                    d.coach_assessment_id,
                    d.keyholder_assessment_id,
                    d.final_decision,
                    d.resolution_method.value,
                    d.impact_score.value,
                    int(d.impact_score.is_significant),
                    json.dumps(d.impact_score.contributing_factors),
                    int(d.is_critical_change),
                    int(d.requires_user_approval),
                    int(d.safety_override),
                    d.explanation,
                    d.approval_status.value,
                ),
            )
        return d.id

    def get_decision_result(self, decision_id: str) -> DecisionResult | None:
        with self._core.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM decision_results WHERE id = ?", (decision_id,)
            ).fetchone()
        if row is None:
            return None
        return DecisionResult(
            id=row["id"],
            created_at=parse_iso(row["created_at"]),
            engine_version=row["engine_version"],
            context_snapshot_id=row["context_snapshot_id"],
            coach_assessment_id=row["coach_assessment_id"],
            keyholder_assessment_id=row["keyholder_assessment_id"],
            final_decision=row["final_decision"],
            resolution_method=ResolutionMethod(row["resolution_method"]),
            impact_score=ImpactScore(
                value=row["impact_score"],
                is_significant=bool(row["impact_is_significant"]),
                contributing_factors=json.loads(row["impact_factors_json"]),
            ),
            is_critical_change=bool(row["is_critical_change"]),
            requires_user_approval=bool(row["requires_user_approval"]),
            safety_override=bool(row["safety_override"]),
            explanation=row["explanation"],
            approval_status=ApprovalStatus(row["approval_status"]),
        )

    def set_decision_approval_status(self, decision_id: str, status: ApprovalStatus) -> None:
        with self._core.transaction() as tx:
            tx.execute(
                "UPDATE decision_results SET approval_status = ? WHERE id = ?",
                (status.value, decision_id),
            )

    def get_pending_approvals(self) -> list[DecisionResult]:
        """Decisions awaiting user approval -- for the Discord approval_flow."""
        with self._core.transaction() as tx:
            rows = tx.execute(
                "SELECT id FROM decision_results WHERE approval_status = ?",
                (ApprovalStatus.PENDING.value,),
            ).fetchall()
        return [self.get_decision_result(r["id"]) for r in rows]

    # -------------------------------------------------------------------
    # Observations (the runtime only writes -- see the observations/ module)
    # -------------------------------------------------------------------

    def save_observation(self, o: ObservationRecord) -> str:
        with self._core.transaction() as tx:
            tx.execute(
                """
                INSERT INTO observations
                    (id, created_at, observation_type, related_decision_id,
                     description, raw_data_json, flagged_for_review)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    o.id,
                    iso(o.created_at),
                    o.observation_type.value,
                    o.related_decision_id,
                    o.description,
                    json.dumps(o.raw_data),
                    int(o.flagged_for_review),
                ),
            )
        return o.id

    def get_unreviewed_observations(self) -> list[ObservationRecord]:
        """Used exclusively by the audit export tool (observations/export.py), never by the runtime."""
        with self._core.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM observations WHERE reviewed_at IS NULL ORDER BY created_at"
            ).fetchall()
        result = []
        for row in rows:
            result.append(
                ObservationRecord(
                    id=row["id"],
                    created_at=parse_iso(row["created_at"]),
                    observation_type=ObservationType(row["observation_type"]),
                    related_decision_id=row["related_decision_id"],
                    description=row["description"],
                    raw_data=json.loads(row["raw_data_json"]),
                    flagged_for_review=bool(row["flagged_for_review"]),
                    reviewed_at=parse_iso(row["reviewed_at"]) if row["reviewed_at"] else None,
                    review_notes=row["review_notes"],
                )
            )
        return result

    def mark_observation_reviewed(self, observation_id: str, now: datetime, notes: str | None = None) -> None:
        """Called exclusively by the audit export / review tool."""
        with self._core.transaction() as tx:
            tx.execute(
                "UPDATE observations SET reviewed_at = ?, review_notes = ? WHERE id = ?",
                (iso(now), notes, observation_id),
            )

    # -------------------------------------------------------------------
    # Rules
    # -------------------------------------------------------------------

    @staticmethod
    def _insert_rule_row(tx: Transaction, rule: Rule) -> None:
        """Shared INSERT logic for rules -- used both directly (save_rule)
        and as part of a larger atomic operation (supersede_rule,
        record_rule_change_with_consent)."""
        tx.execute(
            """
            INSERT INTO rules
                (id, rule_group_id, version, title, description, category,
                 parameters_json, is_active, supersedes_id, created_at,
                 created_by, is_critical)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule.id,
                rule.rule_group_id,
                rule.version,
                rule.title,
                rule.description,
                rule.category,
                json.dumps(rule.parameters),
                int(rule.is_active),
                rule.supersedes_id,
                iso(rule.created_at),
                rule.created_by.value,
                int(rule.is_critical),
            ),
        )

    @staticmethod
    def _insert_consent_row(tx: Transaction, c: ConsentRecord) -> None:
        """Shared INSERT logic for consent_log -- used both directly
        (save_consent_record) and as part of record_rule_change_with_consent."""
        tx.execute(
            """
            INSERT INTO consent_log
                (id, created_at, target_type, target_id, target_version,
                 action, decision_result_id, explanation_shown, user_comment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c.id,
                iso(c.created_at),
                c.target_type.value,
                c.target_id,
                c.target_version,
                c.action.value,
                c.decision_result_id,
                c.explanation_shown,
                c.user_comment,
            ),
        )

    def save_rule(self, rule: Rule) -> str:
        apply_transition(self._core, write=lambda tx, _state: self._insert_rule_row(tx, rule))
        return rule.id

    def supersede_rule(self, new_rule: Rule) -> str:
        """
        Creates a new rule version and deactivates the previous version in
        the same transaction. new_rule.supersedes_id must be set to the
        ID of the previous version.

        Refactored onto apply_transition() (infrastructure/database.py) --
        same behavior as before (two SQL statements in one transaction),
        now built on the shared generic helper instead of its own ad hoc
        composition.
        """

        def write(tx: Transaction, _state: object) -> str:
            if new_rule.supersedes_id:
                tx.execute(
                    "UPDATE rules SET is_active = 0 WHERE id = ?",
                    (new_rule.supersedes_id,),
                )
            self._insert_rule_row(tx, new_rule)
            return new_rule.id

        return apply_transition(self._core, write=write)

    def record_rule_change_with_consent(self, new_rule: Rule, consent: ConsentRecord) -> tuple[str, str]:
        """
        Atomically writes a new rule version (optionally deactivating the
        previous one, just like supersede_rule) TOGETHER WITH its
        ConsentRecord -- philosophy.md 2.5 (Consent & Control): a rule
        change is never legitimate without corresponding consent, so
        this method exists specifically so those two writes can never
        happen independently of each other (not even on a failure
        partway through).

        A genuine demonstration of apply_transition() across TWO
        different tables (rules + consent_log), not just the same table
        twice like supersede_rule.

        Phase 1.4: the first real use of the `events=` slot (outside of
        tests). This is not a cataloged event from
        `domain_events_catalog.md` -- that catalog describes future
        domain modules (Trust Manager, Penalty Engine, ...) that do not
        exist as code yet. This is an honestly-named demonstration on
        the existing Phase 0 table, not a pretense that `rules`/`consent_log`
        are a full domain module per the catalog.
        """

        def write(tx: Transaction, _state: object) -> tuple[str, str]:
            if new_rule.supersedes_id:
                tx.execute(
                    "UPDATE rules SET is_active = 0 WHERE id = ?",
                    (new_rule.supersedes_id,),
                )
            self._insert_rule_row(tx, new_rule)
            self._insert_consent_row(tx, consent)
            return (new_rule.id, consent.id)

        def events(tx: Transaction, _state: object, result: tuple[str, str]) -> None:
            rule_id, consent_id = result
            write_event(
                tx,
                DomainEvent(
                    event_type="consent_log.rule_change_recorded",
                    source_module="database",
                    payload={"rule_id": rule_id, "consent_id": consent_id},
                    occurred_at=new_rule.created_at,
                ),
            )

        return apply_transition(self._core, write=write, events=events)

    def get_active_rules(self) -> list[Rule]:
        with self._core.transaction() as tx:
            rows = tx.execute("SELECT * FROM rules WHERE is_active = 1").fetchall()
        return [self._row_to_rule(r) for r in rows]

    def get_rule_history(self, rule_group_id: str) -> list[Rule]:
        with self._core.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM rules WHERE rule_group_id = ? ORDER BY version",
                (rule_group_id,),
            ).fetchall()
        return [self._row_to_rule(r) for r in rows]

    @staticmethod
    def _row_to_rule(row: sqlite3.Row) -> Rule:
        return Rule(
            id=row["id"],
            rule_group_id=row["rule_group_id"],
            version=row["version"],
            title=row["title"],
            description=row["description"],
            category=row["category"],
            parameters=json.loads(row["parameters_json"]),
            is_active=bool(row["is_active"]),
            supersedes_id=row["supersedes_id"],
            created_at=parse_iso(row["created_at"]),
            created_by=CreatedBy(row["created_by"]),
            is_critical=bool(row["is_critical"]),
        )

    # -------------------------------------------------------------------
    # Consent Log (append-only)
    # -------------------------------------------------------------------

    def save_consent_record(self, c: ConsentRecord) -> str:
        apply_transition(self._core, write=lambda tx, _state: self._insert_consent_row(tx, c))
        return c.id

    def get_consent_history(
        self, target_type: ConsentTargetType, target_id: str | None = None
    ) -> list[ConsentRecord]:
        with self._core.transaction() as tx:
            if target_id is not None:
                rows = tx.execute(
                    "SELECT * FROM consent_log WHERE target_type = ? AND target_id = ? ORDER BY created_at",
                    (target_type.value, target_id),
                ).fetchall()
            else:
                rows = tx.execute(
                    "SELECT * FROM consent_log WHERE target_type = ? ORDER BY created_at",
                    (target_type.value,),
                ).fetchall()
        return [
            ConsentRecord(
                id=r["id"],
                created_at=parse_iso(r["created_at"]),
                target_type=ConsentTargetType(r["target_type"]),
                target_id=r["target_id"],
                target_version=r["target_version"],
                action=ConsentAction(r["action"]),
                decision_result_id=r["decision_result_id"],
                explanation_shown=r["explanation_shown"],
                user_comment=r["user_comment"],
            )
            for r in rows
        ]

    # -------------------------------------------------------------------
    # Conversation messages
    # -------------------------------------------------------------------

    def save_conversation_message(self, m: ConversationMessage) -> str:
        with self._core.transaction() as tx:
            tx.execute(
                """
                INSERT INTO conversation_messages
                    (id, created_at, role, content, discord_channel_id,
                     discord_message_id, related_decision_id, from_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    m.id,
                    iso(m.created_at),
                    m.role.value,
                    m.content,
                    m.discord_channel_id,
                    m.discord_message_id,
                    m.related_decision_id,
                    None if m.from_model is None else int(bool(m.from_model)),
                ),
            )
        return m.id

    def get_user_messages_between(self, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
        """Honesty and proof, slice 4: (time, text) of the user's own messages in [start, end), oldest first. Read-only, for contradiction checks."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT created_at, content FROM conversation_messages WHERE role = 'user' ORDER BY created_at")
        found = []
        for r in rows:
            at = parse_iso(r["created_at"])
            if start <= at < end:
                found.append((at, r["content"] or ""))
        return found

    def get_recent_messages(
        self, discord_channel_id: str, limit: int = 20
    ) -> list[ConversationMessage]:
        """Short-term memory: the last N messages in a channel, chronologically."""
        with self._core.transaction() as tx:
            rows = tx.execute(
                """
                SELECT * FROM conversation_messages
                WHERE discord_channel_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (discord_channel_id, limit),
            ).fetchall()
        messages = [
            ConversationMessage(
                id=r["id"],
                created_at=parse_iso(r["created_at"]),
                role=MessageRole(r["role"]),
                content=r["content"],
                discord_channel_id=r["discord_channel_id"],
                discord_message_id=r["discord_message_id"],
                related_decision_id=r["related_decision_id"],
            )
            for r in rows
        ]
        return list(reversed(messages))
