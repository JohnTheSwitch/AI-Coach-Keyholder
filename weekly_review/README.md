# weekly_review

Roadmap slice 5 (owner, 2026-11-13). On **Sunday from 19:00 local** the wearer gets the week in plain facts, written by code from stored
facts: no model, no Trust scores, nothing that judges. It goes through the notification queue, so it never arrives in quiet hours.

What it says:

- **Commitments** (local Monday to Sunday): kept, partly, not kept, never answered, still open; how many were reported outside their time
  (late or too soon, so they did not count) and how many Kept reports were not shown in their spot check.
- **Spot checks** (when they run): passed, not passed, not shown ("I can't show it now" / "I didn't actually do it"), still open.
- **Check-ins and tokens** (Monday to Saturday, from the daily evaluations; Sunday is evaluated after midnight) and vacation days.

Rules:

- A Sunday inside a vacation gets no review; it is recorded as `vacation`, so it is not sent later that evening either.
- Each week is decided once (`weekly_reviews`). A Sunday the bot missed is not sent on Monday.
- `weekly review off` / `weekly review on` / `weekly review`. No settings row means on.
- The single wearer only, wired when the coach is active (`system/weekly_review_wiring.py`); tenants are not wired.

Tables (migration 065, written only by `service.py`): `weekly_review_settings`, `weekly_reviews`.
Events: `weekly_review.turned_on`, `weekly_review.turned_off`, `weekly_review.week_decided`.
Reads only through the owners' readers: `CoachDesk.get_commitments_between`, `ProofChecks.get_checks_between`,
`system.daily_evaluation.get_daily_evaluations`.
