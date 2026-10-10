# penalty_engine — Slice 1 + Extension

The second real domain module built against the architecture baseline
(`docs/architecture/penalty_window_technical_design.md`,
`docs/architecture/extension_technical_design.md`), consuming Trust
Manager's public read API. Same incremental discipline as
`trust_manager/` and `infrastructure/`.

## What is covered

Canonical sections implemented: **1 (the subset below), 2.1-2.6, 3.1,
3.3 (minus 3.4), 4.1, 4.2 (the subset below), 4.4, 4.5**, plus
**Extension** (`extension_technical_design.md`, all sections).

- **The state machine** — `(none) → ACTIVE`, `ACTIVE ↔ FROZEN`,
  `ACTIVE → COMPLETED` (natural countdown), exactly as Slice 1
  delivered.
- **`ACTIVE → ACTIVE` (Extension)** — `penalty_engine/extension.py`'s
  `should_extend()` (Eligibility → Base Magnitude → Mitigation →
  Capacity Cap), wired into ONE unified incident-consumption path
  (`consume_confirmed_incident()`/`_consume_confirmed_incident_in_transaction()`):
  the first unconsumed Incident starts a window if none is
  ACTIVE/FROZEN; every subsequent unconsumed Incident (including that
  very first one) is consumed through `should_extend()`. Consumption is
  unconditional (`philosophy.md` 3.8); only `extensions_hours` is
  conditional on the decision's `assigned_hours`.
- **Freeze as a set of reasons, startup reconciliation, the public read
  APIs, events** — unchanged from Slice 1.
- **New events**: `extension.decision_recorded` (every consumption,
  eligible or not), `penalty_window.extended`/`.target_duration_changed`
  (only when `assigned_hours > 0`).

## What is deferred to a later slice

- **Recovery Credit integration** (3.4) — depends on Recovery Plan,
  which does not exist yet.
- **`terminate()`** — explicitly deferred by the architecture document
  itself (2.1).
- **Any actual caller of `freeze(reason=partnered_intimacy_authorization)`
  or `freeze(reason=temporary_wear_exemption)`** — mechanism exists and
  is tested generically; Activity Authorization and the exemption
  approval logic (Coach engine) do not exist yet.
- **`occurred_during_recovery_task`** — always `False` in this slice
  (EXT-10; Recovery Plan does not exist to signal it).

## Bootstrap defaults (governance classification)

Added during the Phase 2.7 architecture review, distinguishing "who
decided this exists" (the developer, by necessity) from "who should
have the right to change it going forward" (mostly undecided). Tagged
in code with `# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):`,
verified by `tests/test_bootstrap_default_tags.py` (a format guard
only -- resolving these is not required or scheduled).

| Constant | Created by | Intended owner | Current change mechanism | Why bootstrap default |
|---|---|---|---|---|
| `DEFAULT_BASE_DURATION_HOURS` (`window.py`) | developer | undecided, plausibly **user** | code | Directly determines how long a base penalty lasts -- a felt behavioral consequence, not a technical parameter. |
| `BASE_HOURS_BY_SEVERITY`, `REPETITION_INCREMENT_HOURS` (`extension.py`) | developer | undecided, plausibly **user** | code | Same category as `DEFAULT_BASE_DURATION_HOURS` -- how many hours a given severity/repetition adds is directly experienced, not internal. |
| `MINIMUM_RETAINED_FRACTION` (`extension.py`) | developer | undecided -- genuinely ambiguous between **user** and **system-safety-policy** | code | EXT-5's floor exists to keep a MAJOR/CRITICAL Incident's consequence from being erased by cooperation/context -- arguably a safety guarantee the *system* should own, not a preference an individual should be able to weaken. Marked undecided rather than pre-assigned for this reason. |
| `_SELF_DISCLOSED_MITIGATION`, `_ACTIVE_COOPERATION_MITIGATION`, `_RECOVERY_TASK_MITIGATION` (`extension.py`) | developer | undecided, same ambiguity as `MINIMUM_RETAINED_FRACTION` | code | These three numbers set how much cooperation is "worth" against that same floor -- inseparable from its governance question. |

**Not tagged, and why:** `MAX_TARGET_ACTIVE_HOURS` (336, `window.py`) is
given explicitly by the architecture document (I5) -- architecture-owned,
not a bootstrap default. `_is_high_cooperation()`'s "requires BOTH
factors" rule is the same kind of undecided-ownership choice as the
constants above, but has no single numeric constant to attach a tag
to -- noted in its own comment in prose instead.

## Design notes

- **Four TBD parameter groups from `extension_technical_design.md`
  Section 10**, this slice's own defaults, flagged (not silently
  presented as architecture-decided) -- see the governance table above
  for who might eventually own each one:
  - `BASE_HOURS_BY_SEVERITY` — MINOR=4.0, MODERATE=12.0, MAJOR=24.0,
    CRITICAL=48.0h.
  - `REPETITION_INCREMENT_HOURS` — 6.0h per additional same-rule
    repetition within the current window.
  - `MINIMUM_RETAINED_FRACTION` for MAJOR/CRITICAL — 0.5/0.7, adopting
    the architecture document's own illustrative comment values (they
    were offered as reasonable examples, not arbitrary placeholders) as
    this slice's actual defaults.
  - `_is_high_cooperation()`/`_mitigation_fraction()` — "high
    cooperation" requires BOTH `self_disclosed` AND
    `active_cooperation_in_resolution`; mitigation is graduated
    (0.3 + 0.3 + 0.2 for recovery-task context, capped at 1.0 before the
    MAJOR/CRITICAL floor is applied).
