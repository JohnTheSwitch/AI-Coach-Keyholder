# Domain Events Catalog

> Status: **Architecture baseline — approved for implementation**, per
> the same convention as the rest of `docs/architecture/`.
>
> **This is a consistency-pass document, not a new design phase.** Its
> only purpose: every event exists exactly once, has exactly one
> canonical definition and one publisher, before the transactional
> outbox (Phase 1.4, `implementation_conventions.md` Section 5) is built
> against it. Building this catalog's first version surfaced five real
> cross-document inconsistencies (Section 5) — all five are now
> **resolved**, applied directly to the specific documents where each
> lived, not just proposed here. This document is the registry; the
> owning document remains authoritative for an event's precise trigger
> and payload shape.

---

## 1. The Rule This Catalog Enforces

**One event, one canonical definition, one publisher.** A document
that needs to mention an event it does not own must reference the
canonical document, never redefine the event's name, publisher, or
shape itself. This catalog is the index of which document is
canonical for which event — not a second, competing definition.

Every event below carries eight pieces of information:

| Column | Meaning |
|---|---|
| **Event** | The `event_type` string. |
| **Publisher** | The single module whose code writes this event — never two. |
| **Consumers** | Every module (if any) that reacts to it today. |
| **Aggregate Owner** | The domain entity this event is fundamentally about — usually, but not always, owned by the same module as Publisher (see Section 6 for the one nuance this raises). |
| **Ordering Scope** | The specific aggregate instance within which this event must preserve order relative to others about the same instance. Coincides with Aggregate Owner for every event in this system except the Section 6 nuance. |
| **Cross-Module Today?** | Whether a real, named consumer in another module exists right now — distinct from whether the event is worth recording at all (see Section 4). |
| **Persistent** | Whether it is written to the shared `domain_events` outbox table. True for every event in this catalog today, though Section 4 identifies two candidates that may not need to be, on reflection. |
| **External** | Whether a consumer *outside* this system (Discord notification, future Chaster/Apple Health adapter, audit export) is a plausible future subscriber — not a commitment to build one, just a flag for "keep this payload self-contained and stable." |
| **Canonical Definition** | The document + section that is authoritative for this event's exact trigger and payload. |

---

## 2. Per-Module Publish/Listen Map

### Trust Manager

*Owns: `Incident` (in full), `TrustEvidence`, `TrustDomainState`,
`TrustRecalculation`, `OverallTrustReport`.*

**Publishes:** `trust_domain.created`/`.deactivated`/`.reactivated`,
`incident.reported`, `incident.confirmation_changed`,
`trust_evidence.recorded`/`.disputed`, `trust_domain.recalculated`,
`overall_trust.report_generated`.

**Listens to:** `goal_accountability_assessment.recorded` (Goal
Management), via `get_accountability_assessment()` — never the raw
event payload's domain fields directly.

---

### Penalty Engine (Penalty Window + Extension)

*Owns: `penalty_windows`, `freeze_periods`, `incident_consumption`,
`recovery_credit_ledger`, `recovery_credit_decisions`,
`ExtensionDecision`.*

**Publishes:** `penalty_window.started`/`.frozen`/`.resumed`/`.completed`/`.extended`/`.target_duration_changed`,
`freeze_periods.opened`/`.closed`, `penalty_engine.freeze_expired`,
`emergency_override.triggered`, `extension.decision_recorded`,
`recovery_credit_decision.recorded`.

**Listens to:** `incident.confirmation_changed` (Trust Manager,
filtered to `new_confirmation=CONFIRMED`), `activity_authorization.committed`
(creates the freeze), `activity_authorization.resume_requested`
(closes the freeze), `recovery_plan.task_completed` (Recovery Plan).

---

### Activity Authorization

*Owns: `ActivityAuthorizationDecision`, `ActivityAuthorizationSession`,
`TokenLedgerEntry`.*

**Publishes:** `activity_authorization.requested`/`.decided`/`.confirmation_received`/`.declined`/`.confirmation_expired`/`.committed`/`.commit_failed`/`.freeze_confirmed`/`.freeze_confirmation_failed`/`.resume_requested`/`.resume_confirmed`,
`urge_disclosure.recorded`, `token_ledger.entry_recorded`/`.entry_reversed`.

**Listens to:** `freeze_periods.opened`/`.closed` (Penalty Engine,
filtered to `partnered_intimacy_authorization`), `penalty_engine.freeze_expired`.

