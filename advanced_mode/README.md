# Advanced Mode

Canonical design: `docs/architecture/advanced_mode_technical_design.md`
(**still `Draft for review, not approved for implementation` as a
whole document** — this README describes exactly which specific slice
of that draft has been implemented here, and nothing more).

## What is implemented here

**Now reachable manually via Discord DM** — `mode`/`mode status`/
`mode request advanced`/`mode request standard`/`mode cancel`/
`mode confirm`, wired through `application/service.py`. This module's
own API is unchanged by that; the application layer is simply its
first real caller. See `application/README.md` for the integration
itself (the explicit settle-before-act orchestration, and how a stable
Discord message id becomes the two independent consent references
`request_transition()`/`confirm_transition()` need).

- **`OperatingMode`** (`standard`/`advanced`) as a **global singleton**
  — not per-user. The current domain core (`trust_manager`,
  `penalty_engine`) has no `user_id` anywhere; it is single-subject by
  design. This reflects the project's own **current** architecture, not
  a statement about future multi-user support. `UserAccount`
  (`application/`) exists only for Discord-channel-identity
  bookkeeping — tying `OperatingMode` to it would have been the first
  place in this project where a genuinely normative domain concept was
  scoped to one particular channel identity rather than the system as
  a whole. Mirrors `system_startup_lease`'s own singleton pattern
  (`id INTEGER PRIMARY KEY CHECK (id = 1)`, migration 006).
- **The two-stage `critical_change` transition process**
  (`mode_transition_requests`) — seven distinct statuses
  (`BLOCKED_BY_PENALTY_WINDOW`, `WAITING`, `PAUSED_BY_PENALTY_WINDOW`,
  `AWAITING_CONFIRMATION`, `CANCELLED`, `COMPLETED`, `INVALIDATED`),
  two independent consent references (`requested_via_consent_id`,
  `confirmed_via_consent_id`), a 24-hour uninterrupted wait, and a
  minimum 30 days in Advanced before a Standard request is even
  possible.
