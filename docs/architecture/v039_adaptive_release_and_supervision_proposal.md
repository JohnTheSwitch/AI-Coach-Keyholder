# v0.39 — Token-Based Single-Barrier, Adaptive Release and Proactive Supervision: Design Proposal (v0.1)

> **Status: FINAL (v1.0).** The owner's binding resolutions are in **section 11** and
> take precedence over sections 0–10 wherever they differ (11.2 lists each
> difference). Phase 1 (domain core: migrations 027–028, windows, release engine,
> supervision state) is implemented; the rest is listed in 11.4 as later phases.
> Facts about Chaster were checked against its raw OpenAPI (v0.35.0); **VERIFY** marks
> what still needs a test lock.

## 0. Summary of the proposal and the five things that need your attention

The five pillars map onto four small, separately switchable units, built on the
Phase 1–3 machinery (Trust Manager, Rules Engine, execution plane, outbox, periodic
cycle):

| Unit | Package (new) | Nature |
|---|---|---|
| Time windows | `schedule_windows/` | pure functions, no I/O |
| Barrier + adaptive release | `release_engine/` | deterministic, secret target stored in the backend |
| Supervision + check-ins | `supervision/` | state machine + proactive DM sender |
| Guardrails | `conversation_engine/guardrails.py` | prompt text + structural non-disclosure |

Things I must flag before you read the details:

1. **Chaster's freeze endpoint cannot carry a reason text.** `SetFreezeDto` has
   exactly one field, `isFrozen`. "Requirements not met" cannot be attached to the
   freeze via the Public API (section 1.3).
2. **Freezing does not by itself make release safe or simple.** A frozen lock has
   no countdown; "release" means unfreezing a lock whose timer has already run out
   (section 1.2). That assumption must be proven on a test lock first (**VERIFY**).
3. **A permanent barrier needs a defined maximum.** If the bot, the server or the
   Chaster link is down, a fail-closed barrier means the wearer stays frozen
   indefinitely. I propose a hard maximum hold and rely on Chaster's own
   `maxLimitDate` backstop (section 2.4).
4. **Phase 2/3 limits would starve barrier management.** The approved limits
   (2 actions / 24 h, 4 h cooldown) count freeze/unfreeze; barrier actions need
   their own limit class (section 1.4). It also reverses the Phase 3 decision that
   freeze/unfreeze stay disabled for execution — that needs your explicit approval.
5. **Pillar 4 (a missed check-in "causes penalty decisions via the Rules Engine")
   conflicts with an approved decision.** Incident-driven penalties derive from the
   Penalty Engine so one event is never punished twice (section 5.4 offers two
   options).

---

## 1. Chaster single-barrier control (pillar 1)

### 1.1 Concept
Instead of adding and removing hours, the lock carries **one** barrier: the frozen
state. While the barrier is up the wearer cannot end the session. It comes down
only when the system decides that *all* of the following hold:

```
requirements met  AND  discretionary window active  AND  target release time reached
                  AND  safety gates clear  AND  fresh lock observation says it is safe
```

Time adjustments (Phase 2/3 `add_time` / `remove_time`) stay available for penalties
and rewards; they are no longer the unlock mechanism.

### 1.2 How Chaster actually behaves (what is verified, what is not)
Verified in the raw OpenAPI:
- `POST /locks/{lockId}/freeze`, body `{"isFrozen": boolean}`, scope `locks`,
  204 / 400 ("Cannot change a non-locked lock") / 401 / 403
  ("Missing the lock.freeze or lock.unfreeze permission") / 404.
  "Keyholders can use this endpoint to freeze wearer's locks."
- `LockForWearer` / `LockForKeyholder` expose `isFrozen`, `endDate`, `status`,
  `keyholder`, `frozenAt` (the latter gives us a provider-reported freeze time).
