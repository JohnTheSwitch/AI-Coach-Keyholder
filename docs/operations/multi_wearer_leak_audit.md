# Multi-wearer leak audit (2026-11-16)

Owner, 2026-11-16: before another wearer joins, nothing built around the owner may reach them. This lists what is hard-coded to the owner
and what happens to it for an additional wearer. Checked: every non-test `.py` file for the owner's gear brands, training equipment and
schedule, and the prompt texts (`conversation_engine/`, `ai/persona_voices.py`, `system/coach_dm.py`, `supervision/checkins.py`).

| What | Where | For an additional wearer | Status |
|---|---|---|---|
| Model prompts (voice, personas, coach guidance, check-in questions) | `conversation_engine/`, `ai/`, `system/coach_dm.py` | Generic; nothing personal | Clean |
| Coaching memory, conversation, Trust, incidents, tokens | each wearer's own database | Separate per wearer (tenancy isolation, tested) | Clean |
| Gear inventory (`DEFAULT_OWNED`) | `obedience/service.py` | Starts empty (`ObedienceTasks.default_owned`) | Fixed (parity 12) |
| Gear catalogue names are the owner's items and brands ("gray Satisfyer Deep Diver", "purple RealRock dildo", "Hidden Desire bondage harness") | `obedience/service.py` `GEAR`, task texts | A wearer can only `gear add` these exact pieces; their own items have no entry | Fixed 2026-11-16: an additional wearer sees pieces by role (`ROLE`) and names their own (`gear add vibrating plug as black silicone plug`, migration 088); task texts use their names for toys (`NAME_VARIANTS`); restraints keep their names for the safety notes |
| Erotic task catalogue written around the owner's limits and items | `obedience/service.py` | Off at the start (`erotic_tasks` seeded off); a wearer switching it on gets the owner's catalogue filtered by gear | **Open**: needs the generic gear above and the wearer's limits from onboarding |
| Training plan (rack, weights, Saturday/Sunday schedule, the vest) | `training/plan.py` | Off at the start (`workouts` seeded off) | **Open**: a per-wearer plan before anyone switches workouts on |
| Workout tasks (pull-ups with the vest, push-ups on handles with the Deep Diver) | `obedience/service.py` `WORKOUTS` | Off with `workouts` | **Open**, with the training plan |
| Temptations name the Deep Diver | `temptations/service.py` | Off at the start (`temptations` seeded off) | Owner: fixed (offered only while the Deep Diver is on his gear list). Additional wearers: **open** with generic gear |
| Task board extras (push-ups with the Deep Diver) | `task_board/service.py` | Owner-only (not in parity yet) | Open when the board gets parity |
| Toy control (Deep Diver over Intiface) | `toy_control/` | Not wired for anyone | n/a |

Rule for the operator (owner, 2026-11-16): a tester's settings are never loosened because they asked the operator directly; any loosening
goes through the bot's own rules (the 24-hour wait), exactly as for the owner.
