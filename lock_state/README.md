# Lock State

Canonical design: `docs/architecture/lock_state_technical_design.md`
(**`Draft for review, not approved for implementation`** — this README
describes exactly which specific slice of that draft has been
implemented here).

## Epistemic invariant — read this first

> **Lock state in this slice is user-reported state, not verification
> of physical reality.**

Without external hardware or a provider integration (a future,
separate slice), this system cannot know whether physical keys are
actually secured. Every status this module can represent is what the
**user told the system**, never a verified physical fact. No status
here is named or may be interpreted as `VERIFIED_LOCKED`,
`KEYS_IN_LOCKBOX`, `PHYSICALLY_SECURED`, or anything else implying
technical verification.

## What is implemented here

**`models.py`**:
- `LockReportStatus` — exactly two members, `LOCKED_USER_REPORTED` /
  `UNLOCKED_USER_REPORTED`. The only values ever persisted. Both
  explicitly carry `_USER_REPORTED` in their own name, at every call
  site, not only in a docstring.
- `LockKnowledgeState` — three members, adding `UNKNOWN`. The
  read-result type. `UNKNOWN` is never persisted as a fake row — it is
  what `get_current_knowledge_state()` returns when no report exists
  yet. It is never conflated with `UNLOCKED_USER_REPORTED`: absence of
  information is not evidence of an unlocked state, any more than it
  is evidence of a locked one.
- `LockReport` — immutable. `id`, `user_id`, `status`,
  `sequence_number`, `reported_at`, `reported_via_consent_id`.

**`repository.py`**:
- `LockState` — read-only. `get_current_report(user_id) -> LockReport | None`
  and `get_current_knowledge_state(user_id) -> LockKnowledgeState`
  (the primary read method for most callers). No write method exists
  on this class.
- `LockStateAdministration` — governed write.
  `report_status(*, user_id, status, reported_via_consent_id, now) -> LockReport`.
  Requires a non-empty `reported_via_consent_id` — the same audit-trail
  discipline every other governed write in this project already uses
  (`task_catalog`, `advanced_mode`).

## Persistence — migration 019

`lock_reports` — **append-only**. No `UPDATE`, no `DELETE`, ever, by
application code — the same discipline `task_template_versions`
(migration 014) and `mode_transition_requests` (migration 017) already
apply to their own append-only data. Every report is a new row; "the
current state" is the most recent row for that user, never mutated in
place.

`sequence_number` is the deterministic ordering tiebreaker — assigned
monotonically per `user_id` inside the same write transaction (the
same precedent `goal_management`'s own append-only `version` column
already established). Two reports made in the same instant are still
ordered correctly, never left to depend on timestamp precision or
SQLite's own implicit row order.

`user_id` is this project's existing `UserAccount.id` boundary —
never a raw Discord identifier.

## Failure behavior

- A failed `report_status()` call (invalid input, a foreign-key
  violation) writes nothing — the whole attempt rolls back atomically
  (`infrastructure.database.apply_transition`'s own transactional
  guarantee). No partial or inconsistent row is ever left behind.
- Read methods (`get_current_report`, `get_current_knowledge_state`)
  never write anything, under any circumstance.

## What is implemented here — Slice C addition

**`lock status`/`lock report locked`/`lock report unlocked`** are now
wired into `application/service.py` — `LockState`/`LockStateAdministration`
constructed directly inside `ApplicationService.__init__`, the same
pattern `advanced_mode` already uses. `lock` is its own command family
(`register_family`) — an invalid `lock ...` input gets a deterministic
family reply, never falling through to Conversation Engine. See
`application/README.md` for the full command surface and its own
verified invariants.

## What is explicitly NOT implemented — still draft, still open

- **No `ConversationContextProvider`, no Conversation Engine wiring.**
  The engine does not know this module exists yet -- `lock status`/
  `lock report ...` are deterministic commands, fully separate from
  the model-generated conversational path.
- **No external/hardware verification of any kind.** This module's
  entire epistemic ceiling is "what the user said" — nothing more.
- **Reconciliation with Chaster lives in `infractions/lock_claims.py`** (`docs/architecture/backlog_completion.md`): a report is compared with what the provider observed, and
  neither is ever presented as physical verification. This module itself still has no provider integration. Originally: a future,
  separate slice may add **external/provider-observed** state (a
  genuinely different kind of information than a user report) — that
  must never be silently presented as physical verification of keys
  either, and the canonical reconciliation between a user report and a
  provider-observed state remains an explicit future decision, not
  assumed here.
- **No timers, scheduler, or proactive messaging.**