- `POST /locks/{lockId}/unlock` exists ("Unlocks a lock. For wearers, the lock must
  respect certain constraints"; 400 "Some extensions prevent the unlocking"); its
  permission model for a keyholder is not stated. **This design does not use it** —
  the wearer ends their own session; the system only lowers the barrier.
- Chaster's docs state a frozen lock has no countdown ("freeze ... preventing the
  wearer from making changes or ending the session").

**VERIFY on a test lock before relying on any of this:**
1. With the timer already at/after its end date and the lock frozen, the wearer
   cannot unlock; after `isFrozen:false` they can.
2. What `endDate` does during and after a freeze (it may shift by the frozen duration).
3. What a lock with no timer limit does while frozen.

Consequence for the barrier model: the **timer must already be exhausted** (or
removed) when the barrier is the only thing holding the lock, otherwise unfreezing
resumes a countdown instead of releasing. How the timer is brought to that state
(a one-time `remove_time`, a lock created without a timer limit, or `min/max limit
dates`) is decision D2.

### 1.3 "Requirements not met" reason text
Not possible through the freeze endpoint (single field). Options:
- (a) Show the reason only in Discord (our own message), not on Chaster. **Recommended.**
- (b) A keyholder note (`PUT /keyholder/notes/lock/{lockId}`, scope `keyholder`):
  private to the keyholder, not visible to the wearer — does not meet the intent.
- (c) The Extensions API logs "<extension> froze the lock" in the lock history, but
  that is Route C (`developer` scope), which was not chosen.

### 1.4 Barrier as decisions, through the existing pipeline
New `ActionType` values are not needed: `FREEZE` and `UNFREEZE` already exist in the
Phase 2 enum and in the executor's state machine; v0.39 *enables* them for execution
(Phase 3 had them disabled by decision) via `ENABLE_BARRIER_EXECUTION` (new, default
false, separate from time actions).

- **Raise/keep the barrier** (`FREEZE`): when the lock is observed unfrozen while
  requirements are unmet and no release is authorised (drift, e.g. the barrier was
  lifted on Chaster directly, or a new lock begins). Idempotent: never issued when
  the fresh observation already shows `isFrozen: true`.
- **Lower the barrier** (`UNFREEZE`): produced only by the release engine
  (section 3) when the target time is reached and every gate is clear.
- All executions use the existing at-most-once claim, audit log, runtime halt and
  15-minute freshness rules. `ChasterHttpTransport` gains `set_frozen(lock_id,
  frozen: bool)` (body `{"isFrozen": bool}`; this body *is* documented). The
  transport stays free of any combination / recovery / unlock method.
- **Separate limit class:** barrier actions are exempt from the 2-per-24h and 4-hour
  cooldown limits (which were designed for time changes) but have their own:
  at most 1 `UNFREEZE` per lock per 24 h, at most 3 `FREEZE` per 24 h, and a 10-minute
  cooldown after any barrier action. An `UNFREEZE` never counts toward the time cap.

### 1.5 Relationship to the emergency path
The emergency-unlock plane stays isolated and **independent of every window and
gate in this document**. `application/service.py` still never mentions it; the new
packages must not import `chaster.emergency_unlock*` (AST guard, as for
`chaster_execution`). The guardrail's safety exception (pain, injury, medical,
genuine emergency) is unchanged and takes precedence over suspense wording.

---

## 2. Time windows and safety gates (pillar 2)

### 2.1 Configuration
Existing: `QUIET_HOURS_START=22:00`, `QUIET_HOURS_END=07:00` (currently "for a future
scheduler"). New (all with defaults, all read from `.env`):

| Setting | Default | Meaning |
|---|---|---|
| `APP_TIMEZONE` | `Europe/Prague` | IANA zone used for every local-time rule. **Required**: the clock is UTC-only and the existing quiet-hours strings carry no zone. |
| `WORKDAY_START` / `WORKDAY_END` | `07:00` / `16:30` | local work hours |
| `HOLIDAY_COUNTRY` | `CZ` | public-holiday calendar |

### 2.2 Windows (all in `APP_TIMEZONE`)
- **QUIET** — `22:00 → 07:00` crossing midnight, every day. No unlock, no proactive
  messages, no check-ins. Wins over everything else.
- **WORK** — Monday–Friday, `WORKDAY_START → WORKDAY_END`, **excluding Czech public
  holidays**. No unlock. (Messages are allowed unless QUIET; D6 asks whether
  check-ins should avoid WORK.)
- **DISCRETIONARY** — the remainder, i.e.
  - workday evenings: `WORKDAY_END → QUIET_START` (16:30–22:00),
  - weekends and public holidays: `QUIET_END → QUIET_START` (07:00–22:00).
  Precedence: QUIET > WORK > DISCRETIONARY.
- A public holiday on a weekday is treated as a weekend day (whole day
  discretionary outside QUIET).

### 2.3 Implementation notes
- `schedule_windows/windows.py`: pure `classify(instant_utc, config) -> WindowKind`,
  `next_discretionary_intervals(from_utc, days) -> list[Interval]`. All arithmetic via
  `zoneinfo`; no `datetime.now()` (instant is an argument; the Clock abstraction
  supplies it).
- **Dependencies (need approval, D5):** `holidays` (Czech calendar incl. Easter
  Monday / Good Friday, 8 May, etc.) and `tzdata` (Windows has no system tz
  database). Both pinned in `requirements.txt`. Failure to load the holiday calendar
  fails closed: the day is treated as a workday for *unlock* purposes (stricter) and
  nothing is released.
- **DST:** local boundaries are converted to UTC per date, handling the nonexistent
  hour (spring forward, 2026-03-29) and the repeated hour (fall back, 2026-10-25).
  Quiet hours must never shrink; tests cover both transitions and the midnight cross.
- `holidays` is a lookup, not a guarantee: a wrong/missing holiday only makes the
  system *more* conservative for unlocks (work window), never less.

### 2.4 Gates and the missing maximum
Absolute gates (always evaluated, no setting can bypass): window is DISCRETIONARY;
lock observation ≤ 15 min old, `status == locked`, keyholder lock, `isFrozen`
known; execution halt clear; no active penalty window; no supervision state (D3);
tokens requirement met.

**Maximum hold (decision D4).** As written, nothing guarantees release; a bot outage
or an engine that keeps declining would hold the wearer without bound. I propose:
(1) a configurable `MAX_BARRIER_HOLD_HOURS` after which the target release is forced
into the next discretionary window regardless of tokens (still window-gated), and
(2) configuring Chaster's own `maxLimitDate` on the lock ("after this date the wearer
can release themselves regardless of the timer or extension restrictions" — in the
spec) as the final backstop that does not depend on this software at all.

---

## 3. Adaptive discretionary release engine (pillar 3)

