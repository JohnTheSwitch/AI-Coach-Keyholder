# goal_management — Slice 1

The fourth real domain module, and the first independent of the Trust
Manager → Penalty Engine → Recovery Plan → Recovery Credit branch
(Section 1: Goal Management reads nothing from the Trust Manager for
any decision of its own). Built against
`docs/architecture/goal_technical_design.md`, same incremental
discipline as every prior module.

## What this slice covers

- **`Goal`/`GoalVersion`** (2.2) — mutable current-state + append-only
  content history, the same pattern `penalty_windows` established.
- **Lifecycle** (3.1-3.4) — `create_goal()` (2.3, exempt from GOAL-6),
  direct `pause_goal()`/`resume_goal()`/`complete_goal()` (not
  GoalChangeProposal-gated — see "Two real gaps" below for why
  `complete_goal()` is direct), `archive_goal()` (3.3, GOAL-11: requires
  an already-terminal status, changes no status, has no behavioral
  effect anywhere else).
- **`GoalEvidence`** (4.1) — append-only, GOAL-4. `record_evidence()`
  has no code path to anything else (GOAL-2: no single row, of any
  outcome, automatically triggers an evaluation, a Trust effect, or a
  lifecycle transition).
- **`GoalEvaluation`** (5.1) — append-only, GOAL-3 (non-empty
  `triggering_evidence_ids` enforced), GOAL-9 (structurally has no
  field answering the accountability question — verified directly by
  `TestGoalEvaluation::test_evaluation_has_no_accountability_field`).
  `findings`/`proposed_intervention` are recorded as plain parameters,
  the same way `recovery_plan.propose_task()`'s content is — this slice
  builds the mechanism for recording an evaluation's content, not the
  AI reasoning that will eventually author it (no `ai/`/`core/coach_engine`
  exists yet).
- **`GoalChangeProposal`/`GoalChangeProposalContent`** (5.3, GOAL-6) —
  `create_change_proposal()`, `accept_proposal()` (dispatches to the
  matching internal effect based on `proposed_change`, applying exactly
  the recorded, immutable content — never content reconstructed at
  acceptance time), `decline_proposal()`.
- **The three GOAL-6-gated effects, reachable ONLY via
  `accept_proposal()`** — `_apply_adaptation_in_transaction()`
  (`ADAPT_TARGET`, new `GoalVersion`), `_apply_replacement_in_transaction()`
  (`PROPOSE_REPLACEMENT`, new `Goal` + original → `REPLACED`),
  `_apply_abandonment_in_transaction()` (`PROPOSE_ABANDONMENT`).
  `INCREASE_SUPPORT`/`NO_CHANGE` resolve the proposal with no further
  effect (5.2).
- **Startup reconciliation** (9.3) — `recover_goal_management_state()`
  expires any `PENDING` `GoalChangeProposal` past its
  `proposal_expires_at`; wired into `system/startup.py`, verified
  end-to-end (`tests/system/test_startup.py::TestGoalManagementRecoveryViaStartup`).
  9.2 explains why nothing else needs reconciling.
- **GOAL-1, enforced structurally** — this module imports neither
  `trust_manager` nor `penalty_engine` at all, checked directly
  (`TestGoalStructuralIsolation`), not merely asserted in prose.

## Goal results from commitments (owner, 2026-11-13)

`record_evidence()` had no production caller, so goals counted for nothing. `record_reported_result_in_transaction()` (consumer of
`coach.goal_result_reported`) records one `goal_evidence` row per daily commitment tied to an active goal: kept = `met`, not kept = `missed`,
source `user_report`. It never evaluates the goal, touches Trust or changes its lifecycle (GOAL-2).

## What is deferred

- **`GoalAccountabilityAssessment`'s negotiation fields** (Section 6:
  `review_outcome`/`negotiation_reason`) — deferred with
  `GoalNegotiation`. The assessment itself is implemented, see below.
- **`GoalNegotiation`/`GoalNegotiationRound`** (Section 7) — multi-round
  negotiation between Coach and Keyholder perspectives, including
  `ESCALATED_TO_USER` and the `MOOT` transition (GOAL-14) triggered by a
  Goal reaching a terminal lifecycle state. No negotiation can exist in
  this slice (nothing creates a `GoalAccountabilityAssessment` with
  `review_outcome=NEGOTIATE`, the only trigger for opening one), so
  GOAL-14's mootness rule has no case to apply to yet — not implemented,
  not silently assumed to be needed later.
