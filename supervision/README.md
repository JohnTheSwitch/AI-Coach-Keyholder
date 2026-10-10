# supervision — Proactive supervision state (v0.39 Phase 1)

Canonical: `docs/architecture/v039_adaptive_release_and_supervision_proposal.md`
(final, section 11). Phase 1 is the **state and the Trust levels only**; check-in
prompts, answers, evaluation and penalties are later phases.

- **State** (`repository.py`, migration 028 `supervision_states`): a domain enters
  supervision when its Trust score is `< 0.40` with confidence `≥ 0.30` (weak
  estimates have no authority) and leaves it after the score is `≥ 0.50` with
  confidence `≥ 0.30` for **two consecutive** evaluations (hysteresis; a dip or a
  low-confidence evaluation resets the streak). Driven by the `trust_domain.recalculated`
  event through a consumer registered only when `ENABLE_SUPERVISION=true` (default
  false); the handler uses just the event payload. Emits `supervision.entered|exited`
  (domain id only).
- **Trust levels** (`models.py`, pure): Level 4 `0.85–1.00` (0 check-ins/day,
  hygiene every calendar day), Level 3 `0.70–0.84` (1, every 2 days), Level 2
  `0.45–0.69` (2, every 3 days), Level 1 `0.00–0.44` (3, every 4 days). Hygiene
  availability is by **calendar day in `APP_TIMEZONE`**, not rolling hours.
- **Decision (owner):** check-ins apply whenever the Trust level is below Level 4 (score < 0.85); the < 0.40 threshold
  only switches on the strict *Supervision Mode*. Cadence is therefore level-based: Level 3 one per day, Level 2 two,
  Level 1 three.
- Isolation: imports nothing from `chaster*`, `conversation_engine`, `application`,
  `bot`, `discord` or `ai`.

## Phase 2: proactive check-ins (migration 029, `ENABLE_CHECK_INS`, default off)

`checkins.py` (service), `checkin_schedule.py` (pure slot placement), `checkin_evaluator.py` (rubric).
- **Scheduling:** each local day, `n` check-ins by level, placed only in the discretionary window (never quiet or work
  hours), one random instant per part of the window (a single check-in uses the later half); idempotent per
  (user, date, slot); unsent slots are skipped if the level improves.
- **Delivery (at-most-once):** claim (`sending`) before the send; `DiscordCheckInSender` in `bot/` hands the DM to the
  Discord loop. Undeliverable (DMs closed, not ready, no Discord id) and skipped (late, or the window closed) are never a
  miss; an ambiguous send or a crash is `needs_review`, never resent. One open check-in at a time.
- **Replies:** `ApplicationService` forwards a free-text message (not a command, not a Chaster status question) through an
  injected callable. A reply within the 60-minute deadline is stored locally (never in an event payload) and merged if
  several arrive.
- **Rubric (versioned, `RUBRIC_VERSION = "1"`):** the model only classifies: `SUFFICIENT | INSUFFICIENT | UNCERTAIN |
  NOT_EVALUATED`. Pain/injury/medical/emergency words (English and Czech) are `UNCERTAIN` without calling the model.
  `UNCERTAIN`, `NOT_EVALUATED`, malformed output or a model failure never carry a consequence.
- **Consequences:** silence past the deadline -> `missed` -> event `check_in.missed`; `INSUFFICIENT` -> a 30-minute
  follow-up window (a sufficient follow-up clears it); still not sufficient -> `check_in.insufficient`. The Trust Manager
  records `CHECKIN_MISSED` (-0.06) / `CHECKIN_INSUFFICIENT` (-0.02) evidence (placeholders, `BOOTSTRAP_DEFAULT`),
  idempotent, with an immediate recalculation. The last check-in being `missed` vetoes a release.
- Not built: filing a missed check-in as a low-severity Incident when "flagged" (the flagging rule is unspecified).
- Config: `CHECK_IN_TRUST_DOMAIN` (required; sets the level and receives the evidence), `CHECK_IN_USER_ID`
  (defaults to `CHASTER_EXECUTION_WEARER_USER_ID`).
