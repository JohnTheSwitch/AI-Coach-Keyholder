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

## Phase J3: the coach and check-ins

`CheckInService.prompt_suffix` (set by the bot when `ENABLE_COACH` is on) adds today's commitment and an invitation to a scheduled question. It is plain code, so
the periodic cycle gains no model call, and a failure leaves the plain question. The release check-in keeps the owner's wording. A reply to a check-in becomes a
coach turn with the negotiation instruction (`system/coach_dm.py`), and with `ENABLE_COACHING_MEMORY` on a filtered, dated copy goes to Coaching Memory. The check-in
user now falls back to `OWNER_USER_ID`, like the coach. `mentions_safety_issue` no longer matches "number" (`numb\w*` was too broad).

## Phase 2: proactive check-ins (migration 029, `ENABLE_CHECK_INS`, default on)

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
  records `CHECKIN_MISSED` / `CHECKIN_INSUFFICIENT` evidence (owner matrix: 1st miss -0.02, 2nd consecutive -0.04, 3rd and later
  -0.06, insufficient -0.01; the position is carried as `streak` in the `check_in.missed` event),
  idempotent, with an immediate recalculation. A missed check-in never vetoes a release.
- **Strict Supervision Mode (Trust < 0.40, owner rule):** the response deadline drops from 60 to 30 minutes and the
  follow-up window after an `INSUFFICIENT` answer from 30 to 15 minutes (`SupervisionService.is_active(domain)` is read when the
  prompt is sent / the verdict arrives; stored per check-in). Env: `CHECKIN_RESPONSE_TIMEOUT_MINUTES` (60),
  `CHECKIN_FOLLOW_UP_MINUTES` (30), `CHECKIN_STRICT_RESPONSE_TIMEOUT_MINUTES` (30), `CHECKIN_STRICT_FOLLOW_UP_MINUTES` (15).
- **Escalation (`escalation.py`, migration 030):** three CONSECUTIVE missed check-ins, or three missed within any rolling 48 hours,
  register one low-severity Incident (rule group `CHECKIN_REPEATED_NONCOMPLIANCE`: low impact, partial breach, severity `minor`)
  through the Trust Manager and confirm it as `system_verified`; the Penalty Engine then consumes it like any confirmed Incident.
  Only concluded check-ins count; skipped, undeliverable and needs_review prompts are ignored (neither a miss nor a streak
  breaker). Misses up to an escalation are covered and never counted again; the ledger is
  `UNIQUE` per triggering check-in and a crash between register and confirm is finished on the next pass.
- Config: `CHECK_IN_TRUST_DOMAIN` (default `routine`; sets the level and receives the evidence), `CHECK_IN_USER_ID`
  (defaults to `CHASTER_EXECUTION_WEARER_USER_ID`).

Phase J2: check-in escalation no longer confirms anything. Three missed check-ins lower Trust (per-miss evidence) and issue a Warning (event
`check_in.warning_issued`, DM, retried until delivered); the Incident stays UNCONFIRMED until the keyholder runs
`python -m cli check-in-escalations confirm|dismiss <incident id> --owner-id <id> --yes`.

Phase J2: `open_release_check_in()` opens the on-demand check-in of the Communication-First release (`RELEASE_SLOT`, once per local day). A reply
counts whatever its verdict, and left unanswered it expires as `skipped`, never `missed`. See `release_engine/README.md`.

## Photo check-ins (owner, 2026-11-13; migration 053, `photo_check_ins.py`)

Some check-ins ask for a verification picture in Chaster instead of a text answer: a text answer only shows the wearer was at their phone, a
picture with Chaster's code shows the lock is on.

- **Consent:** only after the wearer sends `photo check-ins on`; `photo check-ins off` stops them at any time and closes an open one as
  `skipped` (never a miss). Stored in `photo_check_in_consent`; events `supervision.photo_check_ins_consented` / `_revoked`.
- **How often:** per week by Trust level (`PHOTO_CHECK_INS_PER_WEEK`): level 4: 1, level 3: 2, level 2: 3, level 1: 4. Each local day is drawn
  once (`last_drawn_date`, so a restart never redraws), the week's remainder spread over its remaining days; a skipped one (PC off at its time)
  does not count and can come back later in the week. Level 4 gets photo check-ins although it gets no text ones.
- **When:** slot `PHOTO_SLOT` (50), inside the discretionary window, with the whole `PHOTO_RESPONSE_TIMEOUT` (2 hours) inside it too. A photo
  check-in neither waits for nor holds back a text one; a text reply never answers it.
- **The answer:** the bot reads only the submission times of the verification pictures on the connected account's locked locks
  (`PeerReviewReader.picture_times`, every 5 minutes while one is open). A picture between the prompt and the deadline (2 minutes of clock skew
  allowed) answers it, and the wearer gets "Got your verification picture". None by the deadline is `missed`, with the same `check_in.missed`
  event and consequence as a text check-in (Trust and warnings only). Pictures that cannot be read are never a miss: `skipped` after 2 more hours.
- The picture itself, its code and its review stay on Chaster; a rejection goes through the community review watch as before.

## Leaving Supervision Mode (owner, 2026-11-13; migration 056)

Live, routine Trust sat at 0.00, and leaving needed 0.50 twice: only clean days earned anything (+0.008 at first), so about 24 perfect days in a
row, and one miss started it over.

- **A good check-in earns Trust:** a scheduled or photo check-in answered on time and well enough emits `check_in.answered`, recorded as
  `checkin_answered` evidence of **+0.02** (as much as a first miss costs). The release check-in never earns; `answered_uncertain` neither.
- **Probation:** `PROBATION_CLEAN_DAYS` (7) clean days in a row, each with at least one answered check-in, end Supervision Mode whatever the score
  (`on_daily_evaluation`, from `daily_evaluation.completed`, which now carries `trust_domain`, `check_ins_due` and `check_ins_ok`). A day with
  nothing due or a vacation day neither counts nor breaks the run; any other day starts it over. Only the daily evaluation's domain (routine).
- **Entering needs a falling recalculation:** after a probation exit the score is still low, and a rising one must not put the wearer straight back.
- **Hygiene** (owner, 2026-11-13): hygiene Trust had no way up and hygiene supervision no way out. A planned opening relocked on time emits
  `hygiene_opening.relocked_on_time` (exemptions), recorded as `hygiene_on_time` evidence of **+0.02**. Supervision in a domain without daily
  evaluations ends after `PROBATION_QUIET_DAYS` (14) days without a confirmed incident there, with at least one such opening in that run
  (`probation_since`, `probation_proofs`; checked on each daily evaluation). A health opening or a late relock is no proof.
