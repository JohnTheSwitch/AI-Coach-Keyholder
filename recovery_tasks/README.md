# recovery_tasks

Brat taming B3 (owner, 2026-11-14; migration 067). During an ACTIVE Penalty Window the bot offers one chore at a time as a Chaster task: a
photo of the result with Chaster's code, checked by the community. **Each passed task takes 2 hours off the window.**

- **Offer:** a card "Earn 2 hours off your Penalty Window. Show me <chore> ..." with **Take it** / **Not now**, valid for 2 hours. Chores come
  from `CHORES` (results a photo can show; the last three are not repeated).
- **Take it:** the Chaster task "Show: <chore>" (photo + code, community review) is assigned; 2 hours to submit.
- **Verified:** the Recovery Plan task completes, its `recovery_plan.task_completed` event becomes a Recovery Credit in the Penalty Engine
  (philosophy 3.4: at most half the window, never taken back), and `penalty_engine/window.py` now counts credits against the window: it ends
  when active hours plus credits reach its target (until 2026-11-14 credits were recorded but never shortened anything). A window extended
  meanwhile regenerates its plan; the verified chore is then recorded through a fresh task, never lost.
- **Rejected, late or missing:** nothing earned and nothing else; a recovery task is voluntary, so a failure is never an Incident. A vote that
  never ends earns nothing (unknown stays unknown). After "Not now", an expired offer or a failure, the next offer comes after 6 hours.
- **Never:** during a vacation, while the window is frozen, when the task would reach into quiet hours, while the runtime is halted, or while
  Chaster's one task slot is taken (a spot check goes first, the wearer's own task is never replaced). Time is never changed.
- The community review watch leaves these photos to this module (never a rejected-verification Incident).

Wiring: `system/recovery_wiring.py` (same Chaster port and lock as the spot checks), buttons through `system/dm_actions.RecoveryDm`
(scope `recovery`). Table: `recovery_chaster_tasks`. Events: `recovery_tasks.offered`, `.assigned`, `.passed`, `.closed`.
