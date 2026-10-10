"""
trust_manager/repository.py

Trust Manager Slice 1 — the repository built on
infrastructure.database.Database, the same composition pattern
database/database.py already established (Phase 1.2): this class opens
no sqlite3 connections of its own, and every write goes through
`self._core.transaction()`/`apply_transition()`.

Canonical spec: docs/architecture/trust_manager_technical_design.md.
See trust_manager/README.md for exactly what this slice covers and what
is deferred.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from trust_manager.checkin_scoring import checkin_evidence_spec
from trust_manager.goal_scoring import (
    GoalAssessmentDirection,
    goal_evidence_confidence,
    goal_evidence_type,
    goal_evidence_weight,
)
from trust_manager.models import (
    BreachDirectness,
    ConfirmationRecord,
    ConfirmationSource,
    ConfirmedIncidentSummary,
    CooperationAssessment,
    EvidenceConfidenceLevel,
    EvidenceType,
    ImpactLevel,
    Incident,
    IncidentAssessment,
    IncidentConfirmation,
    IncidentEvidence,
    IntentAssessment,
    RepetitionEvidence,
    SeverityTier,
    TrustDomain,
    TrustDomainState,
    TrustEvidence,
    TrustRecalculation,
    new_id,
)
from trust_manager.recalculation import (
    CONFIDENCE_ROLLING_WINDOW_DAYS,
    apply_recalculation,
    compute_confidence,
    effective_weight,
)
from trust_manager.severity import assess_severity, raw_weight_for_incident

# 3.4 — critical_change parameters (TI20's same discipline extends here):
# not freely adjustable, changing them requires a ConsentRecord.
DEFAULT_NEW_DOMAIN_SCORE = 0.6
DEFAULT_NEW_DOMAIN_CONFIDENCE = 0.15


# _iso/_parse_iso: thin local aliases for the shared implementation
# (infrastructure/time_format.py) -- kept as private names here so
# every existing call site in this module is unchanged; consolidated
# during the final architecture review pass (Phase 2.7) to remove five
# identical copies of this pair across the codebase.
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso


class TrustManager:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    # -------------------------------------------------------------------
    # 2.1 / TI1 — Domain Registry
    # -------------------------------------------------------------------

    def create_domain(
        self,
        *,
        domain_id: str,
        display_name: str,
        description: str,
        created_via_consent_id: str,
        now: datetime,
        initial_score_override: float | None = None,
        initial_confidence_override: float | None = None,
    ) -> TrustDomain:
        """
        TI1: never created without a consent id. Initializes
        TrustDomainState with the 3.4 defaults, or the provided
        overrides -- both are part of the same approved consent
        request, never a later runtime decision.
        """
        domain = TrustDomain(
            domain_id=domain_id,
            display_name=display_name,
            description=description,
            created_at=now,
            created_via_consent_id=created_via_consent_id,
            initial_score_override=initial_score_override,
            initial_confidence_override=initial_confidence_override,
        )

        def write(tx: Transaction, _state: object) -> TrustDomain:
            tx.execute(
                """
                INSERT INTO trust_domains
                    (domain_id, display_name, description, is_active, created_at,
                     created_via_consent_id, initial_score_override, initial_confidence_override)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    domain.domain_id, domain.display_name, domain.description,
                    _iso(domain.created_at), domain.created_via_consent_id,
                    domain.initial_score_override, domain.initial_confidence_override,
                ),
            )
            tx.execute(
                """
                INSERT INTO trust_domain_state
                    (domain_id, score, confidence, trend, last_recalculated_at, last_relevant_event_at)
                VALUES (?, ?, ?, 'stable', ?, NULL)
                """,
                (
                    domain.domain_id,
                    initial_score_override if initial_score_override is not None else DEFAULT_NEW_DOMAIN_SCORE,
                    initial_confidence_override if initial_confidence_override is not None else DEFAULT_NEW_DOMAIN_CONFIDENCE,
                    _iso(now),
                ),
            )
            return domain

        def events(tx: Transaction, _state: object, _result: TrustDomain) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="trust_domain.created",
                    source_module="trust_manager",
                    payload={"domain_id": domain.domain_id},
                    occurred_at=now,
                ),
            )

        return apply_transition(self._core, write=write, events=events)

    def deactivate_domain(self, domain_id: str, *, via_consent_id: str, now: datetime) -> None:
        """TI1: deactivation always requires its own consent id."""

        def write(tx: Transaction, _state: object) -> None:
            tx.execute(
                "UPDATE trust_domains SET is_active = 0, deactivated_at = ?, deactivated_via_consent_id = ? WHERE domain_id = ?",
                (_iso(now), via_consent_id, domain_id),
            )

        def events(tx: Transaction, _state: object, _result: None) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="trust_domain.deactivated",
                    source_module="trust_manager",
                    payload={"domain_id": domain_id},
                    occurred_at=now,
                ),
            )

        apply_transition(self._core, write=write, events=events)

    def reactivate_domain(self, domain_id: str, *, via_consent_id: str, now: datetime) -> None:
        """
        TI1: reactivation is its OWN critical_change -- never a silent
        flip of is_active back to true using the original consent.
        """

        def write(tx: Transaction, _state: object) -> None:
            tx.execute(
                "UPDATE trust_domains SET is_active = 1, deactivated_at = NULL, deactivated_via_consent_id = NULL WHERE domain_id = ?",
                (domain_id,),
            )

        def events(tx: Transaction, _state: object, _result: None) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="trust_domain.reactivated",
                    source_module="trust_manager",
                    payload={"domain_id": domain_id, "via_consent_id": via_consent_id},
                    occurred_at=now,
                ),
            )

        apply_transition(self._core, write=write, events=events)

    def get_domain_state(self, domain_id: str) -> TrustDomainState | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM trust_domain_state WHERE domain_id = ?", (domain_id,))
        if row is None:
            return None
        return TrustDomainState(
            domain_id=row["domain_id"],
            score=row["score"],
            confidence=row["confidence"],
            trend=row["trend"],
            last_recalculated_at=_parse_iso(row["last_recalculated_at"]),
            last_relevant_event_at=_parse_iso(row["last_relevant_event_at"]) if row["last_relevant_event_at"] else None,
        )

    # -------------------------------------------------------------------
    # 2.6, 3.1-3.6 — Score Recalculation Pipeline (Slice 2)
    # -------------------------------------------------------------------

    def recalculate_domain_trust(self, domain_id: str, *, triggered_by: str, now: datetime) -> TrustRecalculation:
        """
        Public entry point for triggers that run in their OWN
        transaction, separate from whatever produced the evidence being
        consumed -- 'window_completion' and 'scheduled_review' (3.2),
        once those modules exist. The 'incident' trigger does NOT call
        this -- confirm_incident() calls the internal
        `_recalculate_domain_trust_in_transaction()` directly, inside
        its own already-open transaction, since evidence and its
        consumption are produced by the same call in that case.
        """
        def write(tx: Transaction, _state: object) -> TrustRecalculation:
            return self._recalculate_domain_trust_in_transaction(tx, domain_id, triggered_by=triggered_by, now=now)

        def events(tx: Transaction, _state: object, result: TrustRecalculation) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="trust_domain.recalculated",
                    source_module="trust_manager",
                    payload={
                        "domain_id": result.domain_id,
                        "previous_score": result.previous_score,
                        "new_score": result.new_score,
                        "previous_confidence": result.previous_confidence,
                        "new_confidence": result.new_confidence,
                        "triggered_by": result.triggered_by,
                    },
                    occurred_at=now,
                ),
            )

        return apply_transition(self._core, write=write, events=events)

    def _recalculate_domain_trust_in_transaction(
        self, tx: Transaction, domain_id: str, *, triggered_by: str, now: datetime
    ) -> TrustRecalculation:
        """
        The actual pipeline (3.1's data-flow diagram, made concrete):
        select unconsumed TrustEvidence, sum effective_weight (3.3, capped
        per-row), apply the per-recalculation delta cap (3.5, TI19),
        recompute confidence over the rolling window (3.6), write
        TrustRecalculation + TrustRecalculationEvidence (consuming the
        evidence -- TI4's UNIQUE constraint prevents ever consuming a row
        twice), and update TrustDomainState -- all against the caller's
        already-open `tx`, never opening its own.

        TI10b: if there is no unconsumed evidence at all, this still
        recomputes confidence (staleness -- 3.6) with delta_score=0 and
        writes NO TrustRecalculationEvidence rows, rather than refusing
        to run -- a purely confidence-driven recalculation is legitimate.
        """
        state_row = tx.fetch_one("SELECT * FROM trust_domain_state WHERE domain_id = ?", (domain_id,))
        previous_score = state_row["score"]
        previous_confidence = state_row["confidence"]

        unconsumed = self._select_unconsumed_evidence(tx, domain_id)
        proposed_delta = sum(effective_weight(e) for e in unconsumed)
        new_score = apply_recalculation(previous_score, proposed_delta)

        recalculation = TrustRecalculation(
            domain_id=domain_id,
            created_at=now,
            previous_score=previous_score,
            new_score=new_score,
            previous_confidence=previous_confidence,
            new_confidence=0.0,  # placeholder, overwritten below once TrustRecalculationEvidence is written
            triggered_by=triggered_by,
            explanation=(
                f"{len(unconsumed)} unconsumed evidence row(s), "
                f"proposed_delta={proposed_delta:.4f}, bounded new_score={new_score:.4f}"
                if unconsumed else
                f"no unconsumed evidence -- staleness-driven confidence recalculation only (TI10b), trigger={triggered_by}"
            ),
        )

        tx.execute(
            """
            INSERT INTO trust_recalculations
                (id, domain_id, created_at, previous_score, new_score,
                 previous_confidence, new_confidence, triggered_by, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recalculation.id, domain_id, _iso(now), previous_score, new_score,
                previous_confidence, 0.0, triggered_by, recalculation.explanation,
            ),
        )
        for e in unconsumed:
            tx.execute(
                "INSERT INTO trust_recalculation_evidence (recalculation_id, evidence_id, effective_weight, created_at) VALUES (?, ?, ?, ?)",
                (recalculation.id, e.id, effective_weight(e), _iso(now)),
            )

        # 3.6: confidence over the rolling window, computed AFTER this
        # recalculation's own consumption is recorded, so newly-consumed
        # evidence already counts.
        window_start = now - timedelta(days=CONFIDENCE_ROLLING_WINDOW_DAYS)
        applied_in_window = self._select_applied_evidence_in_window(tx, domain_id, window_start)
        new_confidence = compute_confidence(applied_in_window)

        tx.execute("UPDATE trust_recalculations SET new_confidence = ? WHERE id = ?", (new_confidence, recalculation.id))
        tx.execute(
            "UPDATE trust_domain_state SET score = ?, confidence = ?, last_recalculated_at = ?, last_relevant_event_at = ? WHERE domain_id = ?",
            (new_score, new_confidence, _iso(now), _iso(now) if unconsumed else state_row["last_relevant_event_at"], domain_id),
        )

        return TrustRecalculation(
            id=recalculation.id, domain_id=domain_id, created_at=now,
            previous_score=previous_score, new_score=new_score,
            previous_confidence=previous_confidence, new_confidence=new_confidence,
            triggered_by=triggered_by, explanation=recalculation.explanation,
        )

    # -------------------------------------------------------------------
    # 5.1 — register_incident_report() / confirm_incident()
    # -------------------------------------------------------------------

    def register_incident_report(
        self,
        *,
        rule_group_id: str,
        trust_domain: str,
        description: str,
        evidence: IncidentEvidence,
        now: datetime,
    ) -> Incident:
        """A new Incident ALWAYS starts UNCONFIRMED. assess_severity() is
        NOT called yet -- Incident.assessment stays None (TI15)."""
        incident = Incident(
            created_at=now,
            rule_group_id=rule_group_id,
            trust_domain=trust_domain,
            description=description,
            evidence=evidence,
        )

        def write(tx: Transaction, _state: object) -> Incident:
            self._insert_incident_row(tx, incident)
            return incident

        def events(tx: Transaction, _state: object, result: Incident) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="incident.reported",
                    source_module="trust_manager",
                    payload={"incident_id": result.id, "trust_domain": result.trust_domain},
                    occurred_at=now,
                ),
            )

        return apply_transition(self._core, write=write, events=events)

    def confirm_incident(
        self,
        incident_id: str,
        *,
        new_confirmation: IncidentConfirmation,
        source: ConfirmationSource,
        evidence_description: str,
        cooperation: CooperationAssessment | None = None,
        now: datetime,
    ) -> None:
        """
        The only way an Incident advances (TI16). When
        new_confirmation == CONFIRMED, the ConfirmationRecord write, the
        Incident.confirmation/assessment updates, assess_severity(), and
        the resulting TrustEvidence write ALL happen in this one
        transaction (TI23, 14.2) -- no path exists for a CONFIRMED
        Incident to persist with assessment=None beyond this single
        atomic boundary.
        """
        cooperation = cooperation or CooperationAssessment()

        def load(tx: Transaction) -> Incident:
            return self._load_incident(tx, incident_id)

        def write(tx: Transaction, incident: Incident) -> ConfirmationRecord:
            record = ConfirmationRecord(
                incident_id=incident_id,
                created_at=now,
                previous_confirmation=incident.confirmation,
                new_confirmation=new_confirmation,
                source=source,
                evidence_description=evidence_description,
            )
            tx.execute(
                """
                INSERT INTO confirmation_records
                    (id, incident_id, created_at, previous_confirmation, new_confirmation, source, evidence_description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (record.id, incident_id, _iso(now), incident.confirmation.value,
                 new_confirmation.value, source.value, evidence_description),
            )
            tx.execute(
                "UPDATE incidents SET confirmation = ? WHERE id = ?",
                (new_confirmation.value, incident_id),
            )

            if new_confirmation == IncidentConfirmation.CONFIRMED:
                # SAME transaction (TI23) -- severity, assessment, and
                # TrustEvidence never wait for a second write boundary.
                severity = assess_severity(incident.evidence)
                assessment = IncidentAssessment(
                    intrinsic_severity=severity,
                    confirmation=new_confirmation,
                    cooperation=cooperation,
                    evidence=incident.evidence,
                    rubric_explanation=(
                        f"impact={incident.evidence.actual_or_potential_impact.value}, "
                        f"intent={incident.evidence.intentionality.value}, "
                        f"breach={incident.evidence.rule_breach_directness.value}, "
                        f"repetition_count={incident.evidence.repetition.same_rule_confirmed_count} "
                        f"-> {severity.value}"
                    ),
                )
                self._update_incident_assessment(tx, incident_id, assessment)
                incident.assessment = assessment  # same in-memory object events() receives as _incident -- lets the payload include it without a second DB read
                evidence_row = self._insert_trust_evidence_for_incident(tx, incident, assessment, now)
                # Written directly here, inside write(), rather than via
                # the events= callable below: events= only receives the
                # ConfirmationRecord this function returns, not the
                # TrustEvidence row created conditionally inside it. Both
                # writes are still in the exact same transaction (this
                # whole function runs against the one `tx` apply_transition
                # opened) -- TI23 only requires one transaction, not that
                # every event be emitted from the same callable.
                write_event(
                    tx,
                    DomainEvent(
                        event_type="trust_evidence.recorded",
                        source_module="trust_manager",
                        payload={"evidence_id": evidence_row.id, "domain_id": evidence_row.domain_id},
                        occurred_at=now,
                    ),
                )
                # 3.2: the 'incident' recalculation trigger fires
                # immediately after INCIDENT_IMPACT evidence is created,
                # for confirmation=CONFIRMED only (exactly this branch) --
                # in the SAME transaction, since both the evidence and its
                # consumption are produced by the same call, with no
                # cross-transaction event delivery needed for this
                # particular trigger (contrast: window_completion/
                # scheduled_review, deferred, will genuinely need to run
                # in their own later transaction).
                recalculation = self._recalculate_domain_trust_in_transaction(
                    tx, incident.trust_domain, triggered_by="incident", now=now,
                )
                write_event(
                    tx,
                    DomainEvent(
                        event_type="trust_domain.recalculated",
                        source_module="trust_manager",
                        payload={
                            "domain_id": recalculation.domain_id,
                            "previous_score": recalculation.previous_score,
                            "new_score": recalculation.new_score,
                            "previous_confidence": recalculation.previous_confidence,
                            "new_confidence": recalculation.new_confidence,
                            "triggered_by": recalculation.triggered_by,
                        },
                        occurred_at=now,
                    ),
                )

            return record

        def events(tx: Transaction, _incident: Incident, record: ConfirmationRecord) -> None:
            payload = {
                "incident_id": incident_id,
                "trust_domain": _incident.trust_domain,
                "rule_group_id": _incident.rule_group_id,
                "previous_confirmation": record.previous_confirmation.value,
                "new_confirmation": record.new_confirmation.value,
            }
            if _incident.assessment is not None:
                # Populated only when this transition reached CONFIRMED
                # (TI15/TI23) -- carried directly in the payload, not left
                # for a consumer to fetch via get_incident_assessment(),
                # exactly the lesson system/README.md documents: a
                # consumer handler running inside consume_event()'s own
                # transaction cannot call back into another module's
                # transaction-opening public API (NestedTransactionError).
                payload["intrinsic_severity"] = _incident.assessment.intrinsic_severity.value
                payload["cooperation_self_disclosed"] = _incident.assessment.cooperation.self_disclosed
                payload["cooperation_active_cooperation_in_resolution"] = (
                    _incident.assessment.cooperation.active_cooperation_in_resolution
                )
            write_event(
                tx,
                DomainEvent(
                    event_type="incident.confirmation_changed",
                    source_module="trust_manager",
                    payload=payload,
                    occurred_at=now,
                ),
            )

        apply_transition(self._core, load=load, write=write, events=events)

    # -------------------------------------------------------------------
    # 13 — Public Read API for Cross-Module Queries
    # -------------------------------------------------------------------

    def get_incident_assessment(self, incident_id: str) -> IncidentAssessment | None:
        """
        The ONLY permitted way for another module to read an Incident's
        severity/cooperation data. Returns None if the Incident is not
        CONFIRMED (TI15) or does not exist.
        """
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if row is None or row["confirmation"] != IncidentConfirmation.CONFIRMED.value:
            return None
        return self._row_to_assessment(row)

    def get_confirmed_incidents_since(self, since: datetime) -> list[ConfirmedIncidentSummary]:
        """
        The ONLY permitted way for another module to enumerate CONFIRMED
        Incidents. Never exposes assessment or confirmation history --
        only the four fields ConfirmedIncidentSummary carries.
        """
        with self._core.transaction() as tx:
            rows = tx.fetch_all(
                "SELECT id, trust_domain, rule_group_id, created_at FROM incidents WHERE confirmation = ? AND created_at >= ?",
                (IncidentConfirmation.CONFIRMED.value, _iso(since)),
            )
        return [
            ConfirmedIncidentSummary(
                id=r["id"], trust_domain=r["trust_domain"], rule_group_id=r["rule_group_id"],
                created_at=_parse_iso(r["created_at"]),
            )
            for r in rows
        ]

    # -------------------------------------------------------------------
    # 14.3 — Crash/Restart Recovery
    # -------------------------------------------------------------------

    def get_confirmed_incidents_with_null_assessment(self) -> list[Incident]:
        """Detects the TI23 anomaly (a CONFIRMED Incident whose assessment
        write never completed) -- should be empty going forward under the
        14.2 fix; exists so recover_trust_manager_state() can repair any
        that predate it."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all(
                "SELECT * FROM incidents WHERE confirmation = ? AND assessment_intrinsic_severity IS NULL",
                (IncidentConfirmation.CONFIRMED.value,),
            )
        return [self._row_to_incident(r) for r in rows]

    def recover_trust_manager_state(self, now: datetime) -> int:
        """
        Called from on_system_startup() (system_state_machine.md Section 7)
        as step 1, before Penalty Engine recovery. Idempotent (TI24):
        running this with nothing to repair is a no-op. Returns the
        number of Incidents repaired.
        """
        repaired = 0
        for incident in self.get_confirmed_incidents_with_null_assessment():
            severity = assess_severity(incident.evidence)
            assessment = IncidentAssessment(
                intrinsic_severity=severity,
                confirmation=IncidentConfirmation.CONFIRMED,
                cooperation=CooperationAssessment(),
                evidence=incident.evidence,
                rubric_explanation=f"repaired by recover_trust_manager_state() -> {severity.value}",
            )

            def write(tx: Transaction, _state: object, _incident=incident, _assessment=assessment) -> TrustEvidence:
                self._update_incident_assessment(tx, _incident.id, _assessment)
                return self._insert_trust_evidence_for_incident(tx, _incident, _assessment, now)

            def events(tx: Transaction, _state: object, evidence_row: TrustEvidence) -> None:
                write_event(
                    tx,
                    DomainEvent(
                        event_type="trust_evidence.recorded",
                        source_module="trust_manager",
                        payload={"evidence_id": evidence_row.id, "domain_id": evidence_row.domain_id},
                        occurred_at=now,
                    ),
                )

            apply_transition(self._core, write=write, events=events)
            repaired += 1
        return repaired

    # -------------------------------------------------------------------
    # 15 -- Goal Accountability Assessment integration (Phase 1)
    # -------------------------------------------------------------------

    def record_goal_assessment_evidence(
        self, *, assessment_id: str, trust_domain: str, relevant_to_trust: bool, direction: str,
        rationale: str, now: datetime,
    ) -> TrustEvidence | None:
        """
        Direct entry point (own transaction) with the same semantics as
        the event-driven path; see `_record_goal_assessment_evidence_in_transaction`.
        """
        def write(tx: Transaction, _state: object) -> TrustEvidence | None:
            return self._record_goal_assessment_evidence_in_transaction(
                tx, assessment_id=assessment_id, trust_domain=trust_domain,
                relevant_to_trust=relevant_to_trust, direction=direction, rationale=rationale, now=now,
            )

        return apply_transition(self._core, write=write)

    def _record_goal_assessment_evidence_in_transaction(
        self, tx: Transaction, *, assessment_id: str, trust_domain: str, relevant_to_trust: bool,
        direction: str, rationale: str, now: datetime,
    ) -> TrustEvidence | None:
        """
        TI21/TI25. Writes GOAL_PROGRESS / GOAL_SETBACK evidence ONLY when
        the assessment is relevant to Trust and its direction is
        progress or setback; otherwise (and for an unknown or inactive
        domain) writes nothing and returns None. Idempotent: evidence
        for an assessment that already has it is not duplicated (checked
        before insert, so a redelivery never poisons the transaction).
        On a new row, recalculates the domain immediately in the same
        transaction (triggered_by='goal_assessment') and emits
        `trust_evidence.recorded` and `trust_domain.recalculated`.
        Takes the plain event-payload fields (no call back into Goal
        Management is possible inside consume_event()'s transaction).
        """
        try:
            parsed = GoalAssessmentDirection(direction)
        except ValueError:
            raise ValueError(f"unknown goal assessment direction: {direction!r}") from None
        evidence_type = goal_evidence_type(parsed)
        if not relevant_to_trust or evidence_type is None:
            return None

        domain = tx.fetch_one("SELECT is_active FROM trust_domains WHERE domain_id = ?", (trust_domain,))
        if domain is None or not domain["is_active"]:
            return None

        if tx.fetch_one(
            "SELECT 1 FROM trust_evidence WHERE source_entity_type = ? AND source_entity_id = ? AND evidence_type = ?",
            ("goal_accountability_assessment", assessment_id, evidence_type.value),
        ):
            return None

        evidence = TrustEvidence(
            domain_id=trust_domain, created_at=now, evidence_type=evidence_type,
            source_entity_type="goal_accountability_assessment", source_entity_id=assessment_id,
            raw_weight=goal_evidence_weight(parsed), evidence_confidence=goal_evidence_confidence(parsed),
            explanation=rationale,
        )
        tx.execute(
            """
            INSERT INTO trust_evidence
                (id, domain_id, created_at, evidence_type, source_entity_type, source_entity_id,
                 raw_weight, evidence_confidence, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.id, evidence.domain_id, _iso(now), evidence.evidence_type.value,
                evidence.source_entity_type, evidence.source_entity_id,
                evidence.raw_weight, evidence.evidence_confidence, evidence.explanation,
            ),
        )
        write_event(
            tx,
            DomainEvent(
                event_type="trust_evidence.recorded", source_module="trust_manager",
                payload={"evidence_id": evidence.id, "domain_id": evidence.domain_id},
                occurred_at=now,
            ),
        )
        recalculation = self._recalculate_domain_trust_in_transaction(
            tx, trust_domain, triggered_by="goal_assessment", now=now,
        )
        write_event(
            tx,
            DomainEvent(
                event_type="trust_domain.recalculated", source_module="trust_manager",
                payload={
                    "domain_id": recalculation.domain_id,
                    "previous_score": recalculation.previous_score,
                    "new_score": recalculation.new_score,
                    "previous_confidence": recalculation.previous_confidence,
                    "new_confidence": recalculation.new_confidence,
                    "triggered_by": recalculation.triggered_by,
                },
                occurred_at=now,
            ),
        )
        return evidence

    # -------------------------------------------------------------------
    # v0.39 -- check-in evidence (check_in.missed / check_in.insufficient)
    # -------------------------------------------------------------------

    def record_check_in_evidence(
        self, *, check_in_id: str, trust_domain: str, kind: str, now: datetime, streak: int = 1,
    ) -> TrustEvidence | None:
        def write(tx: Transaction, _state: object) -> TrustEvidence | None:
            return self._record_check_in_evidence_in_transaction(
                tx, check_in_id=check_in_id, trust_domain=trust_domain, kind=kind, now=now, streak=streak,
            )

        return apply_transition(self._core, write=write)

    def _record_check_in_evidence_in_transaction(
        self, tx: Transaction, *, check_in_id: str, trust_domain: str, kind: str, now: datetime, streak: int = 1,
    ) -> TrustEvidence | None:
        """One piece of evidence per check-in and kind (TI25), then an immediate recalculation in the
        same transaction. Unknown or inactive domains write nothing. UNCERTAIN / NOT_EVALUATED verdicts
        never reach this method: only `check_in.missed` and `check_in.insufficient` events do."""
        evidence_type, weight, confidence = checkin_evidence_spec(kind, streak)
        domain = tx.fetch_one("SELECT is_active FROM trust_domains WHERE domain_id = ?", (trust_domain,))
        if domain is None or not domain["is_active"]:
            return None
        if tx.fetch_one(
            "SELECT 1 FROM trust_evidence WHERE source_entity_type = ? AND source_entity_id = ? AND evidence_type = ?",
            ("check_in", check_in_id, evidence_type.value),
        ):
            return None
        evidence = TrustEvidence(
            domain_id=trust_domain, created_at=now, evidence_type=evidence_type, source_entity_type="check_in",
            source_entity_id=check_in_id, raw_weight=weight, evidence_confidence=confidence,
            explanation="Missed a proactive check-in." if kind == "missed"
            else "A check-in answer stayed insufficient after the follow-up window.",
        )
        tx.execute(
            """
            INSERT INTO trust_evidence
                (id, domain_id, created_at, evidence_type, source_entity_type, source_entity_id,
                 raw_weight, evidence_confidence, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (evidence.id, evidence.domain_id, _iso(now), evidence.evidence_type.value, evidence.source_entity_type,
             evidence.source_entity_id, evidence.raw_weight, evidence.evidence_confidence, evidence.explanation),
        )
        write_event(tx, DomainEvent(
            event_type="trust_evidence.recorded", source_module="trust_manager",
            payload={"evidence_id": evidence.id, "domain_id": evidence.domain_id}, occurred_at=now,
        ))
        recalculation = self._recalculate_domain_trust_in_transaction(
            tx, trust_domain, triggered_by="check_in", now=now,
        )
        write_event(tx, DomainEvent(
            event_type="trust_domain.recalculated", source_module="trust_manager",
            payload={
                "domain_id": recalculation.domain_id, "previous_score": recalculation.previous_score,
                "new_score": recalculation.new_score, "previous_confidence": recalculation.previous_confidence,
                "new_confidence": recalculation.new_confidence, "triggered_by": recalculation.triggered_by,
            },
            occurred_at=now,
        ))
        return evidence

    # -------------------------------------------------------------------
    # v0.39 -- Clean Day streak recovery (written by the daily evaluation)
    # -------------------------------------------------------------------

    def _record_clean_streak_bonus_in_transaction(
        self, tx: Transaction, *, user_id: str, local_date: str, trust_domain: str, bonus: float, streak: int, now: datetime,
    ) -> TrustEvidence | None:
        """One positive piece of evidence per user and local date (TI25), then an immediate recalculation in the
        same transaction. Unknown or inactive domains and a zero bonus (Trust already at the cap) write nothing."""
        if bonus <= 0:
            return None
        domain = tx.fetch_one("SELECT is_active FROM trust_domains WHERE domain_id = ?", (trust_domain,))
        if domain is None or not domain["is_active"]:
            return None
        source_id = f"{user_id}:{local_date}"
        if tx.fetch_one(
            "SELECT 1 FROM trust_evidence WHERE source_entity_type = ? AND source_entity_id = ? AND evidence_type = ?",
            ("daily_evaluation", source_id, EvidenceType.CLEAN_STREAK_BONUS.value),
        ):
            return None
        evidence = TrustEvidence(
            domain_id=trust_domain, created_at=now, evidence_type=EvidenceType.CLEAN_STREAK_BONUS,
            source_entity_type="daily_evaluation", source_entity_id=source_id, raw_weight=bonus, evidence_confidence=1.0,
            explanation=f"Clean day number {streak} in a row.",
        )
        tx.execute(
            """
            INSERT INTO trust_evidence
                (id, domain_id, created_at, evidence_type, source_entity_type, source_entity_id,
                 raw_weight, evidence_confidence, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (evidence.id, evidence.domain_id, _iso(now), evidence.evidence_type.value, evidence.source_entity_type,
             evidence.source_entity_id, evidence.raw_weight, evidence.evidence_confidence, evidence.explanation),
        )
        write_event(tx, DomainEvent(
            event_type="trust_evidence.recorded", source_module="trust_manager",
            payload={"evidence_id": evidence.id, "domain_id": evidence.domain_id}, occurred_at=now,
        ))
        recalculation = self._recalculate_domain_trust_in_transaction(tx, trust_domain, triggered_by="clean_streak", now=now)
        write_event(tx, DomainEvent(
            event_type="trust_domain.recalculated", source_module="trust_manager",
            payload={
                "domain_id": recalculation.domain_id, "previous_score": recalculation.previous_score,
                "new_score": recalculation.new_score, "previous_confidence": recalculation.previous_confidence,
                "new_confidence": recalculation.new_confidence, "triggered_by": recalculation.triggered_by,
            },
            occurred_at=now,
        ))
        return evidence

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _select_unconsumed_evidence(tx: Transaction, domain_id: str) -> list[TrustEvidence]:
        """Evidence not yet referenced by any trust_recalculation_evidence
        row -- TI4's UNIQUE(evidence_id) is what guarantees a row selected
        here can never be selected again by a later recalculation."""
        rows = tx.fetch_all(
            """
            SELECT te.* FROM trust_evidence te
            WHERE te.domain_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM trust_recalculation_evidence tre WHERE tre.evidence_id = te.id
              )
            ORDER BY te.created_at
            """,
            (domain_id,),
        )
        return [TrustManager._row_to_trust_evidence(r) for r in rows]

    @staticmethod
    def _select_applied_evidence_in_window(tx: Transaction, domain_id: str, window_start: datetime) -> list[TrustEvidence]:
        """3.6: confidence is computed over evidence that has actually
        been applied (consumed by some recalculation, ever) and whose
        created_at falls within the rolling window -- not merely
        'evidence that exists', which would let never-consumed evidence
        (e.g. still awaiting a future recalculation) inflate confidence
        before it has actually informed the score."""
        rows = tx.fetch_all(
            """
            SELECT te.* FROM trust_evidence te
            JOIN trust_recalculation_evidence tre ON tre.evidence_id = te.id
            WHERE te.domain_id = ? AND te.created_at >= ?
            """,
            (domain_id, _iso(window_start)),
        )
        return [TrustManager._row_to_trust_evidence(r) for r in rows]

    @staticmethod
    def _row_to_trust_evidence(row) -> TrustEvidence:
        return TrustEvidence(
            id=row["id"],
            domain_id=row["domain_id"],
            created_at=_parse_iso(row["created_at"]),
            evidence_type=EvidenceType(row["evidence_type"]),
            source_entity_type=row["source_entity_type"],
            source_entity_id=row["source_entity_id"],
            raw_weight=row["raw_weight"],
            evidence_confidence=row["evidence_confidence"],
            explanation=row["explanation"],
        )

    @staticmethod
    def _insert_incident_row(tx: Transaction, incident: Incident) -> None:
        ev = incident.evidence
        tx.execute(
            """
            INSERT INTO incidents (
                id, created_at, rule_group_id, trust_domain, confirmation, description,
                evidence_impact, evidence_intentionality, evidence_breach_directness,
                evidence_confidence, evidence_repetition_count, evidence_repetition_window_days,
                evidence_repetition_source_ids_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident.id, _iso(incident.created_at), incident.rule_group_id, incident.trust_domain,
                incident.confirmation.value, incident.description,
                ev.actual_or_potential_impact.value, ev.intentionality.value, ev.rule_breach_directness.value,
                ev.evidence_confidence.value, ev.repetition.same_rule_confirmed_count,
                ev.repetition.evaluation_window_days, json.dumps(list(ev.repetition.source_incident_ids)),
            ),
        )

    @staticmethod
    def _load_incident(tx: Transaction, incident_id: str) -> Incident:
        row = tx.fetch_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if row is None:
            raise IncidentNotFoundError(incident_id)
        return TrustManager._row_to_incident(row)

    @staticmethod
    def _row_to_incident(row) -> Incident:
        evidence = IncidentEvidence(
            actual_or_potential_impact=ImpactLevel(row["evidence_impact"]),
            intentionality=IntentAssessment(row["evidence_intentionality"]),
            rule_breach_directness=BreachDirectness(row["evidence_breach_directness"]),
            evidence_confidence=EvidenceConfidenceLevel(row["evidence_confidence"]),
            repetition=RepetitionEvidence(
                same_rule_confirmed_count=row["evidence_repetition_count"],
                evaluation_window_days=row["evidence_repetition_window_days"],
                source_incident_ids=tuple(json.loads(row["evidence_repetition_source_ids_json"])),
            ),
        )
        assessment = None
        if row["assessment_intrinsic_severity"] is not None:
            assessment = TrustManager._row_to_assessment(row, evidence=evidence)
        return Incident(
            id=row["id"],
            created_at=_parse_iso(row["created_at"]),
            rule_group_id=row["rule_group_id"],
            trust_domain=row["trust_domain"],
            description=row["description"],
            evidence=evidence,
            confirmation=IncidentConfirmation(row["confirmation"]),
            assessment=assessment,
        )

    @staticmethod
    def _row_to_assessment(row, evidence: IncidentEvidence | None = None) -> IncidentAssessment:
        if evidence is None:
            evidence = IncidentEvidence(
                actual_or_potential_impact=ImpactLevel(row["evidence_impact"]),
                intentionality=IntentAssessment(row["evidence_intentionality"]),
                rule_breach_directness=BreachDirectness(row["evidence_breach_directness"]),
                evidence_confidence=EvidenceConfidenceLevel(row["evidence_confidence"]),
                repetition=RepetitionEvidence(
                    same_rule_confirmed_count=row["evidence_repetition_count"],
                    evaluation_window_days=row["evidence_repetition_window_days"],
                    source_incident_ids=tuple(json.loads(row["evidence_repetition_source_ids_json"])),
                ),
            )
        return IncidentAssessment(
            intrinsic_severity=SeverityTier(row["assessment_intrinsic_severity"]),
            confirmation=IncidentConfirmation(row["confirmation"]),
            cooperation=CooperationAssessment(
                self_disclosed=bool(row["assessment_cooperation_self_disclosed"]),
                active_cooperation_in_resolution=bool(row["assessment_cooperation_active_resolution"]),
                notes=row["assessment_cooperation_notes"],
            ),
            evidence=evidence,
            rubric_explanation=row["assessment_rubric_explanation"],
        )

    @staticmethod
    def _update_incident_assessment(tx: Transaction, incident_id: str, assessment: IncidentAssessment) -> None:
        tx.execute(
            """
            UPDATE incidents SET
                assessment_intrinsic_severity = ?,
                assessment_cooperation_self_disclosed = ?,
                assessment_cooperation_active_resolution = ?,
                assessment_cooperation_notes = ?,
                assessment_rubric_explanation = ?
            WHERE id = ?
            """,
            (
                assessment.intrinsic_severity.value,
                int(assessment.cooperation.self_disclosed),
                int(assessment.cooperation.active_cooperation_in_resolution),
                assessment.cooperation.notes,
                assessment.rubric_explanation,
                incident_id,
            ),
        )

    @staticmethod
    def _insert_trust_evidence_for_incident(
        tx: Transaction, incident: Incident, assessment: IncidentAssessment, now: datetime
    ) -> TrustEvidence:
        raw_weight = raw_weight_for_incident(assessment.intrinsic_severity, assessment.cooperation)
        evidence_confidence_numeric = {"low": 0.3, "medium": 0.65, "high": 0.9}[incident.evidence.evidence_confidence.value]
        evidence_row = TrustEvidence(
            domain_id=incident.trust_domain,
            created_at=now,
            evidence_type=EvidenceType.INCIDENT_IMPACT,
            source_entity_type="incident",
            source_entity_id=incident.id,
            raw_weight=raw_weight,
            evidence_confidence=evidence_confidence_numeric,
            explanation=assessment.rubric_explanation,
        )
        tx.execute(
            """
            INSERT INTO trust_evidence
                (id, domain_id, created_at, evidence_type, source_entity_type, source_entity_id,
                 raw_weight, evidence_confidence, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_row.id, evidence_row.domain_id, _iso(now), evidence_row.evidence_type.value,
                evidence_row.source_entity_type, evidence_row.source_entity_id,
                evidence_row.raw_weight, evidence_row.evidence_confidence, evidence_row.explanation,
            ),
        )
        return evidence_row


class IncidentNotFoundError(LookupError):
    def __init__(self, incident_id: str) -> None:
        super().__init__(f"No Incident with id={incident_id!r}")
        self.incident_id = incident_id
