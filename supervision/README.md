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
- Open question recorded in the proposal: supervision becomes active only below 0.40,
  while Levels 2–4 describe check-in frequencies at higher scores; whether those
  apply outside supervision is undecided, so no scheduling is built yet.
- Isolation: imports nothing from `chaster*`, `conversation_engine`, `application`,
  `bot`, `discord` or `ai`.