- **`ConfirmedIncidentSummary` (Trust Manager) gained `rule_group_id`**
  — EXT-2's current-window-scoped repetition count needs it locally,
  without a cross-module read mid-transaction (see below).
- **`incident_consumption` gained a `rule_group_id` column** (migration
  007, additive) — the same reasoning: EXT-2's count is computed from
  this module's own table, never by asking Trust Manager.
- **`incident.confirmation_changed`'s payload gained `intrinsic_severity`,
  `cooperation_self_disclosed`, `cooperation_active_cooperation_in_resolution`**
  (only populated when the transition reaches CONFIRMED) — the
  event-driven consumer handler (`system/startup.py`) needs
  `ExtensionContext`'s two Trust-Manager-owned fields without calling
  back into Trust Manager's API mid-transaction. This is the same
  NestedTransactionError lesson `system/README.md` documents for
  `trust_domain`, now extended to Extension's needs.
- **`remaining_active_hour_capacity` is computed as
  `MAX_TARGET_ACTIVE_HOURS - (base_duration_hours + extensions_hours)`**
  — the room left before the absolute 336-hour ceiling, NOT related to
  `active_hours_elapsed()`/the countdown at all. Extension's capacity
  cap (Section 3.4) is a structural constraint on how large
  `target_active_hours` may grow, independent of how much time remains
  on the current countdown.
- **A real finding from writing the integration tests**: with default
  (unspecified) cooperation, `confirm_incident()`'s
  `CooperationAssessment()` default is LOW (`self_disclosed=False,
  active_cooperation_in_resolution=False`) — meaning an isolated MINOR
  Incident is, by default, *eligible* for Extension
  (`ELIGIBLE_BY_LOW_COOPERATION`). Tests that want to exercise "clean"
  state-machine behavior without incidental extension now pass an
  explicit HIGH-cooperation `CooperationAssessment` — this is a real
  behavior, not a test artifact, and is exactly what `philosophy.md`
  2.1/3.8 would predict: cooperation must be actively demonstrated to
  earn leniency, not assumed by default.

## Two real bugs found and fixed (Phase 2.7 focused review)

- **`resume(reason)` used to close only the most recently opened open
  `FreezePeriod` for that reason.** `emergency_override`/
  `temporary_wear_exemption` have no schema-level uniqueness constraint
  preventing a second concurrent open (unlike
  `partnered_intimacy_authorization`'s
  `idx_freeze_periods_one_open_intimacy_auth`), so a double-submitted
  `emergency_freeze()` (double-tap, or a retry after a timeout) could
  leave an orphaned second open row that a single `resume()` call
  silently failed to close — the window would stay `FROZEN` with no
  visible reason why. Fixed: `resume()` now closes EVERY open
  `FreezePeriod` matching the given reason, emitting one
  `freeze_periods.closed` event per row closed (each now carries its
  own `freeze_period_id` in its payload). See
  `tests/penalty_engine/test_repository.py::TestResumeClosesAllMatchingOpenFreezes`.
- **`_record_recovery_credit_in_transaction()` had no pre-check before
  its INSERT**, unlike its structural sibling
  `_consume_confirmed_incident_in_transaction()` — it relied solely on
  `UNIQUE(completion_id)` (I26), meaning a second DIRECT call (not the
  event-driven path, already protected by `consume_event()`'s own
  dedup) crashed with a raw `sqlite3.IntegrityError` instead of behaving
  gracefully. Fixed: a duplicate call now returns the previously
  recorded `RecoveryCreditDecision` — a deliberately different choice
  from the Incident-consumption analog's `None`, since a credit
  decision is naturally a look-up-able record, not just a
  did-something-change flag. See
  `tests/penalty_engine/test_recovery_credit.py::TestI26Dedup`.



## v0.39: dynamic window duration and mode caps (migration 031)
- `policy.py` / `duration.py`: a NEW window starts with `PW_Days = MaxDays - Trust * (MaxDays - MinDays)` hours, from the incident
  severity (minor / moderate / major+critical = severe), the Trust score of the incident's domain at consumption time (it already
  includes the incident's own drop) and the operating mode. Ranges (days) Standard / Advanced: minor 1-3 / 2-5, moderate 3-5 /
  5-10, severe 5-7 / 8-15. A new window never starts above 168 h (Standard) / 360 h (Advanced).
- **Hard cap per window:** `penalty_windows.max_target_hours` (Standard 336 h, Advanced 720 h) is fixed when the window starts;
  `target_active_hours()` and the extension capacity use it. The reserved half is only reachable through extensions of
  later violations inside the active window.
- **First minor offense = warning:** consumed into `incident_warnings` (event `penalty_warning.recorded`), no window; the Trust
  drop already came with the incident evidence. A rule group counts earlier consumptions and warnings as prior offenses.
- Without a policy (`PenaltyEngine(...)` built directly, as in older tests) the legacy 24 h base and the 336 h cap apply. The
  runtime builds the engine through `system/penalty_policy.make_penalty_engine`.
- `violations_catalog.py`: the catalog with default Trust drops (minor 0.02, moderate 0.05, severe 0.10) for entries without a
  specific number. It is declarative; goal failures are not violations.
