# release_engine — Single Barrier release, domain core (v0.39 Phase 1)

Canonical: `docs/architecture/v039_adaptive_release_and_supervision_proposal.md`
(final, section 11) and `activity_authorization_technical_design.md` (ledger shape,
`commit_authorization`). **The domain core never calls Chaster itself.** `system/release_tick.py` (`ReleaseEnginePeriodicTask`) drives it from
the periodic cycle behind `ENABLE_RELEASE_ENGINE`; the barrier is lowered through `chaster_execution`. No model, Discord or conversation path can reach it
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

## Phase 2: the periodic tick (`system/release_tick.py`)
One pass per cycle: sync provider states from the execution plane -> stop if a release is in flight -> hard
vetoes (penalty window, supervision, missed check-in) cancel any plan -> requirement met (balance >= 1) and the
barrier confirmed up -> ensure the secret plan -> plan due -> `commit_authorization()` (spends the token) and record an
`UNFREEZE` decision under rule `barrier.release` -> backstop when the barrier has been up `MAX_BARRIER_HOLD_HOURS`
(72, placeholder; ignores vetoes, spends nothing, still needs a valid window and a confirmed barrier).
- **Never spends for a release that cannot happen:** nothing is committed unless the execution plane is active and the
  runtime halt is clear (planning-only otherwise). A decision that will never execute (older than ~16 minutes) is undone:
  the spend is reversed.
- **Provider mapping:** decision `executing` -> `provider_requested`; `executed` -> `provider_confirmed`;
  `failed`/`blocked`/`expired` -> `provider_failed` (refund); `needs_review` -> `physical_state_unknown` (spend kept,
  human review, no new plan while open).
- **Chaster reads are minimal:** the observation is refreshed (read-only) at most once per 15 minutes and only when a
  decision depends on it. Raising the barrier (`FREEZE`) is NOT automated: it stays a manual / Chaster-side act.
- **Model-facing:** `ReleaseEngine.coarse_phase()` returns only `requirements_met_pending` / `requirements_not_met`; the
  composition root hands it to the prompt through `ReleasePhaseContextProvider` (guardrail rule 6 forbids any timing).
- Weekends and public holidays use a 10:00 base (`WEEKEND_RELEASE_START`) + the same 30-210 minute offset.

## Not built yet
Token earning from goal/task events (no amount was specified; tokens can only be earned through the domain service
today), automatic barrier raising, hygiene.

