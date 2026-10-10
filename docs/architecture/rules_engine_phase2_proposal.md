# Phase 2 — Lock Decision Loop and Rules Engine: Design Proposal (v0.1)

> **Status: proposal for owner review. Nothing here is implemented.**
> Written before the sanitised Chaster lock sample arrives; every rule that
> depends on `isFrozen`, `canBeUnlocked` or `keyholder` is marked
> **BLOCKED** until that sample is verified.

## 1. What exists today (verified in code and design documents)

- **Trust**: per-domain `score` and `confidence` (`trust_manager`), now fed by
  incidents *and* goal assessments (Phase 1). Read-only API: `get_domain_state()`.
- **Penalty Engine** already owns an internal *Penalty Window* and
  **Extension** (`extension_technical_design.md`): incident -> hours added to the
  window, with eligibility / magnitude / mitigation / capacity-cap and an
  auditable `ExtensionDecision`. It also owns freeze reasons. This is an
  **internal** model; it does not touch Chaster.
- **A drafted but unapproved design already covers this layer**:
  `relationship_decision_engine_technical_design.md` (Relationship Engine ->
  Decision Engine -> one `Decision` with a mandatory `explanation`; Entitlement
  classes *Absolute / Guaranteed / Discretionary*; DEC-6: effects are applied only
  through the owning module's public API; Hidden Token Economy never exposed).
- **Chaster is read-only** (`list_active_locks`) plus the isolated
  emergency-unlock plane. There is no write path.
- `philosophy.md` 4.2: prohibited categories are never unlocked by favourable
  internal standing or any runtime exception.

**Consequence:** Phase 2 should be the *deterministic core of the Decision
Engine already drafted*, not a second parallel engine. Otherwise the repo ends up
with two competing "decision" layers.

## 2. Proposed scope of Phase 2 (decisions only, no execution)

A pure, deterministic `RulesEngine.evaluate(inputs, ruleset, now) -> Decision`
(new package `rules_engine/`; imports nothing from `conversation_engine`, nothing
from `chaster`'s write side — it does not exist).

**Inputs** (an immutable snapshot assembled by the application from each module's
public read API; no raw table reads):
`trust_score`, `trust_confidence` (per domain), recent goal-assessment outcomes,
Penalty Window state, provider-reported lock view (age + staleness from
`chaster/llm_lock_view.py`), the active `Ruleset`, and the time of the last
applied decision per action type.

**Output — `Decision`** (frozen dataclass, persisted append-only):
`id, action_type, parameters, entitlement_class, inputs_digest (the exact values
used), rule_id, ruleset_version, reason (mandatory, non-empty), status`.
`action_type` is a closed enum:

| Action | Meaning | Status in Phase 2 |
|---|---|---|
| `NO_ACTION` | the common case; still recorded with a reason | ready |
| `ADD_TIME(seconds)` | lengthen the lock | ready as a decision; BLOCKED for execution until Phase 3 |
| `REMOVE_TIME(seconds)` | shorten the lock | same |
| `FREEZE` / `UNFREEZE` | | **BLOCKED**: needs verified `isFrozen` semantics |
| `REVEAL_CODE_PART` | release part of a combination | **excluded from Phase 2** (see 5) |

The engine never executes anything. Execution is Phase 3, and only decisions
produced here may ever be executed.

## 3. How trust thresholds and goal outcomes become decisions

Rules are **configurable data**, validated at load (fail-fast), versioned, and
changed only through a consent-gated service (same `critical_change` discipline
as Trust constants). Shape:

```
Rule { id, domain, when: [Condition], action: ActionTemplate, cooldown, limits }
Condition: trust_score (>=,<=) T  AND  confidence >= C  AND  trend / recent
           goal outcome (e.g. 2 consecutive setbacks) AND lock_view fresh
```

Illustrative (numbers are placeholders for you to set, not proposals):
- sustained high trust and confidence above a floor -> `REMOVE_TIME` of a small step;
- a setback streak, or trust below a floor with sufficient confidence -> `ADD_TIME`
  of a small step;
- **low confidence never triggers a reward or a penalty** (a weak estimate has
  limited authority — Trust Manager 3.6 already says so).

**Hard safety limits the engine itself cannot exceed** (enforced in code, outside
the ruleset): maximum seconds added / removed per rolling period; maximum number
of actions per period; a minimum cooldown between actions in the same direction;
no action if provider data is older than a freshness bound or the lock state is
unknown; a global `rules_engine_enabled` flag (default off).

## 4. Determinism, audit and replay

- Same inputs + same `ruleset_version` + same `now` -> identical `Decision`.
  `now` comes only from the clock abstraction.
- A decision is persisted in the same transaction as its audit row, via a domain
  service (new migration 025: `lock_decisions`, `lock_rulesets`). Append-only.
- Replay: a stored `inputs_digest` lets any decision be re-evaluated and compared.
- Triggers are events, not the LLM: `trust_domain.recalculated` and
  `goal_accountability_assessment.recorded` enqueue an evaluation through the
  existing outbox; there is no Discord, command or model entry point.

## 5. Entitlement classes and the hard line

- **Absolute / prohibited** (never reaches the engine's discretion): emergency
  unlock plane (stays isolated; `application/service.py` still never mentions it)
  and anything `philosophy.md` 4.2 lists as prohibited.
- **Code or combination reveal is explicitly out of Phase 2.** It is irreversible
  and, as worded, in tension with 4.2 ("never unlocked by favourable internal
  standing"). It needs its own design review with you; I propose it stays out of
  the action enum until then.
- Time changes are **Guaranteed** (fixed rule on current state) in this phase.
  The Hidden Token Economy / Discretionary class are left to the Decision
  Engine document and are not introduced here.

## 6. Relationship to existing modules

- **Penalty Engine / Extension**: stays the owner of the *internal* penalty
  window. Open question: should an `ADD_TIME` decision be derived from an
  `ExtensionDecision` (one source of truth), or be an independent trust-driven
  rule? I recommend the former for incident-driven additions, with trust/goal
  rules covering the rest, to avoid two systems adding time for one event.
- **Persona (Phase 4)**: receives the final, executed `Decision.reason` as
  verified context. The model cannot alter it.

## 7. Test plan

Pure unit tests for every rule/condition and each safety limit (property-style:
no input can exceed the per-period cap); ruleset validation failures; determinism
and replay; "LLM cannot reach it" (import/AST guard, as for `lock_state`);
event-driven integration through `on_system_startup`; the existing
no-domain-writes and `emergency` guards stay green.

## 8. Decisions needed from you

1. **Align with the drafted Decision Engine document** (recommended) or treat the
   Rules Engine as a separate package? If aligned, do you approve that draft's
   entitlement vocabulary (Absolute / Guaranteed / Discretionary)?
2. **Time unit and limits**: allowed step sizes, per-period caps, cooldowns, and
   the freshness bound for Chaster data.
3. **Penalty Window relationship** (section 6): derive incident-driven time from
   `ExtensionDecision`, or keep the two independent?
4. **Reveal of code parts**: confirm it is excluded from Phase 2.
5. **Ruleset ownership**: who may change rules, and through which consent
   mechanism (the same `critical_change` consent as Trust constants)?
6. **Chaster sample** (your sanitised JSON): needed before FREEZE/UNFREEZE and any
   rule reading `canBeUnlocked` / `keyholder`. Please also say whether the
   keyholder in this setup is a Chaster-side keyholder or you as self-lock.
7. **Dry-run**: confirm Phase 2 ships decisions in a log/visible-to-owner only
   mode, with execution entirely deferred to Phase 3.

## 9. Not in this proposal

No Chaster write endpoints or OAuth scopes (Phase 3 must verify them against the
official documentation, not memory), no persona changes, no scheduled
recalculation, no Hidden Token Economy.
