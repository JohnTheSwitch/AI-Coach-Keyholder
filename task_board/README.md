# task_board

The task board (owner, 2026-11-15; story idea P1, approved with its task list). Every Monday, at the first moment of free time, the DM offers
3 tasks for the week, each with a deadline and a reward in token progress: chores, fitness (never started on a training day), discipline and,
about 60% of weeks, one erotic task drawn from the obedience catalogue (the full version, only gear he owns, never the DM-only gag tasks).
No study tasks (he doesn't study).

- **Take it**: a promise. One taken task at a time; a week's taken rewards are capped at +0.10.
- **Doing it now** (in free time): it becomes an obedience task (`obedience.service.plan_board_task`, slot 2 of that day): Chaster task with
  a brief text, photo within the hour, community vote, the usual buttons (health or safety cancels without consequence).
- **Verified**: the reward joins that day's token pool (`get_board_rewards`, read by `system/daily_evaluation.py`).
- **Rejected / missed photo**: the obedience path asks him; unexplained = Warning (OBEDIENCE_TASK_MISSED).
- **Taken but never started by the deadline**: asked the same way. **Never taken**: expires without a trace.

Table `board_tasks` (migration 079), written only here. Events: `task_board.offered`, `task_board.taken`, `task_board.decided`. This module
never calls Chaster; wiring: `system/obedience_wiring.build_task_board`.