- **`INVALIDATED`** (added under direct review, migration 018) --
  `confirm_transition()` re-reads `operating_mode_state` and compares
  it against the request's own `source_mode`, atomically, inside the
  same write transaction, BEFORE the Penalty Window check. A mismatch
  means the request's original premise -- confirming a transition FROM
  a specific starting mode -- is no longer valid (something else
  changed `OperatingMode` in the meantime). Distinct from `CANCELLED`
  (not an explicit user cancellation) and from `COMPLETED`/`PAUSED`
  (the request's *premise*, not merely its timing, is wrong). Follows
  the exact same commit-then-raise discipline as
  `ModeTransitionInterruptedByPenaltyWindowError`: the row is committed
  to `INVALIDATED` first, `ModeTransitionSourceModeMismatchError` is
  raised only after that transaction has already returned normally.
- **`advanced_mode/repository.py`** — two structurally separate public
  classes, the same split `task_catalog` established first in this
  project:
  - **`AdvancedMode`** — read-only (`get_current_mode`,
    `get_active_request`). No write method exists on this class at
    all — verified directly by
    `TestAdvancedModeHasNoWriteCapability`. Never applies a lazy state
    transition as a side effect of being read.
  - **`AdvancedModeAdministration`** — `critical_change`-governed write
    API: `request_transition`, `cancel_request`, `confirm_transition`,
    and the explicit reconciliation command `advance_transition_state`.
- **`penalty_engine/repository.py`'s new
  `get_active_or_frozen_penalty_window_in_transaction(tx)`** — the one
  necessary change outside this new module, flagged and confirmed
  before implementation. The existing public
  `get_active_or_frozen_penalty_window()` now delegates to it, so the
  underlying query is never duplicated. Its own docstring is explicit
  about exactly what it does and does not guarantee — see "The
  conservative Penalty Window contract" below.
- **`database/migrations/017_advanced_mode.sql`** —
  `operating_mode_state` (bootstrapped to `standard` on a fresh
  install), `mode_transition_requests`, and a partial unique index
  enforcing at most one non-terminal request globally.
- **`database/migrations/018_advanced_mode_invalidated_status.sql`** —
  `invalidated_at`, and the partial unique index recreated (`DROP` +
  `CREATE`, migration 017 itself untouched) to also treat `INVALIDATED`
  as terminal.
- **56 tests** (53 in `tests/advanced_mode/`, 3 in
  `tests/penalty_engine/` for the new transaction-scoped read),
  including a real multi-threaded concurrency test for
  `request_transition()`, failure-injection tests proving both
  `confirm_transition()` branches' cross-table atomicity, and dedicated
  tests that reopen a **fresh database connection** (not the
  exception's own attached object) to verify neither
  `ModeTransitionInterruptedByPenaltyWindowError` nor
  `ModeTransitionSourceModeMismatchError` ever claims a state that
  wasn't actually committed.

## The explicit reconciliation command — and why it isn't hidden in a read

There is **no background scheduler anywhere in this project**
(deferred since Phase 0, still deferred). The time-driven transition
`WAITING -> AWAITING_CONFIRMATION` cannot happen automatically at the
moment 24 hours elapses — it has to be evaluated lazily, the next time
something asks. The first design of this slice made `get_active_request()`
apply that evaluation internally, as a side effect of reading. **This
was rejected under direct review**: a method presented as read-only
must never have hidden state-changing behavior — it breaks the same
read/write separation `task_catalog` already established, and
complicates every caller's transactional expectations, testing, and
audit story.

Instead: **`advance_transition_state(penalty_engine, now)`** is the
*only* place deterministic, time/Penalty-Window-driven transitions are
applied -- `AdvancedModeAdministration`'s own write API, never
`AdvancedMode`'s read API. It is idempotent (a stable state produces no
`UPDATE` and an unchanged `wait_started_at`/`confirmable_at` -- verified
directly, not merely asserted) and applies **at most one** transition
per call, never cascading multiple steps (e.g. `PAUSED_BY_PENALTY_WINDOW`
restarting into `WAITING` must not, in the same call, continue into
`AWAITING_CONFIRMATION` -- the freshly restarted 24-hour wait has
obviously not elapsed). **Now called from Discord**, via
`application/service.py`'s own explicit
`ApplicationService._settle_mode_state()` -- invoked at the start of
every `mode ...` command, never hidden inside a read there either. No
other trigger exists yet (a startup hook, or a periodic call
independent of any incoming message) -- a user's own next `mode ...`
message remains the only thing that settles state; see
`application/README.md`.

## The exception-inside-a-transaction lesson (`confirm_transition()`)

`Database.transaction()`'s own docstring: *"Commits on normal exit,
rolls back on any exception raised inside the `with` block."* An
earlier draft of `confirm_transition()` would have written
`PAUSED_BY_PENALTY_WINDOW` and then raised
`ModeTransitionInterruptedByPenaltyWindowError` from inside the same
transactional `write()` closure -- which would have rolled back the
very write the exception's own message claims happened. **Caught under
direct review before implementation, not after.**

The actual implementation: `write()` returns normally in both branches
(`(request, was_interrupted)`), so the transaction always commits
first. `confirm_transition()` raises `ModeTransitionInterruptedByPenaltyWindowError`
**strictly outside** `apply_transition()`'s own call -- by the time the
exception exists, the `PAUSED_BY_PENALTY_WINDOW` write is already
durable. Verified two ways: the exception's own `.request` attribute,
and -- independently, since checking only the attached object isn't
proof the write reached disk -- a **fresh `Database` connection** opened
after the exception is caught.

## The `source_mode` mismatch lesson (`INVALIDATED`)

A second gap found under direct review, same class as the one above:
`confirm_transition()`'s first draft went straight from checking
`status == AWAITING_CONFIRMATION` to writing `target_mode` as the new
current mode — it never re-read `operating_mode_state` and compared it
against the request's own `source_mode`. If `OperatingMode` changed via
any other path between request creation and confirmation, confirmation
proceeded anyway, silently overwriting whatever that other change had
set.

The fix re-reads `operating_mode_state`, atomically, inside the same
write transaction — **before** the Penalty Window check (there is no
point checking Penalty Window for a request whose own starting premise
already no longer holds). A mismatch commits the request to
`INVALIDATED` — deliberately not `CANCELLED` (this isn't an explicit
user cancellation) and not `COMPLETED`/`PAUSED` (the request's
*premise*, not merely its timing, is wrong) — following the exact same
commit-then-raise discipline as
`ModeTransitionInterruptedByPenaltyWindowError`:
`ModeTransitionSourceModeMismatchError` is raised only after the
`INVALIDATED` write has already committed.

## The conservative Penalty Window contract

**Advanced Mode works with `penalty_engine`'s own *persisted* status,
not a time-settled one.** `get_active_or_frozen_penalty_window_in_transaction(tx)`
is a raw `SELECT` against `penalty_windows.status` — it never calls
`PenaltyEngine.ensure_current_state(now)`, whose own docstring
describes it as *"the mandatory precondition... called at the start of
every operation that depends on the window's state."* `ensure_current_state()`
opens its own separate transactions and publishes its own domain
events on completion — it cannot safely run nested inside Advanced
Mode's own write transaction, and building a genuinely transactional
settlement variant would mean either entangling `penalty_engine`'s own
event-publishing into Advanced Mode's transaction (wrong layering), or
a "cheap" read that diverges from what `penalty_windows.status` itself
still says until something else actually settles it.

**Practical consequence, deliberately accepted for this iteration:** a
Penalty Window whose target duration has elapsed by wall-clock time,
but which nothing has yet called `ensure_current_state(now)` for,
still reads as `active`/`frozen` here. `advance_transition_state()`/
`confirm_transition()` may therefore keep a mode transition blocked
*longer* than the theoretical countdown-completion moment would
suggest — this is a **conservative** direction only. It can never let a
transition proceed *during* a genuinely active or frozen Penalty
Window; it can only delay a transition that should already be free to
proceed, until something settles the window through its own, separate
call path.

This is **not new to Advanced Mode** — `application/service.py`'s own
`status` command reads `get_active_or_frozen_penalty_window()` the same
way, with no `ensure_current_state()` call either. Advanced Mode
inherited an existing, project-wide gap; it did not introduce a new
one.

### Project-wide open point (not an Advanced Mode task)

> Design a unified way for every operation that depends on Penalty
> Window state to get a genuinely time-settled answer, without a
> hidden caller precondition, without nested transactions, and without
> breaking `penalty_engine`'s own domain-event flow.

Known to affect, at minimum: Advanced Mode (this module),
`application/service.py`'s `status` command, and potentially any
future consumer of `PenaltyEngine`'s read API. Not resolved here, and
this document's own global status is unaffected by leaving it open.

## Invariant enforcement -- precisely, by layer

Per explicit review requirement: this README does not claim a stronger
guarantee than what actually enforces it.

| Invariant | Enforced by |
|---|---|
| At most one non-terminal `mode_transition_requests` row (MODE-1) | **Both**: `AdvancedModeAdministration.request_transition()`'s own check (application), and `idx_one_active_mode_transition_request` (a partial unique index over a constant expression -- SQLite-level, verified directly against this exact SQLite version before being proposed, and again via `TestMode1DatabaseConstraint`) |
| `target_mode != source_mode` | **Both**: the table's own `CHECK (target_mode != source_mode)` constraint (database), and `request_transition()`'s own check before that row is ever built (application) |
| `operating_mode_state` has exactly one row | **Database only**: `id INTEGER PRIMARY KEY CHECK (id = 1)` |
| `source_mode` still matches the actual current `OperatingMode` at confirmation | **Application only** — `confirm_transition()`'s own re-read of `operating_mode_state`, atomically inside the same write transaction, checked *before* the Penalty Window check. No database constraint (would require comparing against another table's current row at write time — SQLite `CHECK` cannot reference another table). A mismatch commits `INVALIDATED` and raises `ModeTransitionSourceModeMismatchError` strictly after that commit — the same commit-then-raise discipline as the Penalty Window interruption below. |
| Eligibility/current-version field-combination invariants (e.g. `AWAITING_CONFIRMATION => confirmable_at is not None`, `PAUSED_BY_PENALTY_WINDOW => confirmable_at is None`, `INVALIDATED => invalidated_at is not None and confirmed_at is None and cancelled_at is None`) | **Application only** -- documented on `ModeTransitionRequest`'s own docstring, not a database `CHECK`. No SQLite constraint expresses "if status = X then column Y must/must not be NULL" without a much larger, unwarranted schema change for this slice. |
| 30-day minimum before Advanced -> Standard | **Application only** -- `request_transition()`'s own check against `operating_mode_state.mode_activated_at`. No database constraint (would require comparing against the current wall-clock time, which SQLite `CHECK` constraints cannot reference). |
| A non-empty consent reference on every write | **Application only** -- `_require_consent_id()`, called by every `AdvancedModeAdministration` method. No `NOT NULL`-equivalent check exists for "non-empty after `.strip()`" at the SQL level (the columns themselves are `NOT NULL`/nullable as appropriate, which is a weaker guarantee than "non-empty, non-whitespace"). |
| `confirm_transition()` never leaves `confirmed_via_consent_id` set without `OperatingMode` actually having changed | **Application only**, via the exception-outside-transaction discipline above -- not a database constraint, a transactional/control-flow guarantee. |
| `PAUSED_BY_PENALTY_WINDOW`/`AWAITING_CONFIRMATION` requiring a Penalty Window check | **Application only**, reading `penalty_engine`'s own *persisted* status through `get_active_or_frozen_penalty_window_in_transaction(tx)` -- **not** a time-settled/"live" check; see "The conservative Penalty Window contract" section below for exactly what this does and does not guarantee. |

