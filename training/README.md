# training

His training plan in the bot (owner, 2026-11-15: "I built it to help me workout"; work queue Q4c; migration 071). The plan is the one written
on 2026-11-14 in his workout chat (`plan.py`: exercises, sets, rep ranges, start weights, steps).

- **Schedule:** Saturday full leg day (A front of the thighs / B back of the thighs and glutes, alternating); Sunday full upper body in
  supersets; one short session on Monday or Thursday (legs in an odd ISO week, upper body in an even one). On Monday the bot asks which
  day; no answer means Thursday. No legs on Friday or Sunday.
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
