"""
goal_management/repository.py

Goal Management — built on infrastructure.database.Database, the same
composition pattern every other repository in this system uses.

GOAL-1 (structural): this file imports nothing from trust_manager or
penalty_engine. Goal Management reads nothing from the Trust Manager
for any decision of its own, and never writes to penalty_windows,
freeze_periods, or incidents (Section 1) — there is no code path here
by which it could.

Canonical spec: docs/architecture/goal_technical_design.md.
See goal_management/README.md for exactly what this slice covers, two
real gaps found in the architecture document while implementing it,
and how each was resolved.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from goal_management.models import (
    AssessmentAlreadyExistsError,
    Goal,
    GoalAccountabilityAssessment,
    GoalAccountabilityDirection,
    GoalChangeProposal,
    GoalChangeProposalContent,
    GoalChangeProposalNotFoundError,
    GoalEvaluation,
    GoalEvaluationNotFoundError,
    GoalEvidence,
    GoalInterventionType,
    GoalLifecycleStatus,
    GoalNotFoundError,
    GoalOutcome,
    GoalProposalStatus,
    GoalVersion,
    InvalidGoalTransitionError,
    InvalidProposalStateError,
    derive_accountability_direction,
    new_id,
)
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso

_TERMINAL_STATUSES = (GoalLifecycleStatus.COMPLETED, GoalLifecycleStatus.ABANDONED, GoalLifecycleStatus.REPLACED)


class GoalManager:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    # -------------------------------------------------------------------
    # 2.3 — Creation. Exempt from GOAL-6 (only "beyond the first"
    # GoalVersion, and terminal transitions, require a GoalChangeProposal).
    # -------------------------------------------------------------------

    def create_goal(
        self, title: str, target_description: str, trust_domain: str, created_via: str, *, now: datetime,
    ) -> Goal:
        version_id = new_id()  # pre-generated -- see migration 010's comment on why goals has no FK to goal_versions

        def write(tx: Transaction, _state: object) -> Goal:
            goal = Goal(
                current_version_id=version_id, status=GoalLifecycleStatus.ACTIVE,
                created_at=now, status_changed_at=now,
            )
            tx.execute(
                """
                INSERT INTO goals (goal_group_id, current_version_id, status, created_at, status_changed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (goal.goal_group_id, version_id, goal.status.value, _iso(now), _iso(now)),
            )
            version = GoalVersion(
                id=version_id, goal_group_id=goal.goal_group_id, version=1,
                title=title, target_description=target_description, trust_domain=trust_domain,
                created_at=now, created_via=created_via,
            )
            self._insert_version(tx, version)
            return goal

        def events(tx: Transaction, _state: object, result: Goal) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="goal.created", source_module="goal_management",
                    payload={"goal_group_id": result.goal_group_id, "goal_version_id": version_id, "trust_domain": trust_domain},
                    occurred_at=now,
                ),
            )

        return apply_transition(self._core, write=write, events=events)

    # -------------------------------------------------------------------
    # 3.2 — Direct lifecycle transitions (pause/resume/complete), NOT
    # gated by a GoalChangeProposal. See goal_management/README.md for
    # why `complete_goal()` is direct despite GOAL-6's literal wording
    # (a real gap in the architecture document: GoalInterventionType has
    # no value for proposing completion).
    # -------------------------------------------------------------------

    def pause_goal(self, goal_group_id: str, reason: str, *, now: datetime) -> None:
        self._transition_goal(goal_group_id, GoalLifecycleStatus.PAUSED, "pause",
                               allowed_from=(GoalLifecycleStatus.ACTIVE,), event_type="goal.paused",
                               extra_payload={"reason": reason}, now=now)

    def resume_goal(self, goal_group_id: str, *, now: datetime) -> None:
        self._transition_goal(goal_group_id, GoalLifecycleStatus.ACTIVE, "resume",
                               allowed_from=(GoalLifecycleStatus.PAUSED,), event_type="goal.resumed",
                               extra_payload={}, now=now)

    def complete_goal(self, goal_group_id: str, reason: str, *, now: datetime) -> None:
        self._transition_goal(goal_group_id, GoalLifecycleStatus.COMPLETED, "complete",
                               allowed_from=(GoalLifecycleStatus.ACTIVE, GoalLifecycleStatus.PAUSED),
                               event_type="goal.completed", extra_payload={"reason": reason}, now=now)

    def _transition_goal(
        self, goal_group_id: str, new_status: GoalLifecycleStatus, action: str,
        *, allowed_from: tuple[GoalLifecycleStatus, ...], event_type: str, extra_payload: dict, now: datetime,
    ) -> None:
        def write(tx: Transaction, _state: object) -> None:
            row = tx.fetch_one("SELECT * FROM goals WHERE goal_group_id = ?", (goal_group_id,))
            if row is None:
                raise GoalNotFoundError(goal_group_id)
            current_status = GoalLifecycleStatus(row["status"])
            if current_status not in allowed_from:
                raise InvalidGoalTransitionError(
                    goal_group_id, current_status.value, action, tuple(s.value for s in allowed_from),
                )
            tx.execute(
                "UPDATE goals SET status = ?, status_changed_at = ? WHERE goal_group_id = ?",
                (new_status.value, _iso(now), goal_group_id),
            )

        def events(tx: Transaction, _state: object, _result: None) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type=event_type, source_module="goal_management",
                    payload={"goal_group_id": goal_group_id, **extra_payload},
                    occurred_at=now,
                ),
            )

        apply_transition(self._core, write=write, events=events)

    def archive_goal(self, goal_group_id: str, *, now: datetime) -> None:
        """GOAL-11: requires Goal.status to already be terminal. Never
        changes status; has no behavioral effect anywhere else in the
        system -- visibility only. Not gated by a GoalChangeProposal
        (3.3): archiving changes no content and no meaning."""
        def write(tx: Transaction, _state: object) -> None:
            row = tx.fetch_one("SELECT * FROM goals WHERE goal_group_id = ?", (goal_group_id,))
            if row is None:
                raise GoalNotFoundError(goal_group_id)
            current_status = GoalLifecycleStatus(row["status"])
            if current_status not in _TERMINAL_STATUSES:
                raise InvalidGoalTransitionError(
                    goal_group_id, current_status.value, "archive", tuple(s.value for s in _TERMINAL_STATUSES),
                )
            tx.execute("UPDATE goals SET archived_at = ? WHERE goal_group_id = ?", (_iso(now), goal_group_id))

        def events(tx: Transaction, _state: object, _result: None) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="goal.archived", source_module="goal_management",
                    payload={"goal_group_id": goal_group_id}, occurred_at=now,
                ),
            )

        apply_transition(self._core, write=write, events=events)

    # -------------------------------------------------------------------
    # 4.1, 5.1 — Evidence and Evaluation (append-only observations)
    # -------------------------------------------------------------------

    def record_evidence(
        self, goal_group_id: str, goal_version_id: str, period_start: datetime, period_end: datetime,
        outcome: GoalOutcome, observed_progress: str, source: str, *, now: datetime,
    ) -> GoalEvidence:
        """GOAL-2: recording evidence never itself triggers a
        GoalEvaluation, a Trust effect, or a lifecycle transition --
        this function does none of those things, by construction (it
        has no code path to any of them)."""
        evidence = GoalEvidence(
            goal_group_id=goal_group_id, goal_version_id=goal_version_id,
            period_start=period_start, period_end=period_end, outcome=outcome,
            observed_progress=observed_progress, source=source, created_at=now,
        )

        def write(tx: Transaction, _state: object) -> GoalEvidence:
            tx.execute(
                """
                INSERT INTO goal_evidence
                    (id, goal_group_id, goal_version_id, period_start, period_end, outcome, observed_progress, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (evidence.id, goal_group_id, goal_version_id, _iso(period_start), _iso(period_end),
                 outcome.value, observed_progress, source, _iso(now)),
            )
            return evidence

        def events(tx: Transaction, _state: object, result: GoalEvidence) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="goal_evidence.recorded", source_module="goal_management",
                    payload={"goal_evidence_id": result.id, "goal_group_id": goal_group_id, "outcome": outcome.value},
                    occurred_at=now,
                ),
            )

        return apply_transition(self._core, write=write, events=events)

    def record_evaluation(
        self, goal_group_id: str, triggering_evidence_ids: tuple[str, ...], findings: str,
        proposed_intervention: GoalInterventionType, proposed_intervention_detail: str, *, now: datetime,
    ) -> GoalEvaluation:
        """GOAL-3: triggering_evidence_ids must be non-empty."""
        if not triggering_evidence_ids:
            raise ValueError("triggering_evidence_ids must be non-empty (GOAL-3)")

        evaluation = GoalEvaluation(
            goal_group_id=goal_group_id, created_at=now, triggering_evidence_ids=triggering_evidence_ids,
            findings=findings, proposed_intervention=proposed_intervention,
            proposed_intervention_detail=proposed_intervention_detail,
        )

        def write(tx: Transaction, _state: object) -> GoalEvaluation:
            tx.execute(
                """
                INSERT INTO goal_evaluations
                    (id, goal_group_id, created_at, triggering_evidence_ids_json, findings, proposed_intervention, proposed_intervention_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (evaluation.id, goal_group_id, _iso(now), json.dumps(list(triggering_evidence_ids)),
                 findings, proposed_intervention.value, proposed_intervention_detail),
            )
            return evaluation

        def events(tx: Transaction, _state: object, result: GoalEvaluation) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="goal_evaluation.recorded", source_module="goal_management",
                    payload={"goal_evaluation_id": result.id, "goal_group_id": goal_group_id,
                             "proposed_intervention": proposed_intervention.value},
                    occurred_at=now,
                ),
            )

        return apply_transition(self._core, write=write, events=events)

    # -------------------------------------------------------------------
    # 6 -- GoalAccountabilityAssessment (Phase 1: the Goal -> Trust bridge)
    # -------------------------------------------------------------------

    def record_accountability_assessment(
        self, evaluation_id: str, *, relevant_to_trust: bool, rationale: str, now: datetime,
        direction: GoalAccountabilityDirection | None = None,
    ) -> GoalAccountabilityAssessment:
        """
        Domain-service entry point only; no model or Discord path calls
        this. The direction is derived from the outcomes of the
        evaluation's triggering GoalEvidence (all MET -> PROGRESS, all
        MISSED -> SETBACK, otherwise NEUTRAL). A caller may pass an
        explicit direction only to NEUTRALISE (e.g. an excused outcome);
        it can never assert PROGRESS/SETBACK that the evidence does not
        support. GOAL-13: one assessment per evaluation. Always emits
        `goal_accountability_assessment.recorded`, relevant or not --
        whether Trust acts on it is the Trust Manager's decision.
        """
        if not rationale or not rationale.strip():
            raise ValueError("rationale must be non-empty")

        def write(tx: Transaction, _state: object) -> GoalAccountabilityAssessment:
            ev_row = tx.fetch_one("SELECT * FROM goal_evaluations WHERE id = ?", (evaluation_id,))
            if ev_row is None:
                raise GoalEvaluationNotFoundError(evaluation_id)
            if tx.fetch_one("SELECT 1 FROM goal_accountability_assessments WHERE evaluation_id = ?", (evaluation_id,)):
                raise AssessmentAlreadyExistsError(evaluation_id)
            evidence_ids = json.loads(ev_row["triggering_evidence_ids_json"])
            outcomes = tuple(
                GoalOutcome(tx.fetch_one("SELECT outcome FROM goal_evidence WHERE id = ?", (eid,))["outcome"])
                for eid in evidence_ids
            )
            derived = derive_accountability_direction(outcomes)
            resolved = derived if direction is None else direction
            if resolved != derived and resolved != GoalAccountabilityDirection.NEUTRAL:
                raise ValueError(
                    f"direction {resolved.value!r} is not supported by the evaluation's evidence "
                    f"(derived: {derived.value!r}); a caller may only neutralise"
                )
            goal_group_id = ev_row["goal_group_id"]
            domain_row = tx.fetch_one(
                "SELECT gv.trust_domain FROM goals g JOIN goal_versions gv ON gv.id = g.current_version_id "
                "WHERE g.goal_group_id = ?", (goal_group_id,),
            )
            if domain_row is None:
                raise GoalNotFoundError(goal_group_id)
            assessment = GoalAccountabilityAssessment(
                evaluation_id=evaluation_id, goal_group_id=goal_group_id, trust_domain=domain_row["trust_domain"],
                created_at=now, relevant_to_trust=relevant_to_trust, direction=resolved, rationale=rationale,
            )
            tx.execute(
                """
                INSERT INTO goal_accountability_assessments
                    (id, evaluation_id, goal_group_id, trust_domain, created_at, relevant_to_trust, direction, rationale)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (assessment.id, evaluation_id, goal_group_id, assessment.trust_domain, _iso(now),
                 1 if relevant_to_trust else 0, resolved.value, rationale),
            )
            return assessment

        def events(tx: Transaction, _state: object, result: GoalAccountabilityAssessment) -> None:
            # Self-contained payload: the Trust Manager consumer runs inside
            # consume_event()'s transaction and cannot call back into this module.
            write_event(
                tx,
                DomainEvent(
                    event_type="goal_accountability_assessment.recorded", source_module="goal_management",
                    payload={
                        "assessment_id": result.id, "evaluation_id": result.evaluation_id,
                        "goal_group_id": result.goal_group_id, "trust_domain": result.trust_domain,
                        "relevant_to_trust": result.relevant_to_trust, "direction": result.direction.value,
                        "rationale": result.rationale,
                    },
                    occurred_at=now,
                ),
            )

        return apply_transition(self._core, write=write, events=events)

    def get_accountability_assessment(self, assessment_id: str) -> GoalAccountabilityAssessment | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM goal_accountability_assessments WHERE id = ?", (assessment_id,))
        if row is None:
            return None
        return GoalAccountabilityAssessment(
            id=row["id"], evaluation_id=row["evaluation_id"], goal_group_id=row["goal_group_id"],
            trust_domain=row["trust_domain"], created_at=_parse_iso(row["created_at"]),
            relevant_to_trust=bool(row["relevant_to_trust"]),
            direction=GoalAccountabilityDirection(row["direction"]), rationale=row["rationale"],
        )

    # -------------------------------------------------------------------
    # 5.3 — GoalChangeProposal / GoalChangeProposalContent (GOAL-6)
    # -------------------------------------------------------------------

    def create_change_proposal(
        self, goal_group_id: str, proposed_change: GoalInterventionType, reason: str, proposal_expires_at: datetime,
        *, evaluation_id: str | None = None, proposed_title: str | None = None,
        proposed_target_description: str | None = None, proposed_replacement_goal_group_id: str | None = None,
        now: datetime,
    ) -> GoalChangeProposal:
        proposal = GoalChangeProposal(
            evaluation_id=evaluation_id, goal_group_id=goal_group_id, proposed_change=proposed_change,
            proposal_expires_at=proposal_expires_at, created_at=now,
        )
        content = GoalChangeProposalContent(
            proposal_id=proposal.id, proposed_title=proposed_title,
            proposed_target_description=proposed_target_description,
            proposed_replacement_goal_group_id=proposed_replacement_goal_group_id, reason=reason,
        )

        def write(tx: Transaction, _state: object) -> GoalChangeProposal:
            tx.execute(
                """
                INSERT INTO goal_change_proposals
                    (id, evaluation_id, goal_group_id, proposed_change, proposal_expires_at, status, created_at, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (proposal.id, evaluation_id, goal_group_id, proposed_change.value,
                 _iso(proposal_expires_at), proposal.status.value, _iso(now)),
            )
            tx.execute(
                """
                INSERT INTO goal_change_proposal_contents
                    (id, proposal_id, proposed_title, proposed_target_description, proposed_replacement_goal_group_id, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (content.id, proposal.id, proposed_title, proposed_target_description,
                 proposed_replacement_goal_group_id, reason),
            )
            return proposal

        def events(tx: Transaction, _state: object, result: GoalChangeProposal) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="goal_change_proposal.created", source_module="goal_management",
                    payload={"goal_change_proposal_id": result.id, "goal_group_id": goal_group_id,
                             "proposed_change": proposed_change.value},
                    occurred_at=now,
                ),
            )

        return apply_transition(self._core, write=write, events=events)

    def accept_proposal(self, proposal_id: str, *, now: datetime) -> None:
        """
        GOAL-6: applies the exact, immutable GoalChangeProposalContent
        recorded at proposal time -- never content reconstructed from
        context at acceptance time. Dispatches to the matching
        lifecycle/version effect based on `proposed_change`;
        INCREASE_SUPPORT/NO_CHANGE have no effect on the Goal itself
        beyond resolving the proposal (5.2).
        """
        def write(tx: Transaction, _state: object) -> tuple[GoalInterventionType, str]:
            proposal_row = tx.fetch_one("SELECT * FROM goal_change_proposals WHERE id = ?", (proposal_id,))
            if proposal_row is None:
                raise GoalChangeProposalNotFoundError(proposal_id)
            status = GoalProposalStatus(proposal_row["status"])
            if status != GoalProposalStatus.PENDING:
                raise InvalidProposalStateError(proposal_id, status.value)

            content_row = tx.fetch_one("SELECT * FROM goal_change_proposal_contents WHERE proposal_id = ?", (proposal_id,))
            goal_group_id = proposal_row["goal_group_id"]
            proposed_change = GoalInterventionType(proposal_row["proposed_change"])

            tx.execute(
                "UPDATE goal_change_proposals SET status = ?, resolved_at = ? WHERE id = ?",
                (GoalProposalStatus.ACCEPTED.value, _iso(now), proposal_id),
            )

            if proposed_change == GoalInterventionType.ADAPT_TARGET:
                self._apply_adaptation_in_transaction(tx, goal_group_id, content_row, now)
            elif proposed_change == GoalInterventionType.PROPOSE_REPLACEMENT:
                self._apply_replacement_in_transaction(tx, goal_group_id, content_row, now)
            elif proposed_change == GoalInterventionType.PROPOSE_ABANDONMENT:
                self._apply_abandonment_in_transaction(tx, goal_group_id, now)
            # INCREASE_SUPPORT / NO_CHANGE: no further effect (5.2).

            return proposed_change, goal_group_id

        def events(tx: Transaction, _state: object, result: tuple[GoalInterventionType, str]) -> None:
            proposed_change, goal_group_id = result
            write_event(
                tx,
                DomainEvent(
                    event_type="goal_change_proposal.resolved", source_module="goal_management",
                    payload={"goal_change_proposal_id": proposal_id, "goal_group_id": goal_group_id, "resolution": "accepted"},
                    occurred_at=now,
                ),
            )

        apply_transition(self._core, write=write, events=events)

    def decline_proposal(self, proposal_id: str, *, now: datetime) -> None:
        self._resolve_proposal_without_effect(proposal_id, GoalProposalStatus.DECLINED, "declined", now)

    def _resolve_proposal_without_effect(
        self, proposal_id: str, new_status: GoalProposalStatus, resolution_label: str, now: datetime,
    ) -> None:
        def write(tx: Transaction, _state: object) -> str:
            row = tx.fetch_one("SELECT * FROM goal_change_proposals WHERE id = ?", (proposal_id,))
            if row is None:
                raise GoalChangeProposalNotFoundError(proposal_id)
            status = GoalProposalStatus(row["status"])
            if status != GoalProposalStatus.PENDING:
                raise InvalidProposalStateError(proposal_id, status.value)
            tx.execute(
                "UPDATE goal_change_proposals SET status = ?, resolved_at = ? WHERE id = ?",
                (new_status.value, _iso(now), proposal_id),
            )
            return row["goal_group_id"]

        def events(tx: Transaction, _state: object, goal_group_id: str) -> None:
            write_event(
                tx,
                DomainEvent(
                    event_type="goal_change_proposal.resolved", source_module="goal_management",
                    payload={"goal_change_proposal_id": proposal_id, "goal_group_id": goal_group_id, "resolution": resolution_label},
                    occurred_at=now,
                ),
            )

        apply_transition(self._core, write=write, events=events)

    # -------------------------------------------------------------------
    # Internal effects — reachable ONLY via accept_proposal() (GOAL-6)
    # -------------------------------------------------------------------

    def _apply_adaptation_in_transaction(self, tx: Transaction, goal_group_id: str, content_row, now: datetime) -> GoalVersion:
        goal_row = tx.fetch_one("SELECT * FROM goals WHERE goal_group_id = ?", (goal_group_id,))
        if goal_row is None:
            raise GoalNotFoundError(goal_group_id)
        current_status = GoalLifecycleStatus(goal_row["status"])
        # The lifecycle diagram (3.1) draws the adapt() loop only on ACTIVE.
        if current_status != GoalLifecycleStatus.ACTIVE:
            raise InvalidGoalTransitionError(goal_group_id, current_status.value, "adapt", (GoalLifecycleStatus.ACTIVE.value,))

        current_version_row = tx.fetch_one("SELECT * FROM goal_versions WHERE id = ?", (goal_row["current_version_id"],))
        new_version = GoalVersion(
            goal_group_id=goal_group_id, version=current_version_row["version"] + 1,
            title=content_row["proposed_title"] or current_version_row["title"],
            target_description=content_row["proposed_target_description"] or current_version_row["target_description"],
            trust_domain=current_version_row["trust_domain"],  # fixed at creation (11.1) -- never changed by adaptation
            created_at=now, created_via="coach_proposed_user_approved",
            adaptation_reason=content_row["reason"], supersedes_id=current_version_row["id"],
        )
        self._insert_version(tx, new_version)
        tx.execute("UPDATE goals SET current_version_id = ? WHERE goal_group_id = ?", (new_version.id, goal_group_id))
        write_event(
            tx,
            DomainEvent(
                event_type="goal.adapted", source_module="goal_management",
                payload={"goal_group_id": goal_group_id, "goal_version_id": new_version.id, "reason": content_row["reason"]},
                occurred_at=now,
            ),
        )
        return new_version

    def _apply_replacement_in_transaction(self, tx: Transaction, goal_group_id: str, content_row, now: datetime) -> Goal:
        goal_row = tx.fetch_one("SELECT * FROM goals WHERE goal_group_id = ?", (goal_group_id,))
        if goal_row is None:
            raise GoalNotFoundError(goal_group_id)
        current_status = GoalLifecycleStatus(goal_row["status"])
        allowed = (GoalLifecycleStatus.ACTIVE, GoalLifecycleStatus.PAUSED)
        if current_status not in allowed:
            raise InvalidGoalTransitionError(goal_group_id, current_status.value, "replace", tuple(s.value for s in allowed))

        tx.execute(
            "UPDATE goals SET status = ?, status_changed_at = ? WHERE goal_group_id = ?",
            (GoalLifecycleStatus.REPLACED.value, _iso(now), goal_group_id),
        )

        current_version_row = tx.fetch_one("SELECT * FROM goal_versions WHERE id = ?", (goal_row["current_version_id"],))
        # 2.4/5.3: trust_domain is deliberately absent from
        # GoalChangeProposalContent (changing it is out of scope) --
        # the replacement inherits the original's trust_domain.
        new_version_id = new_id()
        replacement = Goal(
            current_version_id=new_version_id, status=GoalLifecycleStatus.ACTIVE,
            created_at=now, status_changed_at=now, replaces_goal_group_id=goal_group_id,
        )
        tx.execute(
            """
            INSERT INTO goals (goal_group_id, current_version_id, status, created_at, status_changed_at, replaces_goal_group_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (replacement.goal_group_id, new_version_id, replacement.status.value, _iso(now), _iso(now), goal_group_id),
        )
        new_version = GoalVersion(
            id=new_version_id, goal_group_id=replacement.goal_group_id, version=1,
            title=content_row["proposed_title"] or current_version_row["title"],
            target_description=content_row["proposed_target_description"] or current_version_row["target_description"],
            trust_domain=current_version_row["trust_domain"],
            created_at=now, created_via="coach_proposed_user_approved",
        )
        self._insert_version(tx, new_version)

        write_event(
            tx,
            DomainEvent(
                event_type="goal.replaced", source_module="goal_management",
                payload={"goal_group_id": goal_group_id, "replacement_goal_group_id": replacement.goal_group_id},
                occurred_at=now,
            ),
        )
        write_event(
            tx,
            DomainEvent(
                event_type="goal.created", source_module="goal_management",
                payload={"goal_group_id": replacement.goal_group_id, "goal_version_id": new_version_id,
                         "trust_domain": new_version.trust_domain},
                occurred_at=now,
            ),
        )
        return replacement

    def _apply_abandonment_in_transaction(self, tx: Transaction, goal_group_id: str, now: datetime) -> None:
        goal_row = tx.fetch_one("SELECT * FROM goals WHERE goal_group_id = ?", (goal_group_id,))
        if goal_row is None:
            raise GoalNotFoundError(goal_group_id)
        current_status = GoalLifecycleStatus(goal_row["status"])
        allowed = (GoalLifecycleStatus.ACTIVE, GoalLifecycleStatus.PAUSED)
        if current_status not in allowed:
            raise InvalidGoalTransitionError(goal_group_id, current_status.value, "abandon", tuple(s.value for s in allowed))
        tx.execute(
            "UPDATE goals SET status = ?, status_changed_at = ? WHERE goal_group_id = ?",
            (GoalLifecycleStatus.ABANDONED.value, _iso(now), goal_group_id),
        )
        write_event(
            tx,
            DomainEvent(
                event_type="goal.abandoned", source_module="goal_management",
                payload={"goal_group_id": goal_group_id}, occurred_at=now,
            ),
        )

    # -------------------------------------------------------------------
    # 9.3 — Startup Reconciliation
    # -------------------------------------------------------------------

    def recover_goal_management_state(self, now: datetime) -> list[str]:
        """
        Called from on_system_startup(), inside the same
        system_startup_lease as every other module's recovery step.
        Expires any PENDING GoalChangeProposal past its
        proposal_expires_at; leaves everything else untouched (9.2:
        this module has no other non-terminal, multi-step state to
        reconcile). Returns the list of proposal_ids expired.
        """
        with self._core.transaction() as tx:
            pending = tx.fetch_all(
                "SELECT id FROM goal_change_proposals WHERE status = ? AND proposal_expires_at <= ?",
                (GoalProposalStatus.PENDING.value, _iso(now)),
            )
        expired_ids = []
        for row in pending:
            self._resolve_proposal_without_effect(row["id"], GoalProposalStatus.EXPIRED, "expired", now)
            expired_ids.append(row["id"])
        return expired_ids

    # -------------------------------------------------------------------
    # Reads
    # -------------------------------------------------------------------

    def get_goal(self, goal_group_id: str) -> Goal | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM goals WHERE goal_group_id = ?", (goal_group_id,))
        return self._row_to_goal(row) if row else None

    def get_goal_version(self, version_id: str) -> GoalVersion | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM goal_versions WHERE id = ?", (version_id,))
        return self._row_to_version(row) if row else None

    def get_change_proposal(self, proposal_id: str) -> GoalChangeProposal | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM goal_change_proposals WHERE id = ?", (proposal_id,))
        return self._row_to_proposal(row) if row else None

    def get_change_proposal_content(self, proposal_id: str) -> GoalChangeProposalContent | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM goal_change_proposal_contents WHERE proposal_id = ?", (proposal_id,))
        if row is None:
            return None
        return GoalChangeProposalContent(
            id=row["id"], proposal_id=row["proposal_id"], proposed_title=row["proposed_title"],
            proposed_target_description=row["proposed_target_description"],
            proposed_replacement_goal_group_id=row["proposed_replacement_goal_group_id"], reason=row["reason"],
        )

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _insert_version(tx: Transaction, version: GoalVersion) -> None:
        tx.execute(
            """
            INSERT INTO goal_versions
                (id, goal_group_id, version, title, target_description, trust_domain, created_at, created_via, adaptation_reason, supersedes_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (version.id, version.goal_group_id, version.version, version.title, version.target_description,
             version.trust_domain, _iso(version.created_at), version.created_via, version.adaptation_reason, version.supersedes_id),
        )

    @staticmethod
    def _row_to_goal(row) -> Goal:
        return Goal(
            goal_group_id=row["goal_group_id"], current_version_id=row["current_version_id"],
            status=GoalLifecycleStatus(row["status"]), created_at=_parse_iso(row["created_at"]),
            status_changed_at=_parse_iso(row["status_changed_at"]),
            replaces_goal_group_id=row["replaces_goal_group_id"],
            archived_at=_parse_iso(row["archived_at"]) if row["archived_at"] else None,
        )

    @staticmethod
    def _row_to_version(row) -> GoalVersion:
        return GoalVersion(
            id=row["id"], goal_group_id=row["goal_group_id"], version=row["version"],
            title=row["title"], target_description=row["target_description"], trust_domain=row["trust_domain"],
            created_at=_parse_iso(row["created_at"]), created_via=row["created_via"],
            adaptation_reason=row["adaptation_reason"], supersedes_id=row["supersedes_id"],
        )

    @staticmethod
    def _row_to_proposal(row) -> GoalChangeProposal:
        return GoalChangeProposal(
            id=row["id"], evaluation_id=row["evaluation_id"], goal_group_id=row["goal_group_id"],
            proposed_change=GoalInterventionType(row["proposed_change"]),
            proposal_expires_at=_parse_iso(row["proposal_expires_at"]),
            status=GoalProposalStatus(row["status"]), created_at=_parse_iso(row["created_at"]),
            resolved_at=_parse_iso(row["resolved_at"]) if row["resolved_at"] else None,
        )
