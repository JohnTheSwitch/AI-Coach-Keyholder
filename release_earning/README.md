# release_earning

Earn the release (owner, 2026-11-15; roadmap "Approved, not built yet", item 4). A token alone no longer plans a release: first a release task.

- **Training day:** the day's training session is the release task (verified in Chaster like every session, or credited by the owner).
- **Otherwise:** 20 release lines, "I am grateful to my keyholder for every release I earn." (`lines.service.assign_release`; on paper as a
  Chaster task like any lines; typing counts when the task slot stayed taken). Release lines are not a punishment: they never hold a hygiene
  opening, are not release debt and are not counted as owed lines in the weekly review.
- **Earned once per release:** a verified session or completed release lines since the last token spent on a release that was not refunded
  (`ReleaseEngine.get_last_spend_at`). Rejected lines are written again; a missed training day means release lines the next day. The token is
  never lost.

`ReleaseEarning.hold(now)` is asked by `system/release_tick.py` before a plan is made (after the hard vetoes); it returns
`release_waiting_for_training`, `release_waiting_for_lines` or None. A failure to read anything holds nothing, like release debt.
No table of its own; nothing here touches a token, a plan or a lock. Health openings, exemptions and the emergency option never go through the
release tick. The bot wires it where release debt is wired (`bot/discord_bot.py`).


## The release window (owner, 2026-11-15/16; story idea P4, autonomous queue B3; `window.py`, migration 083)

When a release is confirmed (`release_engine.last_confirmed_release`), a DM gives him 2 hours to relock and send a verification picture in
Chaster. A picture between the confirmation and the window's end + 10 minutes closes it; none = he is asked to explain
(RELEASE_WINDOW_OVERRUN, warning level, 20 lines "I relock on time when my keyholder says so."). Unreadable pictures are retried and dropped
after 6 hours (unknown never counts). No lock action. Owns `release_windows`.
