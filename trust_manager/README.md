# trust_manager — Slice 1 + Slice 2

The first real domain module built against the architecture baseline
(`docs/architecture/trust_manager_technical_design.md`). Built in two
tested slices — the same incremental discipline used for
`infrastructure/` (Clock → Database → Outbox, each its own phase).

## What is covered (Slice 1 + Slice 2)

Canonical sections implemented: **2.1, 2.2, 2.4 (enum only), 2.6, 2.8,
2.10, 3.1-3.6, 5.1, 5.2, 5.3, 5.4, 7 (the subset below), 8 (the subset
below), 13, 14**.

- **Domain Registry + Domain State** (`create_domain`,
  `deactivate_domain`, `reactivate_domain`, `get_domain_state`) — TI1's
  consent-id requirement, the 3.4 default values.
- **Incident + Confirmation lifecycle** (`register_incident_report`,
  `confirm_incident`) — the atomic CONFIRMED-path fix (TI23/14.2), now
  extended: reaching `CONFIRMED` also triggers the 'incident'
  recalculation (3.2), in the SAME transaction (see "Slice 2 design
  notes" below for why this is safe).
- **The severity/cooperation rubric** (`trust_manager/severity.py`).
- **The score recalculation pipeline** (`trust_manager/recalculation.py`
  — pure functions; `TrustManager.recalculate_domain_trust()`/
  `_recalculate_domain_trust_in_transaction()` — database access):
  `effective_weight()` (3.3/TI9, capped), `apply_recalculation()`
  (3.5/TI19, the per-recalculation delta cap and score clamp),
  `compute_confidence()` (3.6, a diminishing-returns function of
  evidence volume within the rolling window). `TrustDomainState.score`/
  `confidence` **now actually update** — the Slice 1 gap this slice
  exists to close.
- **The public read API** (13) — `get_incident_assessment()`,
  `get_confirmed_incidents_since()`.
- **Crash recovery** (14.3) — `recover_trust_manager_state()`.
- **Events** — Slice 1's events, plus `trust_domain.recalculated`
  (every recalculation, even a purely confidence-driven one with
  `delta_score=0` — 8's "even when `delta=0`").

## What is deferred to a later slice

- **`TrustEvidenceDispute`** (2.5) and the three restricted manual-review
  operations (2.9).
- **`OverallTrustReport`** (2.7, Section 4).
- **The `window_completion` and `scheduled_review` recalculation
  triggers** (3.2) — both require modules that do not exist yet
  (Penalty Engine; a check-in/scheduling mechanism for assembling
  `ExposureRecord`, 3.7). `recalculate_domain_trust()` is already
  callable with any `triggered_by` string, so wiring either trigger
  later is a caller-side addition, not a change to this module.
  `ExposureRecord` and `maybe_create_sustained_period_evidence()` (3.7)
  are similarly not yet implemented — no code path produces
  `SUSTAINED_PERIOD` evidence in this slice.
- **`should_extend()`/`ExtensionContext`** (Section 6).
- **`TrustDomainState.trend`** — stays at its initial value
  (`'stable'`, set at domain creation); no code in either slice computes
  or updates it. The architecture document does not specify how trend
  is derived, so nothing was invented here rather than guess.

## Bootstrap defaults (governance classification)

Added during the Phase 2.7 architecture review, after distinguishing
"who decided this exists" (almost always: the developer, by necessity,
to make the architecture executable) from "who should have the right
to change it going forward" (genuinely undecided for several
constants). Tagged in code with `# BOOTSTRAP_DEFAULT(owner=undecided,
mechanism=code):`, verified by `tests/test_bootstrap_default_tags.py`
(a format guard only -- it never requires these to be resolved).

| Constant | Created by | Intended owner | Current change mechanism | Why bootstrap default |
|---|---|---|---|---|
| `COOPERATION_SELF_DISCLOSURE_OFFSET`, `COOPERATION_ACTIVE_RESOLUTION_OFFSET` (`severity.py`) | developer | undecided, plausibly **user** | code | Directly shapes how forgiving the system is toward one person's cooperative behavior -- a personal-policy question, not a technical one. |
| `severity_base_weight` (`severity.py`) | developer | undecided, plausibly **user** | code | Same category as the offsets above -- how severely each tier affects Trust is a felt behavioral consequence, not an implementation detail. |
| `MAX_ABS_EFFECTIVE_WEIGHT` (`recalculation.py`) | developer | undecided, plausibly **architecture** or **system-safety-policy** | code | 3.3/TI9 mandates *some* cap exists but gives no number; this one reads more like a numerical-stability safeguard than a felt personal-policy choice -- flagged for that reason, not pre-assigned to the user. |
| `CONFIDENCE_K` (`recalculation.py`) | developer | undecided, plausibly **architecture** | code | Shapes the mathematical curve of a derived quantity (confidence), not a directly-felt consequence -- same reasoning as `MAX_ABS_EFFECTIVE_WEIGHT`. |

**Not tagged, and why:** `MAX_ABSOLUTE_DELTA_PER_RECALCULATION` (0.15)
and `CONFIDENCE_ROLLING_WINDOW_DAYS` (180) are given explicit numeric
values directly in `trust_manager_technical_design.md` (search that
document for these exact names) -- architecture-owned, not bootstrap
defaults. `DEFAULT_NEW_DOMAIN_SCORE`/`DEFAULT_NEW_DOMAIN_CONFIDENCE`
(`repository.py`) are likewise given explicit values by the document
(3.4). `_MIN_INCIDENT_RAW_WEIGHT` (`severity.py`) is a technical
sign-guarantee clamp, not a policy value -- see its own comment for why
it was deliberately excluded.

## Slice 2 design notes

- **Why the 'incident' trigger recalculates inside `confirm_incident()`'s
  own transaction, rather than via a separate, later call**: the
  evidence being consumed and its consumption are produced by the exact
  same code path, with no cross-module event delivery involved — there
  is no crash-recovery gap to protect against here the way TI23 protects
  the assessment/evidence write, because nothing can complete between
  "evidence written" and "evidence consumed" when both happen in the
  same transaction. `window_completion`/`scheduled_review` will
  genuinely need `recalculate_domain_trust()`'s separate-transaction
  form, since they react to a `domain_events` delivery from a different
  module's transaction.
- **Confidence is computed from evidence recorded as "applied" (i.e.
  already referenced by a `trust_recalculation_evidence` row), not from
  every existing `TrustEvidence` row** — this matches 3.6's own
  language ("confidence naturally decreases when old evidence 'drops
  out' of the window without being replaced") and means confidence never
  counts evidence that has not yet actually informed the score.
- **TI4's `UNIQUE(evidence_id)`** on `trust_recalculation_evidence`
  (not a composite key with `recalculation_id`) is what makes "consumed
  at most once, ever" a database-enforced guarantee, not merely an
  application-level convention — verified directly
  (`test_evidence_is_consumed_exactly_once`).

## Design notes carried over from Slice 1

- **`Incident` is mutable-with-status, not append-only**
  (`implementation_conventions.md` Section 7) — `confirmation` and
  `assessment` are "what is true now"; history lives in the append-only
  `ConfirmationRecord` trail, not in old values of this row.
- **`IncidentNotFoundError`** — raised by `confirm_incident()` when
  given an unknown `incident_id`, on purpose (a genuine precondition
  violation, not a domain outcome).
- **Row ↔ dataclass mapping** is intentionally verbose rather than
  clever — every field maps to one named column, no dynamic field
  iteration.

## Phase 1: Goal Accountability Assessment integration (Section 15)

Goal outcomes reach Trust **only** through this path (tasks reach Trust only
via Goals; there is no direct Task -> Trust route).

- `system/startup.py` registers `trust_manager` for
  `goal_accountability_assessment.recorded`. The handler calls
  `TrustManager._record_goal_assessment_evidence_in_transaction()` using only
  the event payload; `record_goal_assessment_evidence()` is the same logic in
  its own transaction.
- Evidence is written only if the assessment is relevant to Trust **and** its
  direction is `progress` (`GOAL_PROGRESS`) or `setback` (`GOAL_SETBACK`).
  Neutral, not-relevant, unknown-domain and inactive-domain cases write nothing.
- Idempotent: checked before insert, plus the `UNIQUE(source_entity_type,
  source_entity_id, evidence_type)` constraint (TI25), so a redelivery never
  creates a second row or a second recalculation.
- A new row triggers an immediate recalculation in the same transaction
  (`triggered_by='goal_assessment'`) and emits `trust_evidence.recorded` and
  `trust_domain.recalculated`. The scheduled trigger remains deferred.
- `trust_manager/goal_scoring.py` (pure): progress `+0.05`, setback `-0.08`
  (asymmetric), confidence `1.0`, all `BOOTSTRAP_DEFAULT`. The existing 0.5
  per-row and 0.15 per-recalculation caps still apply.

v0.39 Phase I4: `register_incident_report()` accepts an optional `incident_id`, so a caller that claims the id in its own ledger first
(`peer_review/`) can retry a registration idempotently: a second insert of the same id fails on the primary key instead of creating a
duplicate Incident. Every new Incident still starts UNCONFIRMED.