---

### Hygiene Privilege

*Owns: `DiscretionaryHygieneBreakGrant`/`Session`,
`HygienePenaltyOverrideDetermination`, `EffectiveHygienePolicyResult`.*

**Publishes:** `hygiene_privilege.policy_evaluated`/`.override_determination_recorded`/`.break_requested`/`.grant_decided`/`.grant_expired_unused`/`.grant_failed_at_start`/`.session_started`/`.session_ended`.

**Listens to:** nothing by event — reads Penalty Window and
`TrustDomainState('hygiene')` directly via their public read APIs on
every call, per the "Hygiene → read-only" row of the Interpretation
Handoff Pattern (`implementation_conventions.md` Section 3).

---

### Goal Management

*Owns: `Goal`, `GoalVersion`, `GoalEvidence`, `GoalEvaluation`,
`GoalAccountabilityAssessment`, `GoalNegotiation`,
`GoalChangeProposal`.*

**Publishes:** `goal.created`/`.adapted`/`.paused`/`.resumed`/`.completed`/`.abandoned`/`.replaced`,
`goal_evidence.recorded`, `goal_evaluation.recorded`,
`goal_change_proposal.created`/`.resolved`,
`goal_accountability_assessment.recorded`,
`goal_negotiation.opened`/`.round_added`/`.resolved`/`.escalated`/`.moot`.

**Listens to:** nothing (GOAL-1).

---

### Recovery Plan

*Owns: `RecoveryPlan`, `RecoveryTask`, `RecoveryTaskCompletion`.*

**Publishes:** `recovery_plan.created`/`.frozen`/`.resumed`/`.regenerated`/`.completed`,
`recovery_plan.task_proposed`/`.task_accepted`/`.task_completed`/`.task_withdrawn`.

**Listens to:** `penalty_window.started`/`.frozen`/`.resumed`/`.target_duration_changed`/`.completed` (Penalty Engine) — every transition here is a direct reaction to one of these (RP-6).

---

## 3. Consolidated Registry

> **Ordering Scope** is the aggregate instance within which two events
> must preserve relative order (e.g., two events about the same
> `penalty_window_id`) — in this system it coincides with **Aggregate
> Owner** for every event except the one nuance Section 6 explains.
> Ordering across *different* aggregate instances is never required,
> because consumers re-read current state through a read API rather
> than relying on event arrival order for correctness (the
> Interpretation Handoff Pattern already provides this property, not
> something the outbox needs to add).
>
> **Cross-Module Today?** is Yes only where a real, named consumer in
> another module exists right now. See Section 4 for what "No" here
> actually means — it is not the same as "unimportant."

