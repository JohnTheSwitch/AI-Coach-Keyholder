# release_engine — Single Barrier release, domain core (v0.39 Phase 1)

Canonical: `docs/architecture/v039_adaptive_release_and_supervision_proposal.md`
(final, section 11) and `activity_authorization_technical_design.md` (ledger shape,
`commit_authorization`). **Domain core only: nothing calls Chaster and nothing is wired
into the periodic cycle yet.** No model, Discord or conversation path can reach it
(AST-guarded; nothing in `conversation_engine`, `application`, `bot`, `chaster*`,
`ai`, `memory_system` or `rules_engine` may import it).

## What exists

- **Token ledger** (`ledger.py`, migration 027): append-only `token_ledger`
  (`earn | spend | adjustment | reversal`), balance = `SUM(delta)`, no update/delete
  path, one SPEND per authorization (partial unique index), one REVERSAL per entry,
  one EARN per source event. Tokens are a hidden mechanism: nothing here is exposed.
- **Scheduler** (`scheduler.py`, pure): target = `WORKDAY_END` (local) + an offset of
  `UNLOCK_WINDOW_MIN_OFFSET_MINUTES`..`MAX` (30–210) drawn by HMAC-SHA256 from a
  stored secret seed and the local date. Deterministic per (seed, date), so a plan
  survives a restart without redrawing; a different date gives an independent draw
  (used for re-targeting). The result is always checked against the window
  classifier; configuration is rejected if `WORKDAY_END + max offset` would reach quiet
  hours. Weekends and holidays use the same base time (see the proposal, section 11).
- **`ReleaseEngine`** (`service.py`):
  - `ensure_plan()` — requirement met (balance ≥ 1) never unlocks immediately; it
    stores one `planned` plan (secret `target_at` + `seed`) per user.
  - `commit_authorization()` — the commit point (D8). In one transaction: due? →
    hard vetoes (penalty window, supervision, missed check-in cancel the plan) →
    lock barrier confirmed (else retry later) → window must be DISCRETIONARY (else
    re-target) → binding balance re-check → SPEND exactly one token → `authorized`.
  - `commit_backstop()` — `MAX_BARRIER_HOLD_HOURS` safety release: no token, ignores
    token/veto gates, still needs a confirmed barrier and a valid window.
  - Provider progress: `authorized → provider_requested → provider_confirmed |
    provider_failed | physical_state_unknown`. A provably failed request reverses the
    spend (compensating row); an unknown outcome keeps it and needs a human.
- Events carry ids only (`release_plan.created|retargeted|cancelled`,
  `release.authorized|provider_requested|provider_confirmed|provider_failed|
  physical_state_unknown`); never a timestamp, target or seed.

## Not built yet
Periodic tick, barrier raise/lower through `chaster_execution` (freeze/unfreeze with
their own limit pool), the coarse `release_phase` context for the model, the
guardrail text, token earning from goal/task events.