### 3.1 Tokens (Hidden Token Economy, as drafted — nothing exists in code yet)
`philosophy.md` §4.2 and `relationship_decision_engine_technical_design.md` §5.3
already define a **hidden** token mechanism owned by the Decision Engine (DEC-4/DEC-5:
never exposed). v0.39 implements its first slice:
- Append-only `token_ledger` (earn / spend / expire, amount, source entity, reason).
  Sources: goal accountability assessments (`progress`), task completions *via goals*
  (Phase 1 rule: tasks reach trust only through goals), answered check-ins (D7).
- `required_tokens` per release cycle is system-configured data (like rulesets: file
  or migration config, validated at load, no LLM mutation, no runtime change without
  consent + audit).
- "Requirements met" = tokens ≥ requirement for the current cycle **and** no veto
  condition (penalty window active, supervision active, last check-in missed).

### 3.2 No instant unlock
Reaching the requirement does not lower the barrier. It creates a **release plan**:
1. A trigger event (`tokens.requirement_met`) is recorded.
2. `release_engine` computes a **target release timestamp** inside a future
   DISCRETIONARY interval, within `[earliest_eligible, latest_allowed]`.
3. The plan is stored; a periodic cycle (the existing 60 s worker) re-checks it.
4. When `now ≥ target` and the gates (2.4) pass, an `UNFREEZE` decision is emitted
   and executed through the pipeline of 1.4. If a gate fails at that moment the plan
   is re-targeted to the next valid interval rather than executed.
5. New disqualifying events (missed check-in, setback, penalty window) cancel the
   plan; it is recomputed when the requirement is met again.

### 3.3 "Pattern-based" target
The target is chosen by a pure function `plan_release(history, windows, config, seed)`:
- **History** = the user's previous releases (local weekday/time-of-day), previous
  plan outcomes, and recent trust trend — read from `release_plans` (own table).
- **Pattern** = a deterministic weighting that avoids both predictability and
  repetition: slots from the valid discretionary intervals are scored (down-weight
  the times of the last N releases and of the same weekday, mild preference for
  earlier evenings after strong compliance, later after weak) and one is drawn with a
  **cryptographically random seed generated once and stored** with the plan. Storing
  the seed makes the plan reproducible for audit/replay while remaining unguessable.
- Constraints: never inside QUIET or WORK; at least a minimum delay after the
  requirement is met (D8); never later than the maximum hold (2.4).
- This is the Decision Engine's **Discretionary** class implemented deterministically
  today (no LLM participates, in line with §8 of that draft).

### 3.4 Secrecy of the target
The exact timestamp is stored in `release_plans.target_at` and is **never** exposed:
- Not in any prompt/context builder: `release_engine` is not importable from
  `conversation_engine`, `application`, `chaster/llm_*`, `chaster/status_context`
  or `chaster/presentation` (AST guard). The only data crossing into model context is
  the coarse enum `release_phase ∈ {requirements_unmet, requirements_met_pending,
  released}` — never a time, a window, a duration or a count.
- Not in `lock_decisions.inputs_json` / `reason`, event payloads, INFO-level logs,
  Discord embeds, `chaster status`, or error messages. A scrubbing test fails if a
  stored ISO timestamp from a plan appears in any user- or model-facing string.
- The owner can inspect plans through a **local script only** (like
  `scripts/execution_control.py`), never through Discord.
- Honesty (philosophy 2.6): non-disclosure is allowed; stating something false is
  not. Replies must not claim that no schedule exists, or invent one (section 6).

### 3.5 Events and tables
Events: `release_plan.created`, `.retargeted`, `.cancelled`, `.due`, `.executed`
(payloads carry ids only, no timestamps). Tables (migrations proposed, **not created**):
`token_ledger`, `release_plans` (target_at, seed, status, created/updated), plus
`window_state_log` only if audit of classification is wanted (optional).

---

## 4. Time handling of the periodic cycle
The existing `system/periodic.py` cycle (60 s, single-instance lease) gains two steps:
`release_engine.tick(now)` and `supervision.tick(now)`, each behind its own flag
(`ENABLE_RELEASE_ENGINE`, `ENABLE_SUPERVISION`, both default false). Each tick is
idempotent and clock-injected. Order: outbox → rules engine → release → supervision
→ executor. A failing step is logged and never aborts the cycle (existing pattern).

---

## 5. Proactive supervision and check-ins (pillar 4)

### 5.1 ActiveSupervisionState
Entered when a domain's trust **score < 0.40 with confidence ≥ 0.30** (low confidence
alone never triggers supervision, consistent with Trust Manager 3.6). **Exit with
hysteresis:** score ≥ 0.50 (confidence ≥ 0.30) for two consecutive evaluations, to
prevent flapping around the threshold. Persisted in `supervision_states`
(domain, entered_at, entry_score, exited_at, exit_reason), driven by the
`trust_domain.recalculated` event. Events: `supervision.entered`, `supervision.exited`.
Thresholds are `BOOTSTRAP_DEFAULT` placeholders.

### 5.2 Proactive check-ins
New capability: **the bot initiates Discord DMs** (today it only replies). While
supervision is active, the periodic cycle schedules `check_in.prompt_due`:
- cadence/limits: configurable (proposal: at most 3 per day, minimum 3 h apart),
  never in QUIET; the exact time of each prompt is jittered inside allowed hours.