| Event | Publisher | Consumers | Aggregate Owner | Ordering Scope | Cross-Module Today? | Persistent | External | Canonical Definition |
|---|---|---|---|---|---|---|---|---|
| `trust_domain.created`/`.deactivated`/`.reactivated` | trust_manager | — | TrustDomain | TrustDomain | No | ✔ | ✖ | `trust_manager_technical_design.md` §8 |
| `incident.reported` | trust_manager | — | Incident | Incident | No | ✔ | ✖ | `trust_manager_technical_design.md` §8 |
| `incident.confirmation_changed` | trust_manager | penalty_engine (filtered) | Incident | Incident | **Yes** | ✔ | ✖ | `trust_manager_technical_design.md` §8 |
| `trust_evidence.recorded` | trust_manager | — | TrustEvidence | Incident (the Incident it concerns) | No | ✔ | ✖ | `trust_manager_technical_design.md` §8 |
| `trust_evidence.disputed` | trust_manager | — | TrustEvidenceDispute | Incident | No | ✔ | ✖ | `trust_manager_technical_design.md` §8 |
| `trust_domain.recalculated` | trust_manager | — | TrustRecalculation | TrustDomain | No | ✔ | ✔ (future audit) | `trust_manager_technical_design.md` §8 |
| `overall_trust.report_generated` | trust_manager | — | OverallTrustReport | — (a report, not tied to one domain instance) | No | ✔ | ✖ | `trust_manager_technical_design.md` §8 |
| `penalty_window.started`/`.frozen`/`.resumed`/`.completed` | penalty_engine | recovery_plan | PenaltyWindow | PenaltyWindow | **Yes** | ✔ | ✔ (Discord) | `penalty_window_technical_design.md` §4.2 |
| `penalty_window.extended`/`.target_duration_changed` | penalty_engine | recovery_plan | PenaltyWindow | PenaltyWindow | **Yes** | ✔ | ✖ | `penalty_window_technical_design.md` §4.2 |
| `freeze_periods.opened`/`.closed` | penalty_engine | activity_authorization (filtered) | FreezePeriod | FreezePeriod | **Yes** | ✔ | ✖ | `penalty_window_technical_design.md` §4.2 |
| `penalty_engine.freeze_expired` | penalty_engine | activity_authorization | FreezePeriod | FreezePeriod | **Yes** | ✔ | ✖ | `penalty_window_technical_design.md` §4.2/4.5 |
| `emergency_override.triggered` | penalty_engine | — *(no in-system consumer today — see Finding 5)* | FreezePeriod | FreezePeriod | No | ✔ | ✔ (safety-critical) | `penalty_window_technical_design.md` §4.2 |
| `extension.decision_recorded` | penalty_engine | — | ExtensionDecision | PenaltyWindow (the window it decided against) | No | ✔ | ✖ | `extension_technical_design.md` §5 |
| `recovery_credit_decision.recorded` | penalty_engine | — | RecoveryCreditDecision | PenaltyWindow | No | ✔ | ✖ | `penalty_window_technical_design.md` §4.2 |
| `activity_authorization.requested`/`.decided`/`.confirmation_received`/`.declined`/`.confirmation_expired` | activity_authorization | — | ActivityAuthorizationDecision | ActivityAuthorizationDecision | No | ✔ | ✖ | `activity_authorization_technical_design.md`, Domain Events |
| `activity_authorization.committed` | activity_authorization | penalty_engine | ActivityAuthorizationDecision | ActivityAuthorizationDecision | **Yes** | ✔ | ✖ | same |
| `activity_authorization.commit_failed` | activity_authorization | — | ActivityAuthorizationDecision | ActivityAuthorizationDecision | No | ✔ | ✖ | same |
| `activity_authorization.freeze_confirmed`/`.freeze_confirmation_failed` | activity_authorization | — | ActivityAuthorizationDecision (see §6) | ActivityAuthorizationDecision | No | ✔ | ✖ | same |
| `activity_authorization.resume_requested` | activity_authorization | penalty_engine | ActivityAuthorizationDecision | ActivityAuthorizationDecision | **Yes** | ✔ | ✖ | same |
| `activity_authorization.resume_confirmed` | activity_authorization | — | ActivityAuthorizationDecision (see §6) | ActivityAuthorizationDecision | No | ✔ | ✖ | same |
| `urge_disclosure.recorded` | activity_authorization | *(future: UrgeSupportProtocol)* | ActivityAuthorizationRequest | ActivityAuthorizationRequest | No | ✔ | ✔ (Coach reaction) | same |
| `token_ledger.entry_recorded`/`.entry_reversed` | activity_authorization | — | TokenLedgerEntry | TokenLedgerEntry (per user's ledger) | No | ✔ | ✔ (Discord balance) | same |
| `hygiene_privilege.policy_evaluated` | hygiene_privilege | — | EffectiveHygienePolicyResult | — (see Finding 6: candidate audit-only reclassification) | No | ✔ | ✖ | `hygiene_privilege_technical_design.md` §5 |
| `hygiene_privilege.override_determination_recorded` | hygiene_privilege | — | HygienePenaltyOverrideDetermination | — (see Finding 6) | No | ✔ | ✖ | same |
| `hygiene_privilege.break_requested`/`.grant_decided`/`.grant_expired_unused`/`.grant_failed_at_start` | hygiene_privilege | — | DiscretionaryHygieneBreakGrant | DiscretionaryHygieneBreakGrant | No | ✔ | ✖ | same |
| `hygiene_privilege.session_started`/`.session_ended` | hygiene_privilege | *(future: Coach check-in)* | DiscretionaryHygieneBreakSession | DiscretionaryHygieneBreakSession | No | ✔ | ✔ (future Coach) | same |
| `goal.created`/`.adapted`/`.paused`/`.resumed`/`.completed`/`.abandoned`/`.replaced` | goal_management | — | Goal | Goal | No | ✔ | ✔ (future Discord) | `goal_technical_design.md` §13 |
| `goal_evidence.recorded` | goal_management | — | GoalEvidence | Goal | No | ✔ | ✖ | same |
| `goal_evaluation.recorded` | goal_management | — | GoalEvaluation | Goal | No | ✔ | ✖ | same |
| `goal_change_proposal.created`/`.resolved` | goal_management | — | GoalChangeProposal | Goal | No | ✔ | ✖ | same |
| `goal_accountability_assessment.recorded` | goal_management | trust_manager | GoalAccountabilityAssessment | Goal | **Yes** | ✔ | ✖ | same |
| `lock_decision.recorded` | rules_engine | — | Decision (dry run) | Lock Decision | No | ✔ | ✖ | `rules_engine_phase2_proposal.md` |
| `goal_negotiation.opened`/`.round_added`/`.resolved`/`.escalated`/`.moot` | goal_management | — | GoalNegotiation | Goal | No | ✔ | ✖ | same |
| `recovery_plan.created`/`.frozen`/`.resumed`/`.regenerated`/`.completed` | recovery_plan | — | RecoveryPlan | RecoveryPlan (== PenaltyWindow 1:1) | No | ✔ | ✖ | `recovery_plan_technical_design.md` §5 |
| `recovery_plan.task_proposed`/`.task_accepted`/`.task_withdrawn` | recovery_plan | — | RecoveryTask | RecoveryPlan | No | ✔ | ✔ (Discord) | same |
| `recovery_plan.task_completed` | recovery_plan | penalty_engine | RecoveryTaskCompletion | RecoveryPlan | **Yes** | ✔ | ✖ | same |

---

## 4. Is This Really a Domain Event? (Classification, Not Just Naming)

The `Cross-Module Today?` column, once filled in, made something visible
that the first version of this catalog's `Persistent: ✔` column (then
uniform and unquestioned) had papered over: **most events in this
system have no consumer today.** Roughly 7 of the ~40 rows do; the rest
exist because a module's own design document says the write should be
observable, not because anything currently observes it.