- **The check-in/conversation mechanism that actually produces
  `GoalEvidence`** (4.2) — explicitly out of scope in the architecture
  document itself, not only in this slice.
- **The actual Coach reasoning behind `GoalEvaluation.findings`/
  `proposed_intervention`** — this slice's `record_evaluation()` accepts
  these as plain parameters; no LLM/`ai/` code authors them yet.

## Two real gaps found in the architecture document while implementing it

**1. `GoalInterventionType` has no value for proposing `COMPLETED`.**
Section 3.4 states plainly that `ACTIVE/PAUSED → COMPLETED` "is always a
Coach-proposed, user-confirmed judgment (5.3)" — implying it should go
through the same `GoalChangeProposal` mechanism as abandonment and
replacement. But `GoalInterventionType` (5.1) enumerates exactly five
values (`ADAPT_TARGET`, `INCREASE_SUPPORT`, `NO_CHANGE`,
`PROPOSE_REPLACEMENT`, `PROPOSE_ABANDONMENT`) — none of them means
"propose completion." GOAL-6's own literal wording ("no terminal
lifecycle transition takes effect without a GoalChangeProposal") would
then include `COMPLETED`, but the object model gives no way to route it
through one.

Resolved, in this slice, by treating `complete_goal()` as a direct call
(like `pause_goal()`/`resume_goal()`, not gated by a proposal) — the
least-presumptuous resolution available: inventing a sixth
`GoalInterventionType` value the document never specified would be a
bigger leap than diverging from one invariant's literal scope where the
document's own data model doesn't support it. Flagged here rather than
silently resolved; worth settling explicitly (add the missing
intervention type, or explicitly exempt `COMPLETED` from GOAL-6) before
building whatever eventually decides to call `complete_goal()`.

**2. `adapt()`'s permitted source status is ambiguous.** The lifecycle
diagram (3.1) draws the `adapt()` self-loop only on the `ACTIVE` state,
but nothing in the surrounding prose explicitly says adaptation from
`PAUSED` is disallowed — it's just absent from the picture. This slice
follows the diagram literally: `_apply_adaptation_in_transaction()`
only permits `ACTIVE`
(`tests/goal_management/test_repository.py::TestChangeProposalAdaptTarget::test_adapt_target_only_permitted_from_active`).
If a paused Goal genuinely needs its target adjustable without first
resuming it, that's a real product question, not one this slice
resolved by assumption.

## Design notes

- **`goals.current_version_id` has no FOREIGN KEY to `goal_versions(id)`**
  (migration 010) — a genuine chicken-and-egg at creation time (the
  `Goal` row and its first `GoalVersion` reference each other).
  Resolved by pre-generating the version's id in Python, inserting
  `goals` first, then `goal_versions` — application-enforced ordering,
  not DB-enforced, consistent with both rows only ever being written
  together in one transaction.
- **`PROPOSE_REPLACEMENT`'s new Goal inherits the original's
  `trust_domain`** — `GoalChangeProposalContent` deliberately has no
  field for it (5.3: "changing it is out of scope for an
  adaptation/replacement proposal in this version of the document").
  Inheriting rather than requiring a new value is this slice's own,
  minimal resolution, not a documented requirement.
- **Row ↔ dataclass mapping is intentionally verbose**, same convention
  as every other module in this system.

## Phase 1: GoalAccountabilityAssessment (migration 024)

`GoalManager.record_accountability_assessment(evaluation_id, *,
relevant_to_trust, rationale, now, direction=None)` is the only writer
(domain-service API; no model, Discord or admin entry point).

- **Direction is derived, not asserted.** `derive_accountability_direction()`
  maps the triggering evidence: all `MET` -> `PROGRESS`, all `MISSED` ->
  `SETBACK`, anything else (`PARTIALLY_MET`, a mix) -> `NEUTRAL`. A caller
  may pass `direction` only to **neutralise** (e.g. an excused outcome);
  asserting a direction the evidence does not support raises `ValueError`.
- **GOAL-13:** one assessment per evaluation (`UNIQUE evaluation_id`;
  `AssessmentAlreadyExistsError`). Append-only; `trust_domain` is copied from
  the Goal's current version so the event is self-contained.
- **Event:** `goal_accountability_assessment.recorded` is always emitted
  (relevant or not); the Trust Manager decides whether to act on it.
  `get_accountability_assessment()` is the read API.
- Goal Management still imports nothing from `trust_manager` (GOAL-1).
