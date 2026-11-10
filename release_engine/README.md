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
    hard vetoes (penalty window, supervision cancel the plan) →
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
vetoes (penalty window, supervision) cancel any plan -> requirement met (balance >= 1) and the
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

## Release lock read as its keyholder (`RELEASE_LOCK_ID`, owner 2026-11-10)
Without a wearer connection the tick had nothing to observe, so it never ran. With `RELEASE_LOCK_ID` set, `system/keyholder_lock_source.py` reads that one
lock with the keyholder connection (`GET /locks/{id}`) and hands it on only when the connected account's role is `keyholder` and it is `locked`. The
release belongs to `CHASTER_EXECUTION_WEARER_USER_ID`, else `CHECK_IN_USER_ID`, else `OWNER_USER_ID`. Everything after the observation is unchanged.

## Phase 3: token earning and the re-freeze
- **Earning (owner rule, superseding the per-goal token):** tokens come EXCLUSIVELY from the daily Token Progress Pool
  (`token_pool.py`, evaluated at local midnight by `system/daily_evaluation.py`): `P_daily = (0.75 * goals_met/goals_assigned
  + 0.25 * Trust) * 0.286` (a day without goals counts as a goal ratio of 1.0); each full 1.0 earns one token
  (`token_pool.earned`, idempotent per date) and is subtracted. A completed goal earns nothing by itself.
- **Release session:** the discretionary window in which the provider confirmed a release (`last_confirmed_release()`,
  `release_session_over()`). It ends when quiet or workday hours begin or the local date changes (this is also the daily
  session expiry). While it lasts the token balance is naturally 0 (the token paid for this release), so the balance rule below
  applies only outside a session.
- **Re-freeze (`system/release_tick.py`, `barrier.freeze`):** once the session is over and a fresh observation shows a locked
  keyholder lock that is NOT frozen, one `FREEZE` decision is recorded per release (reason code: window ended / no token).
  It only ever re-raises a barrier this system lowered (a lock that was never released by the engine is left alone), is not done
  while a release is in flight, needs execution to be active and the runtime halt clear, and is retried up to 3 times with a
  30-minute back-off after a blocked / failed / expired attempt. `needs_review` is never retried.
- **Limits:** the barrier pool (2 per 24 h, 4 h cooldown) counts RELEASES (unfreezes). A re-freeze is never blocked and does not
  use the release budget, otherwise a release and its re-freeze would block the next day's release.

## v0.39 Phase F4: Progressive Conditioning (`conditioning.py`, migration 038, `ENABLE_SLAVE_TRAINING`)

The pool pays a token at `base(mode) * factor` (base 1.0 Standard, 2.0 Advanced). The hidden factor (1.000 .. 1.500, +0.025) rises only through the consumer of
`release.provider_confirmed` for a `token` authorization, with the owner's per-database switch on and the wearer's `conditioning` on (off freezes it). Never in events,
logs, prompts or wearer replies. See docs/architecture/phase_f_proposal.md section 9.

## Phase J2: Communication-First release (`system/release_notice.py`, migration 045)

A missed check-in is no veto. When every release condition is met, and only then (the plan's timing stays secret), the user is told they are
eligible before anything else happens. If today's check-in is incomplete, that same message requests it and the release waits; the reply releases
them on the next cycle (at most `CHASTER_EXECUTION_INTERVAL_SECONDS`), whatever a rubric makes of it. No token is spent while waiting. An unanswered
prompt expires as `skipped` with no Trust effect; an undeliverable prompt never holds a release; a window that closes meanwhile is explained once.
The backstop never passes through this.