That is not automatically a problem — several of these are genuinely
anticipated, specific future consumers (Coach check-in reactions,
UrgeSupportProtocol, Discord notifications), just not yet built. But it
is worth distinguishing four different things an "event" can actually
be, since only one of them clearly requires the shared, cross-module
`domain_events` outbox with its claim/publish/consumer-dedup machinery:

1. **A true cross-module Domain Event** — another module needs to react
   to it, today or with a concretely named future consumer. Needs the
   shared outbox.
2. **An audit/history record** — exists so a human (or a future audit
   export) can see what happened; no module reacts to it. If the
   underlying entity is *already* its own append-only table (e.g.
   `TrustEvidence`, `EffectiveHygienePolicyResult`), an additional
   "event" saying the same thing is arguably redundant — the table
   already is the durable record; the event does not add information,
   only a second place to look for it.
3. **A history record for a *mutable* entity** — exists because the
   owning row's `status` changes over time and would otherwise lose its
   own history (e.g. `DiscretionaryHygieneBreakGrant`, whose `status`
   overwrites in place). Here, an event genuinely is the only record of
   "what happened when," even without a cross-module consumer — this is
   a real justification, distinct from (2).
4. **A pure internal callback / read-model update** — would not belong
   in this catalog at all, since it never needs to leave the
   transaction that produced it. No example in this system currently
   falls in this category (which is itself worth noting — it means
   nothing here has been over-eventified into the outbox that shouldn't
   be there).

### Finding 6 — Two Candidates for Reclassification as Audit-Only (Flagged, Not Applied)

`hygiene_privilege.policy_evaluated` and
`hygiene_privilege.override_determination_recorded` are category (2),
not (1) or (3): `EffectiveHygienePolicyResult` and
`HygienePenaltyOverrideDetermination` are already append-only tables
(HYG-8 mandates writing the former "even when unchanged," explicitly
for audit purposes). Emitting a `domain_events` row that duplicates
information the entity's own table already durably holds, with no
consumer to deliver it to, is the clearest candidate in this system for
"this may not need the shared outbox at all."

