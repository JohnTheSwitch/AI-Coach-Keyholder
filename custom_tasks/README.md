# custom_tasks

**Question it answers:** which erotic tasks may another wearer be given?

Owner, 2026-11-17: "ask them their limits and let me with you do them the tasks - it will be then send to them for approval". The built-in
catalogue in `obedience/service.py` is written around the owner's own limits and gear, so another wearer never gets it.

1. **Limits, in the wearer's DM:** `limits hard <text>`, `limits soft <text>`, `limits likes <text>`; `limits` shows them.
2. **The owner reads them:** `python -m cli tenants limits <tenant> --owner-id <you>` (limits, gear and every task with its status).
3. **The owner writes a task with Claude:** `python -m cli tenants task-propose <tenant> --text "..." --needs collar,blindfold
   --modes alone|discreet|both --owner-id <you>` (default: only when alone). The wearer gets a DM (through their notification queue).
4. **The wearer decides each one:** `tasks` lists them; `task approve <n>`, `task approve all`, `task reject <n>`. An approved task can be
   rejected at any time, at once (consent is withdrawn immediately).
5. **Their daily task** (`ObedienceTasks.custom`, set only for another wearer) comes only from their approved tasks that suit the mode
   (alone / someone's around) and their gear, not yesterday's, and not one from the last `REPEAT_DAYS` days while another is left. With
   none that fits: `CUSTOM_FALLBACK` ("Show your locked cage", what their spot checks already ask). `erotic tasks on` waits until at least
   one task is approved (`Features.unavailable`).

Not wired for the owner: his catalogue is his own, and a wearer can never write tasks into a database someone else keeps. Someone running the
bot on their own PC is that install's owner (the "they can do it themselves" case); using this module there for their own catalogue is not
built yet.

Tables `wearer_limits` and `custom_tasks` (migration 093), written only here. Events: `custom_tasks.limits_set`, `custom_tasks.proposed`,
`custom_tasks.decided`.

## Workouts and temptations (owner, 2026-11-17)

"Make everything including workouts and temptations available - of course after setting up."

- **Training plan:** `python -m cli tenants training-propose <tenant> --file plan.json --owner-id <you>`. The JSON fills the owner's six
  sessions (`legs_a`, `legs_a_short`, `legs_b`, `legs_b_short`, `upper`, `upper_short`) with the wearer's own exercises, start weights and
  a proof line; `training.plan.plan_from_json` checks it. The schedule stays the owner's (Saturday legs, Sunday upper, one optional short
  session on Monday or Thursday). The wearer reads it with `training plan` and answers `training plan approve` / `training plan reject`.
  A newer plan replaces the approved one only once approved; rejecting the approved plan stops the sessions at once. `workouts on` waits
  for an approved plan (`plan_not_yet`). Table `wearer_training_plans` (migration 094).
- **Temptations:** `python -m cli tenants task-propose <tenant> --temptation --text "..."`: a `custom_tasks` row with `kind = 'temptation'`,
  approved like a task, never drawn as a daily task. `temptations on` waits for one (`temptation_not_yet`); an offer uses only an approved
  one whose `needs` fit their gear, and the offered text is kept on the offer (`temptation_offers.text`).
- Not built: a different weekly schedule per wearer.