- **At-most-once sending**, same pattern as execution: claim the check-in row before
  sending; a crash ends as `needs_review`, never resent automatically.
- Delivery uses the user's known Discord id (from `user_accounts`; **VERIFY** the
  mapping exists for DMs). A failed delivery (DMs closed) is recorded
  `undeliverable` and is **never** treated as a missed check-in.
- Prompt wording is a deterministic template phrased by the persona layer, not
  free LLM generation.
- A reply from the user within the grace period (default 30 min, D7) marks it
  `answered`. The reply content is not judged; it is a presence check. The normal
  conversation handling still processes the message, including the guardrail's
  safety exception.

### 5.3 Missed check-ins
No answer within the grace period (and only for delivered prompts, outside QUIET,
not while a safety conversation is open) → `check_in.missed`.

### 5.4 Consequence of a miss — decision D9 (conflicts with an approved rule)
You asked for a Trust Score drop *and* penalty decisions via the Rules Engine. The
approved Phase 2 rule says incident-driven time derives from `ExtensionDecision`
so one event is not punished twice. Two compatible designs:
- **Option 1 (recommended):** a missed check-in is registered as an **Incident**
  (new rule group `check_in`, low severity) → existing pipeline: `TrustEvidence`,
  the Penalty Engine's `ExtensionDecision`, trust recalculation. The Rules Engine does
  nothing extra for it (no double penalty), but its non-incident rules still react to
  the resulting trust transitions.
- **Option 2:** a new evidence type `CHECK_IN_MISSED` (Trust Manager enum extension)
  + a Rules Engine rule on a new trigger event. Simpler to tune but needs care to
  avoid double effects if an incident is also filed.

Either way: the trust drop is capped by the existing 0.5 per-row and 0.15
per-recalculation caps, and every penalty goes through the existing hard limits.

### 5.5 Interaction with release
Active supervision and a missed check-in veto release (3.1); entering supervision
cancels any pending release plan (3.2 step 5).

---

## 6. Dominance and suspense guardrails (pillar 5)

### 6.1 Where it goes
Pillar text refers to `prompt_builder.py`, but `_SYSTEM_BOUNDARIES` there is
byte-identical by project rule (its md5 must never change). The guardrails live in
`conversation_engine/guardrails.py`; new text goes there as an additional numbered
rule in `CONVERSATION_GUARDRAILS` (rule 6), keeping the "last before the user turn"
position that works for the 8B model.

### 6.2 Two layers, because prompt rules are weak against a small model
1. **Structural (primary):** the model never receives a release time, window, token
   count or requirement size (3.4). It cannot leak what it does not have.
2. **Prompt rule (secondary):** draft wording —
   > 6. RELEASE AND SUSPENSE: Never state, estimate, hint at or confirm when the user
   > will be released: no times, dates, durations, hours, windows or "soon/later today".
   > Never say how many requirements remain or what they are worth. You decide, and the
   > user does not get to know when. Stay in charge: calm, certain, a little teasing, never
   > cruel. You may say the day's requirements are met, but not that release is
   > coming or when. Example: "You've met your requirements today. Whether freedom is
   > granted, and when, depends on how you keep behaving. Be patient." Do not lie: if asked
   > directly whether a schedule exists, say that you keep that to yourself.
   > The safety exception in rule 3 always wins.
3. **Optional hardening (D10):** a deterministic outbound check that rejects a reply
   containing a clock time/date/duration pattern while `release_phase` is not
   `released`, and regenerates once or falls back to a canned line. This is the
   "post-generation check" previously discussed and never approved.

### 6.3 Honesty boundary
Philosophy 2.6 requires truthful *reasons* but not disclosure of computation
(2.22). The wording above hides *when* but does not deny that a decision exists.
"Whether … freedom is granted" is only true if release is genuinely conditional;
that depends on D4 (a guaranteed maximum would make "whether" untrue, and the line
would need to become "when"). Tests will pin the exact permitted/forbidden phrases.

### 6.4 Tests
Guardrail text present and last-before-user; `_SYSTEM_BOUNDARIES` md5 unchanged;
context builders never include the forbidden fields (property test over all context
providers); the `release_phase` enum is the only release datum in any model-bound
payload; leak scan (section 3.4).

---

## 7. Data model (proposed migrations, not created)
- 027: `token_ledger`, `release_plans`.
- 028: `supervision_states`, `check_ins` (due_at, grace_until, status
  `scheduled|sent|answered|missed|undeliverable|needs_review`, claim columns).
- 029 (if D9 option 2): trust evidence type + rules trigger; otherwise none.
Configuration only (no migration): windows, holidays, thresholds, requirement.

## 8. Rollout (each stage independently switchable, dry-run first)
1. **Windows** (pure) + config + tests. No behaviour change.
2. **Barrier observation**: classify and *log* what the barrier engine would do.
   Test-lock verification of the Chaster freeze/timer behaviour (1.2 VERIFY items).
3. **Supervision** in "log only" mode (no DMs), then DMs behind `ENABLE_SUPERVISION`.
4. **Release engine** in dry-run: plans computed and stored, nothing executed.
5. **Barrier execution** on a test lock: raise → hold → release, with the runtime halt.
6. Production use of the rules after a monitored period (tuning the placeholders).