**Not resolved here, deliberately:** this is a judgment call about
outbox scope, not a naming inconsistency like Findings 1–5, and
belongs to whoever designs Phase 1.4, with the tradeoff stated plainly:
keeping them in the shared outbox costs a small amount of redundant
storage and zero consumers today, in exchange for never having to
retrofit outbox delivery later if a consumer *does* eventually want
"notify me the moment a policy evaluation happens" rather than "let me
query the table when I care." Simplicity (one mechanism for every
event, no special case) is a real argument for leaving them in;
minimalism (don't persist-and-route what nothing reads) is a real
argument for taking them out. Flagged so Phase 1.4 makes this choice
knowingly rather than by default.

Every other "Cross-Module Today?: No" row in Section 3 is category (3)
— a genuine history record for a mutable entity — or a specifically
anticipated future consumer already named in its own row, not a
Finding-6-style redundancy candidate.

---

## 5. Findings — Resolved

All five are applied directly to their owning document(s); this
section records what was decided and where, for anyone later asking
"why does this event work this way."

### Finding 1 — `incident.confirmed` Did Not Exist as a Real Event (Resolved)

**Decision:** exactly one canonical event, `incident.confirmation_changed`,
published by `trust_manager` for every confirmation transition.
Consumers that care only about reaching `CONFIRMED` (the Penalty
Engine) filter on the payload's `new_confirmation` field — no second
event type was introduced. A narrower `incident.confirmed` event was
considered and rejected: one write, one event, is simpler than one
write producing two events for what a consumer can already distinguish
from the payload of the single one.

**Applied to:** `trust_manager_technical_design.md` §8 (clarified as
canonical and payload-filterable), `penalty_window_technical_design.md`
(T11, §4.2), `system_state_machine.md` §5 — all three now reference
`incident.confirmation_changed` filtered by payload, none reference a
nonexistent `incident.confirmed`.

### Finding 2 — `activity_authorization.freeze_confirmed`/`.resume_confirmed`: Publisher Disputed (Resolved)

**Decision:** the Penalty Engine publishes two new, generic, canonical
events — `freeze_periods.opened` and `freeze_periods.closed` — for any
`freeze_periods` row opening/closing, for any reason. Activity
Authorization is the sole publisher of its own
`.freeze_confirmed`/`.resume_confirmed`, triggered by consuming those
generic events filtered to `partnered_intimacy_authorization`. This
also gives Hygiene Privilege (and any future consumer) a real,
generic freeze-state-changed event to react to, which previously only
existed as "read current state whenever you happen to check."

**Applied to:** `penalty_window_technical_design.md` §4.2 (new
`freeze_periods.opened`/`.closed` rows, §4.5 updated),
`activity_authorization_technical_design.md` (Domain Events table,
both rows), `system_state_machine.md` §5.

### Finding 3 — `recovery_plan.*` Module Name and Completeness Drifted (Resolved)

**Decision:** `penalty_window_technical_design.md`'s own copy of the
`recovery_plan.*` event list is removed entirely and replaced with a
cross-reference to `recovery_plan_technical_design.md` §5 (module name
`recovery_plan`, includes `.regenerated`) — the actual, later,
authoritative source. No redefinition remains to drift out of sync
again.

**Applied to:** `penalty_window_technical_design.md` §4.2 (I1's stray
`recovery_plan_generator` reference and the `domain_event_consumers`
schema comment both corrected too), `recovery_plan_technical_design.md`
(historical framing note adjusted to past tense).

### Finding 4 — `recovery_engine.credit_decision_recorded`: No Such Module (Resolved)

**Decision:** renamed to `recovery_credit_decision.recorded` — entity
name, not a module prefix, matching the existing
`trust_evidence.recorded`/`goal_evaluation.recorded` pattern. Publisher
remains `penalty_engine` (owns `recovery_credit_decisions`).

**Applied to:** `penalty_window_technical_design.md` §4.2.

### Finding 5 — `emergency_override.triggered`: No Single Owning Module (Resolved)

**The question explicitly checked:** is this event about the Penalty
Engine, or about the system as a whole?

**Answer: about the Penalty Engine — specifically about a `FreezePeriod`
— not about the system as a whole**, and this is worth distinguishing
carefully rather than asserting. Two different things could be meant
by "is this about the system as a whole":

1. *Does emergency override have broad, cross-cutting significance?*
   Yes — that is exactly what the `External: ✔ (safety-critical)`
   marking in the registry already captures. High consumer interest is
   real.
2. *Does the write that causes this event change some state owned by a
   system-wide concept, rather than a specific module's own table?*
   No — the actual write is a `freeze_periods` row, a table the Penalty
   Engine already owns and already writes for every other freeze
   reason. There is no separate "system emergency state" entity
   anywhere in this architecture for this event to be about instead.

These are different axes, and only the second one determines
**Aggregate Owner**/**Publisher** in this catalog's terms — the first
is what **External** already exists to express. Conflating them would
have been the actual mistake here, not the specific answer landed on.

`philosophy.md`'s own treatment of Emergency Override (I16, and 3.10)
supports this: it is presented as a special *reason* within the freeze
mechanism (immediate, unconditional, no preconditions), not as a
distinct domain concept with its own state. If a dedicated Safety
module is ever introduced later — one that tracks something broader
than "is there an open freeze for this reason," e.g. a system-wide
safety mode with its own lifecycle independent of any single
`freeze_periods` row — this event's ownership would need to move with
it, and that would be a deliberate migration (`implementation_conventions.md`
Section 12's "moves ownership of a field between modules" playbook),
not a sign this decision was wrong today. Recorded here explicitly so
that future migration has its reasoning to check against, rather than
re-litigating the question from nothing.

**Decision:** `penalty_engine` is the sole publisher, regardless of
which UI surface called `emergency_freeze()` (Discord command, a
future physical panic button) — the *write* (`freeze_periods`,
reason=`emergency_override`), not the *trigger surface*, determines the
owning module, consistent with every other event in this catalog. No
new "Emergency Service"/"Safety Manager" module was introduced — doing
so would require a new aggregate/table that does not exist and is not
justified by anything in `philosophy.md` (I16 requires zero
cross-module *preconditions* for Emergency Override, not a dedicated
owning module; those are different properties). `emergency_override.triggered`
is emitted in the same transaction as the generic `freeze_periods.opened`,
as a second, additional, more specific event for this one
particularly significant reason — the same "one write, two events"
shape already used for `penalty_window.extended` +
`.target_duration_changed`.

**Applied to:** `penalty_window_technical_design.md` §4.2.

---

## 6. One Nuance Aggregate Owner Surfaces

`activity_authorization.freeze_confirmed`/`.resume_confirmed` are
published by `activity_authorization` but their Aggregate Owner is
still `ActivityAuthorizationDecision` (its own `lifecycle_status`), not
`FreezePeriod` — even though they are *triggered* by a `FreezePeriod`
event. This is correct, not an inconsistency: the event is fundamentally
about "what happened to my own decision's lifecycle," using the
`FreezePeriod` event only as its trigger, the same way a person can
react to a phone call without the resulting action being *about* the
phone call. Publisher and Aggregate Owner coincide for almost every
other event in this catalog; this is the one place they diverge, and
the divergence is itself informative — it is exactly what distinguishes
"I emit this because something happened to a state I own" from "I emit
this because I decided to act on being told something."

---

## 7. What Remains Genuinely Undefined (Not a Gap in This Catalog — a Gap Upstream)

Unchanged from the first version of this catalog — these are not
resolved because resolving them would mean designing a domain
mechanism, not reconciling event names:

- What produces `IncidentEvidence` (`trust_manager_technical_design.md`, open question).
- What produces `GoalEvidence` (`goal_technical_design.md` §4.2).
- Who authors `RecoveryTask` content (`recovery_plan_technical_design.md` §10, open question 1).

---

## 8. Registered for Later: Policy Engine

Unchanged from the first version of this catalog — noted, not
designed: `disabled`/`allowed`/`required` is a plausible reusable shape
across Verification, Activity Authorization, Hygiene, tokens, and
future AI-autonomy contexts. No data model, no event, no code exists
for it, and none should yet.

---

## 9. What This Means for the Future Transactional Outbox (Preview, Not Design)

With all five naming/publisher findings resolved, Phase 1.4 now has a
settled input: every row in Section 3's registry becomes a literal
`event_type` string constant and a `source_module` value, with no
naming decision left open. `apply_transition()`'s already-present
`events=` parameter (`infrastructure/database.py`) gets its first real
callables, one module at a time, starting with whichever module Phase
1.4 targets first.

**Finding 6 — resolved at the start of Phase 1.4:**
`hygiene_privilege.policy_evaluated` and
`.override_determination_recorded` remain in the shared `domain_events`
outbox, despite having no consumer today. Simplicity won over
minimalism here: one mechanism for every event, with no
per-event-type special case for "this one skips the outbox," is worth
more than the small storage cost of two audit-only rows —
`implementation_conventions.md` Section 5 already frames the outbox as
the uniform path for every cross-module-relevant event, and carving out
an exception this early would be the kind of premature optimization
this project's own methodology (observe repeated need, then
generalize — never the reverse) argues against. If these two events
are still consumerless a year from now, that is a decision to revisit
then, with real evidence, not a reason to special-case them today.
