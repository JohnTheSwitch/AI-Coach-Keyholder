# training

His training plan in the bot (owner, 2026-11-15: "I built it to help me workout"; work queue Q4c; migration 071). The plan is the one written
on 2026-11-14 in his workout chat (`plan.py`: exercises, sets, rep ranges, start weights, steps).

- **Schedule:** Saturday full leg day (A front of the thighs / B back of the thighs and glutes, alternating); Sunday full upper body in
  supersets; one short session on Monday or Thursday (legs in an odd ISO week, upper body in an even one). On Monday the bot asks which
  day; no answer means Thursday. No legs on Friday or Sunday.
- **An extra session on demand (owner, 2026-11-16):** `extra training` (also `extra workout`, `trénink navíc`) on a day without a
  session, before that day's task is out, makes today's task one more short session, at most one a week. It balances the week as in his
  workout chat's four-a-week version (upper body when the week's short session is legs, and the other way round), but never legs within
  2 days of another leg day, on a Friday or on a Sunday. Optional like every short session: verified it earns +0.08, skipped it costs
  nothing, and it is never part of the week bonus. Stored in `training_weeks.extra_date/extra_kind` (migration 089); event
  `training.extra_requested`.
- **The session is that day's obedience task** (it replaces the erotic one): the DM shows every exercise with sets, reps, his current
  weight and rest; proof is a Chaster photo of the day's training log with the code, due by the end of free time.
- **Full sessions are mandatory** (missed = asked to explain, unexplained = a Warning and 20 lines). **The short session is optional**:
  missing it is "skipped", no consequence.
- **Progression:** `log squat 105 4x9` or `log squat 105 10,10,9,10` (`log pullups 8,8,7` for body weight). Every set at the top of the
  range = +2.5 kg next time (one plate on the leg machines, 4 kg on the cable stack); body-weight work: "make it harder". The first two
  leg days run the return-from-break rule (squat 95 kg, the rest -10 %, one set fewer); every 7th week is a deload week (half the sets).

Tables (written only by `service.py`): `training_sessions`, `training_weights`, `training_logs`, `training_weeks`. Events:
`training.session_planned`, `training.short_day_chosen`, `training.sets_logged`. Wired through `system/obedience_wiring.py`; the Monday
buttons through `system/dm_actions.TrainingDm` (scope `training`); `log ...` is read in `ApplicationService` before any model.

**Training earns tokens (owner, 2026-11-15).** A session verified in Chaster adds +0.15 (full) or +0.08 (short) to the token pool on a day without a confirmed incident (`system/daily_evaluation.TRAINING_BONUS`, read through `obedience.service.get_verified_training`), and on a day with goals it counts as one more kept item. `TrainingPlan.is_training_day` tells the coach not to propose a separate workout commitment that day.

**Week and progression bonuses (owner, 2026-11-15).** Sunday: +0.10 when every session the plan gave that week (`get_planned_sessions`) was verified; one cancelled for health or an exemption, or skipped for lack of a free slot, is excused; at least two must count. Each logged top set that earned a weight increase (`get_raises`) adds +0.05, at most +0.10 a day, only on a day with a verified session, one per exercise and never at a lighter weight than an earlier log.

**A moved short session (owner, 2026-11-16; migration 090):** `training_weeks.moved_date/moved_kind` hold a short session moved to another
day (filed by Claude on his request; Monday 16 Nov's leg day to Tuesday). It has its own slot, so the week's `extra training` stays free,
and it stays one of the week's planned sessions for the week bonus.