## 9. Test plan summary
Pure window tests (all four kinds, boundaries to the minute, midnight crossing, DST
days, Czech holidays incl. movable ones, weekday holidays, weekends); release planner
property tests (target always inside DISCRETIONARY, never QUIET/WORK, never past the
maximum, reproducible from the stored seed, different seeds differ); barrier action
tests through the fake transport (idempotent freeze, unfreeze only after gates, no
execution while halted or stale); supervision hysteresis; check-in at-most-once
sending, undeliverable ≠ missed, QUIET never messaged; leak and AST guards; the
existing guards (no "emergency" in `application/service.py`, `_SYSTEM_BOUNDARIES`
md5, no `datetime.now()`, no TLS weakening) stay green.

## 10. Decisions needed from you
- **D1 — Freeze reason text:** accept Discord-only "Requirements not met" (1.3 a)?
- **D2 — Barrier timer model:** how is the lock's timer exhausted so unfreezing
  releases rather than resumes a countdown (one-time `remove_time`, no timer limit,
  or other)? Also OK to run the 1.2 verification on a test lock first?
- **D3 — Release vetoes:** confirm penalty window, supervision and a missed check-in
  all block release.
- **D4 — Maximum hold:** is there a guaranteed latest release (and how long), and may
  I rely on Chaster `maxLimitDate` as the independent backstop? This also decides
  whether "whether freedom is granted" may be said truthfully.
- **D5 — New dependencies:** approve `holidays` and `tzdata` (pinned), and
  `APP_TIMEZONE=Europe/Prague`.
- **D6 — Windows details:** should check-ins avoid WORK hours? Is a weekday holiday
  fully discretionary outside QUIET? Confirm 16:30 and 07:00 defaults.
- **D7 — Check-ins:** frequency limits, grace period (30 min?), whether answering
  earns tokens, and confirmation that undeliverable prompts are never penalised.
- **D8 — Release timing:** minimum delay after requirements are met, and how
  strongly the pattern should favour earlier vs later evenings.
- **D9 — Missed check-in consequence:** Option 1 (incident path, recommended) or 2.
- **D10 — Outbound reply check:** approve the deterministic post-generation filter
  for time/date patterns, or rely on structural + prompt layers only?
- **D11 — Enabling barrier actions:** approve lifting the Phase 3 "freeze/unfreeze
  stay disabled" decision for the barrier engine only, with the separate limit class
  of 1.4, behind `ENABLE_BARRIER_EXECUTION`.

---

## 11. Final resolutions (binding) and implementation status

### 11.1 Resolutions
(The owner directive reused the numbers D3–D11 for a different set of topics than
section 10; resolutions are recorded here by subject.)

| Subject | Resolution |
|---|---|
| Freeze reason text | Chaster's freeze takes only `isFrozen`; "Requirements not met" is shown **only in Discord**. |
| Timer & barrier | On the test lock, the timer is allowed to run to 00:00 with `isFrozen: true` as the sole barrier. **Not yet verified** (needs a live test lock). |
| Maximum hold | `MAX_BARRIER_HOLD_HOURS` is a code backstop alongside Chaster's `maxLimitDate`. Value 72 is a placeholder. |
| Suspense wording | May say: "Requirements are met, but release timing rests with keyholder discretion within safety limits." This is truthful because a backstop exists. |
| Freeze/unfreeze | Phase 3 ban lifted **only** for the Barrier Engine in `chaster_execution`, with a **separate limit pool: max 2 barrier actions / 24 h and a 4 h cooldown**, independent of `add_time`/`remove_time`. |
| Guardrails | Go in `conversation_engine/guardrails.py`; `_SYSTEM_BOUNDARIES` stays byte-identical. The model receives only coarse enums (`requirements_met_pending`, `requirements_not_met`), never a target time or offset. |
| Dependencies | `holidays` and `tzdata` added; `APP_TIMEZONE=Europe/Prague`. |
| Token spend | Exactly **1 token** per Single Barrier release, spent inside `commit_authorization()` when all requirements are met and the discretionary window is active. Ledger append-only, balance = `SUM(delta)`, balance ≥ 1 required, no debt. Daily tasks do not bypass tokens or windows. |
| Provider states | `AUTHORIZED → PROVIDER_REQUESTED → PROVIDER_CONFIRMED | PROVIDER_FAILED | PHYSICAL_STATE_UNKNOWN`. |
| Missed check-in | `check_in.missed` feeds Trust as evidence `CHECKIN_MISSED`; if flagged as a confirmed violation or repeated non-compliance it is registered as a low-severity Incident via the Penalty Engine (no double punishment). |
| Hygiene grace | `HYGIENE_GRACE_PERIOD_MINUTES=2`; an overrun emits `hygiene_session.exceeded` → Trust evidence drop; no automatic block on later sessions. |
| Release verification | Freeze/unfreeze behaviour is verified on a designated test lock before live execution. |
| Scheduler | Target = `WORKDAY_END + offset`, offset `UNLOCK_WINDOW_MIN_OFFSET_MINUTES=30` … `UNLOCK_WINDOW_MAX_OFFSET_MINUTES=210`; target and seed persisted; eligibility and authorization re-validated at target time. |
| Supervision | Active when Trust `< 0.40` (confidence `≥ 0.30`); inactive after `≥ 0.50` for 2 consecutive evaluations. Levels: 4 `0.85–1.00` 0 check-ins/day; 3 `0.70–0.84` 1 (evening); 2 `0.45–0.69` 2 (afternoon, evening); 1 `0.00–0.44` 3. Check-ins only outside QUIET and WORK. `CHECKIN_RESPONSE_TIMEOUT_MINUTES=60`. |
| Check-in evaluation | Free text is evaluated by the LLM against a versioned rubric returning `SUFFICIENT | INSUFFICIENT | UNCERTAIN | NOT_EVALUATED`; `UNCERTAIN` never becomes a miss or a penalty. |
| Hygiene openings | Chaster "Hygiene opening" extension ("Allow only keyholder to open"); availability by **calendar days** in `APP_TIMEZONE`: Level 4 daily, 3 every 2 days, 2 every 3, 1 every 4. |
| Constitution | Emergency Override bypasses every window, token and rule and is un-penalised; provider observations never mutate user-reported state; unknown/stale means unknown, never "unlocked"; the model phrases pre-decided facts and never writes domain state. |

