# rules_engine — Phase 2 (STRICT DRY RUN)

Canonical: `docs/architecture/rules_engine_phase2_proposal.md` (approved) and
`docs/architecture/relationship_decision_engine_technical_design.md` (Decision,
Entitlement vocabulary). This package is the deterministic core of that Decision
Engine. **It executes nothing: no Chaster call exists anywhere in it.**

## Behaviour

- `evaluate(inputs, ruleset) -> DecisionOutcome` (`engine.py`) is pure: no
  database, clock, network or model. Same inputs, same outcome.
- Order: (1) absolute safety gates, (2) first matching rule, (3) the action
  must be valid for the lock's state, (4) hard limits. Every outcome, including
  `NO_ACTION`, has a non-empty reason (DEC-2).
- **Actions:** `NO_ACTION`, `ADD_TIME`, `REMOVE_TIME`, `FREEZE`, `UNFREEZE`.
  FREEZE/UNFREEZE are valid but **dormant** (no shipped rule uses them) and are
  checked against `isFrozen`. There is no code-reveal action (excluded by the
  owner).
- **Entitlement:** safety gates and limit refusals are `ABSOLUTE`; matched rules
  are `GUARANTEED`; `DISCRETIONARY` is reserved and never emitted.
- **Safety gates (fail closed, `NO_ACTION`):** no trust data; no lock data;
  lock data older than 15 minutes (or timestamped in the future); status not
  `locked`; not confirmed a keyholder lock; `isFrozen` unknown; a time change on
  a frozen lock.
- **Hard limits (`limits.py`, outside the ruleset, cannot be raised by it):**
  1-hour unit, max 6 hours per action, max 12 hours added and 6 removed per
  rolling 24 hours, max 2 actions per 24 hours, 4 hours minimum between actions.
  Amounts are clamped to the remaining cap; below one hour left is a refusal.
- **Rulesets (`ruleset.py`)** are system-configured data validated fail-fast
  (unique ids, bounded hours, thresholds in [0, 1], no unconditional rule). No
  runtime mutation and no model path. `DEFAULT_RULESET` thresholds are
  `BOOTSTRAP_DEFAULT` placeholders.

## Triggers and persistence (migration 025)

- Only `goal_accountability_assessment.recorded` triggers an evaluation.
  **Incident-driven time is deliberately not handled here**; it derives from the
  Penalty Engine's `ExtensionDecision`, so one incident is never punished twice.
- The outbox handler (`record_trigger_in_transaction`) only writes a
  `lock_decision_triggers` row (id = event id, idempotent). `evaluate_pending()`
  runs afterwards, reads trust state and the optional lock snapshot through
  injected read-only callables, and writes one append-only `lock_decisions` row
  per trigger (`UNIQUE(trigger_id)`) plus a `lock_decision.recorded` event.
  `inputs_json` stores the exact inputs for audit and replay. `status` is always
  `dry_run`.
- Setback/progress streaks come from the trigger history; threshold crossings
  compare the score with the one recorded by the previous decision.

## Enabling

`ENABLE_RULES_ENGINE=true` (default `false`). When off, no consumer is
registered and nothing is recorded. `system/startup.py` runs `evaluate_pending`
after the outbox drains; a failure there is logged and never aborts startup.

## Not done yet

- **No lock-snapshot provider is wired.** `bot/discord_bot.py` passes none, so
  with the flag on every decision is `NO_ACTION` ("no lock data"). Supplying a
  fresh `LockSnapshot` (from persisted Chaster observations) belongs with Phase 3.
- Evaluation currently runs at startup only (that is when the outbox drains).
- The `isFrozen` / `canBeUnlocked` / `keyholder` fields remain unconfirmed
  against a real response; the sample provided in chat is a description, not a
  verified payload.
- Execution, the Hidden Token Economy and the Relationship Engine are out of scope.