## What is explicitly NOT implemented -- still draft, still open

- **No `DelegatedAuthorityPolicy`, no Authority Matrix as code.** No AI
  decision-making exists anywhere in this codebase to gate by it.
- **No change to `MAX_TARGET_ACTIVE_HOURS`/`penalty_engine`'s own
  target-duration algorithm.** `OperatingMode` exists, but nothing
  reads it to change Penalty Window duration yet.
- **No Advanced token economy, no Hygiene `min()` values, no Carry
  Bank, no Equipment Inventory, no Task assignment, no
  `originating_mode` on any task instance.** All remain exactly as open
  as `advanced_mode_technical_design.md` itself describes.
- **No background scheduler** -- and none is introduced by this slice;
  see the reconciliation-command section above. `mode ...` commands
  only settle state when a user actually sends one (see
  `application/README.md`'s own note on `_settle_mode_state()`); there
  is still no automatic notification the moment 24 hours actually
  elapses with nobody watching.

## Three levels (owner, 2026-11-16: "beginner, standard and advanced")

`OperatingMode.BEGINNER` joins Standard and Advanced (`MODE_RANK`, `is_lowering`). Moving down (Advanced -> Standard, anything -> Beginner)
lowers the difficulty: the Iron Contract allows it only while unlocked (`mode request beginner` and a lowering `mode confirm`); moving up
never needs that. Every change keeps the 24-hour wait and the second confirmation; 30 days in Advanced before going down from it.
`AdvancedModeAdministration.start_new_wearer_at_beginner` moves a new additional wearer's database (never transitioned, still at its seeded
Standard) to Beginner once (event `advanced_mode.started_at_beginner`). What Beginner means elsewhere: tokens (`release_engine/conditioning.py`:
base 1.0, +0.025 per release, cap 1.5, the owner's own start), lines (`lines/service.py`: 10 / 20, release lines 10), penalty windows as
Standard, and `system/graduation.py`: after 28 days at Beginner without a confirmed severe incident, the wearer is asked once to move up.