### 11.2 Differences from sections 1–10 (the resolutions win)
- Barrier action limits: 2 per 24 h + 4 h cooldown (was: 1 unfreeze, 3 freeze, 10 min).
- Tokens: exactly 1 per release (was: "tokens ≥ requirement").
- Release target: `WORKDAY_END + 30…210 min` (was: a history-weighted slot draw; history
  weighting is **not** implemented).
- Missed check-in: Trust evidence first, Incident only when flagged (was: Option 1 only).
- Supervision cadence: level-based (was: ≤ 3/day flat).

### 11.3 Issues found while implementing (need your confirmation)
1. **Levels vs supervision.** Supervision is active only below 0.40 (and until 0.50), yet
   Levels 2–4 prescribe check-ins at higher scores. Phase 1 builds the state and the pure
   level tables only. Do Levels 2–4 apply when *not* under supervision (e.g. one evening
   check-in at Level 3)? The Level 1 band (to 0.44) also overlaps the 0.40–0.44 entry gap.
2. **Weekend/holiday base time.** `WORKDAY_END` has no meaning on a non-working day; I used
   the same base (16:30 + 30…210 min = 17:00–20:00) on every day. Say if weekends should
   use a different base.
3. **Morning gap.** On working days the time between quiet end and `WORKDAY_START` is
   classified WORK (never discretionary) when the two differ.
4. **LLM-evaluated check-ins.** An LLM classification feeding Trust consequences sits next
   to invariant 4 (the model never alters Trust). It is acceptable only if the model's
   output is a bounded enum consumed by deterministic code, `UNCERTAIN`/`NOT_EVALUATED` have
   no consequence, and the rubric version is stored. What `INSUFFICIENT` costs is not
   specified (only silence is a miss); proposed: no penalty until you define one.
5. **`commit_authorization()`** exists only as a design in
   `activity_authorization_technical_design.md` (that module is unbuilt). The release
   engine implements the same pattern (re-check under the write lock, one SPEND per
   authorization, compensating REVERSAL) for the Single Barrier only.
6. **Startup order.** The directive's list names Activity Authorization and Hygiene
   Privilege, which are not built, so the real order is unchanged
   (Trust → Penalty → Goal → Recovery → outbox).
7. **Hygiene openings.** How the Chaster extension is triggered/observed through the API
   is not specified or verified; migration 029 was therefore **not** created. Its draft is
   in 11.5.
8. **Test-lock verification** (timer at 00:00 + frozen = held; unfreeze = releasable) has
   not been done; no live execution of barrier actions is possible before it.

### 11.4 Implementation status
- **Done (Phase 1):** migrations 027 (token ledger, release plans, authorizations) and
  028 (supervision states); `schedule_windows` (pure windows, holidays, DST); `release_engine`
  (ledger, scheduler, plan/commit/backstop/provider states); `supervision` (state machine,
  Trust levels) with a flag-gated consumer; dependencies; config; tests.
- **Next:** periodic release tick + `release_phase` context + guardrail text; barrier
  freeze/unfreeze in `chaster_execution` with the separate limit pool; token earning from
  goal events; `CHECKIN_MISSED` evidence; check-in delivery, deadline and rubric evaluation;
  hygiene (after its Chaster integration is specified).

### 11.5 Draft (NOT applied): migration 029
```sql
-- check_ins: id, domain_id, due_at, grace_until, status
--   (scheduled|sent|answered|missed|undeliverable|needs_review), claimed_at, sent_at,
--   answered_at, evaluation (SUFFICIENT|INSUFFICIENT|UNCERTAIN|NOT_EVALUATED), rubric_version
-- hygiene_sessions: id, opened_at, due_back_at, relocked_at, grace_minutes, local_date (APP_TIMEZONE)
-- Both append-mostly; transitions by compare-and-set; events carry ids only.
```

### 11.6 Phase 2 resolutions (binding) and status

| Topic | Resolution | Status |
|---|---|---|
| Levels vs supervision (11.3 #1) | Check-ins apply whenever Trust is below Level 4 (< 0.85); the < 0.40 threshold only activates strict Supervision Mode (Level 1 penalties, strict report demands). | Cadence implemented; "strict report demands" and "Level 1 penalties" are **not defined** and not built. |
| Weekend/holiday base (#2) | Base = 10:00 local (`WEEKEND_RELEASE_START`) + the 30-210 min offset. | Implemented and validated against quiet hours. |
| Insufficient check-in (#3) | `INSUFFICIENT` -> 30-minute follow-up; if still not sufficient -> `check_in.insufficient` Trust evidence (minor drop); `UNCERTAIN` zero consequence. | Implemented (-0.02 placeholder). |
| Hygiene migration (#4) | Deferred until the Chaster extension schema is verified on a test lock; hygiene logic stays dormant. | Deferred. Migration number 029 was used for check-ins (+ `frozenAt`), so hygiene becomes **030**. |
| Maximum hold (#5) | `MAX_BARRIER_HOLD_HOURS=72` code backstop. | Implemented. |
| Activity Authorization (#6) | Keep the `commit_authorization()` pattern inside the release engine. | Unchanged. |
| Test-lock verification (#7) | Done live before `ENABLE_CHASTER_EXECUTION=true`. | **Pending (owner).** |
| Barrier freeze/unfreeze | Enabled strictly for barrier management; separate pool of 2 / 24 h with a 4 h cooldown. | Implemented (rule id `barrier.release`, own limit pool). |
| Release tick | Periodic task behind `ENABLE_RELEASE_ENGINE`. | Implemented (`system/release_tick.py`). |
| Guardrails + context enums | `guardrails.py` rule 6; coarse enums to the prompt. | Implemented; `_SYSTEM_BOUNDARIES` md5 unchanged. |
| Check-in delivery + rubric flow | `supervision/checkins.py`, `supervision/checkin_evaluator.py`. | Implemented behind `ENABLE_CHECK_INS`. |

Findings made while implementing Phase 2 (need your confirmation):
1. **Tokens cannot be earned yet.** No amount per goal outcome was specified, so nothing awards tokens; the release engine
   works only for tokens added through the domain service. This is the missing link between goals and release.
2. **Barrier raising is not automated.** Nothing issues a `FREEZE`; the barrier is raised manually or on Chaster. An automatic
   re-assertion would need a rule that cannot re-freeze a lock right after a legitimate release.
3. **A token is never spent unless the release can happen:** planning-only mode when execution is off, nothing committed when
   the runtime halt is set, and a decision that can no longer execute is refunded.
4. **A reply counts only if it is free text:** commands and Chaster status questions are not forwarded to the check-in service,
   so using the bot normally cannot be misjudged as an insufficient answer.
5. **Check-in level and Trust consequences use one configured Trust domain** (`CHECK_IN_TRUST_DOMAIN`). Multi-domain
   policy is undefined.
6. **"Flagged as a confirmed violation or repeated non-compliance -> low-severity Incident" (D9) is not implemented:** the
   flagging rule is not specified. Only the Trust evidence path exists.
7. **The rubric is a classifier, not an authority:** a prompt-injection attempt can at worst obtain `SUFFICIENT`; nothing else
   the model says is used.

### 11.7 Phase 3 resolutions (binding) and implementation

| Topic | Resolution | Implementation |
|---|---|---|
| Token earning | A COMPLETED goal earns one token; missed / partially met earns 0. | Consumer `release_engine` on `goal.completed` (`system/startup.py`), idempotent per event id. |
| Re-freeze | `FREEZE` (`barrier.freeze`) when the discretionary window ends, the balance drops below 1 outside a session, or the daily session expires. | `ReleaseEnginePeriodicTask._maybe_refreeze()`; one decision per release; see below. |
| Strict Supervision Mode | Check-in deadline 60 -> 30 min, follow-up 30 -> 15 min when Trust < 0.40. | `CheckInService` (`is_strict`), `CHECKIN_*` env values. |
| Escalation | 2 consecutive misses, or 3 within 48 h -> low-severity Incident `CHECKIN_REPEATED_NONCOMPLIANCE`. | `supervision/escalation.py`, migration 030. |
| Check-in Trust domain | `CHECK_IN_TRUST_DOMAIN` defaults to `routine`. | `core/config.py`, `.env.example`. |
| Test-lock verification | Manual, before `ENABLE_CHASTER_EXECUTION=true`. | Still pending (owner). |

Interpretations made (please confirm):
1. **Balance < 1 right after a release.** Spending the token for a release always leaves the balance at 0, so a literal
   "re-freeze when the balance is below 1" would undo the release at once. The re-freeze therefore waits for the end of the
   *release session* (the discretionary window in which the provider confirmed the release). Outside a session an unfrozen lock
   without a token is re-frozen; "window ended", "session expired" and "no token" all resolve to the same moment in practice.
2. **Only a barrier this system lowered is re-raised.** Without a confirmed release of the engine, an unfrozen lock is left alone,
   so enabling the release engine can never freeze a lock that was never managed as a barrier.
3. **Re-freeze and the barrier pool.** The pool (2 per 24 h, 4 h cooldown) limits releases. A re-freeze is exempt and does not
   count, otherwise a release at 20:30 could not be re-frozen at 22:00 (cooldown) and a release plus its re-freeze would block the
   next day's release.
4. **One re-freeze decision per release**, retried (max 3, 30-minute back-off) after blocked / failed / expired, never after
   `needs_review`. A barrier lowered again by hand later is not re-raised automatically.
5. **Env names.** The strict value you named (`CHECKIN_RESPONSE_TIMEOUT_MINUTES=30`) is implemented as the strict setting
   `CHECKIN_STRICT_RESPONSE_TIMEOUT_MINUTES=30`; `CHECKIN_RESPONSE_TIMEOUT_MINUTES` stays the normal 60.
6. **Escalation evidence.** The Incident is filed as low impact / partial breach / unclear intent (severity `minor`) and confirmed as
   `system_verified`. It also lowers Trust through the normal incident path, in addition to the per-check-in evidence
   (-0.06 per missed check-in).
7. **Retroactivity.** Goals completed while the release engine is off are not paid back when it is switched on.

### 11.8 Behavioural refinements (owner directive) -- specification, pure modules, and conflicts

**Implemented as pure, UNWIRED modules (no behaviour of the running system changed):**
`release_engine/token_pool.py` (Token Progress Pool), `trust_manager/clean_streak.py` (Clean Day recovery),
`penalty_engine/duration.py` (dynamic window duration, headroom cap, severity ranges), `penalty_engine/violations_catalog.py`
(catalog by domain). Tests: `tests/system/test_behavior_model.py`.

**Specified values.**
- *Token Progress Pool.* `P_daily = (0.75 * completed/total + 0.25 * trust) * 0.286`, evaluated at local midnight; at `P >= 1.0` earn
  one token (`token_pool.earned`) and subtract 1.0. Decimal arithmetic. A perfect week yields exactly 2 tokens.
- *Clean Day.* No violation, every assigned goal met, every check-in on time and sufficient. Streak recovery +0.008 (days 1-3),
  +0.015 (4-7), +0.025 (8+), Trust capped at 1.00; any violation / failed goal / missed check-in resets the streak.
- *Penalty duration.* `PW_Days = MaxDays - Trust * (MaxDays - MinDays)` in whole hours. Ranges (days): minor 1-3 / 2-5, moderate
  3-5 / 5-10, severe 5-7 / 8-15 (Standard / Advanced). First minor offense = warning, 0 hours. A new window never exceeds 168 h
  (Standard) / 360 h (Advanced); the rest of the hard cap (336 h / 720 h) is reserved for extensions.
- *Catalog.* 13 violation codes in three domains with severity, first-offense Trust drop and side effects. Only the three check-in
  codes can be detected today; hygiene / status-report / settings-tamper / misrepresentation codes are `detectable=False`
  because the modules that would observe them do not exist.

**Conflicts with already-approved and committed behaviour (NOT applied; decision needed before wiring).**
1. *Token earning.* Phase 3 resolution: a completed goal earns +1 token immediately (`goal.completed`, committed in c8a4597). The
   pool replaces that. Both cannot run: choose, then the `goal.completed` consumer is removed.
2. *Barrier mechanics.* Section 2 says not to enforce `isFrozen` loops and to keep freeze endpoints deferred, but the committed
   release engine lowers and re-raises the barrier through freeze / unfreeze (`barrier.release`, `barrier.freeze`), and the
   documented freeze endpoint is the only Chaster mechanism used. The alternative ("lock preventers" / a Single Barrier that is
   not `isFrozen`) names no API we can call. Which mechanism do you want? Also: an archived lock cannot be re-frozen; the
   engine already refuses to act unless the lock is `locked` with a keyholder, and a release that unlocks fully ends the lock.
3. *Check-in penalties.* Committed (Phase 2/3): missed -0.06, insufficient -0.02, incident after 2 consecutive (or 3 in 48 h).
   Directive: 1st -0.02, 2nd -0.04 plus strict mode, 3rd -0.06 plus incident, insufficient -0.01. Different numbers AND a different
   escalation point (the committed rule files the incident at the second miss). The 2nd-miss "strict Supervision Mode" also
   conflicts with strict mode being derived from Trust < 0.40 with hysteresis.
4. *Incident severity.* The committed escalation incident is low severity (`minor`); the catalog lists it MODERATE.
5. *Hard cap.* The Penalty Engine caps a window at 336 h for all modes; Advanced 720 h and the 168 h / 360 h initial caps would
   change `penalty_engine/window.py` (I5) and the Advanced Mode module.
6. *Goal failures and Trust.* Today a MISSED goal lowers Trust by 0.08 through the goal-assessment path and never opens an
   Incident; this agrees with the directive (goal failures drive only Trust and the pool).
7. *Emergency Override.* `philosophy.md` is respected; nothing here touches it, and `application/service.py` still contains no
   such text.

**Open questions (the directive does not define them).**
- A day with no assigned goals: the goal ratio is undefined (the pool function refuses `total = 0`). Skip the day, or count 100 %?
- Is a day with no goals and no check-ins due (Trust level 4) a Clean Day that earns recovery?
- `last_unlock_timestamp`: evaluation covers goals "since the last unlock"; is the pool otherwise independent of the window?
- Where the daily evaluation lives (a periodic job under the cycle lease, once per local midnight, idempotent per date) and how
  streak recovery is recorded (a new Trust evidence type, subject to the 0.5 per row / 0.15 per recalculation caps).
- Partnered Intimacy (2 tokens, debt down to -2, freezes the timer) has no module yet; the ledger currently forbids a negative
  balance.
- Hygiene penalties/lockout and the "complete abandonment of supervision" status need the deferred hygiene module (migration 031).
- Trust drops for moderate/severe catalog entries are unspecified (stored as 0.00).
