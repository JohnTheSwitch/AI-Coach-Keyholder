# protocol

**Question it answers:** which protocol rules does he follow now, did he follow them, and what fixed reply does a message get before the
model sees it?

Owner, 2026-11-15/16 (roadmap 6b, 6c, 6d, P2; autonomous queue A2, A3, A4).

- **Owns:** `protocol_ladder` (migration 082): rung, since when, the next rung once announced, his title for the bot, scan marks.
- **The ladder:** rung 1 (the morning report, `supervision`) starts the ladder. A new rung after the current one held `HOLD_DAYS` (14)
  with at most `MAX_MISSES` (1) duty misses in the last 14 days, announced a day ahead, never removed. Paused (vacation, halt, wear
  exemption) = no climbing.
  1. Morning report (duty). 2. Ask properly: a hygiene / exemption / release request without "please" gets only "Ask properly: please, and
  why." (manner, no penalty). 3. Thank within 2 hours of every verified task (duty: a thank-you check-in per verified task, slots 91-93).
  4. Title at the start of every message: `title <word>` sets it; a free-text message without it gets only "Address me properly." (manner;
  P2). Commands and buttons never pass through this gate. 5. Weekly reflection after the Sunday review, at least 10 words, due Monday 12:00
  (duty, slot 94). **Rung 6 (evening kneel) is not built yet** (decided by Claude, 2026-11-16): the ladder stops at 5 (`TOP_RUNG`) until it
  is; rung 5 cannot be held before late January.
- **A duty miss is a missed check-in:** the rows are check-ins written by `supervision` (`open_duty`, `answer_duties`), so a miss has
  exactly the usual consequence and counts in the day's check-ins.
- **No debate (6b):** pushback on a rule (fixed English and Czech phrases) gets one fixed reply naming the open item (Penalty Window, lines
  owed, today's task) and "Arguing changes nothing." Nothing else that turn, no cost.
- **Wellbeing (6d):** the Sunday review asks how he really is; for 24 hours after it, a message with hard words gets a calm fixed check
  (health exemption, vacation), never a consequence.
- **How he is addressed:** a context fragment for the voice (`conversation_engine/context_providers/address_provider.py`): "slave"
  normally (boy / pet / toy / sub for variety), "good boy" only after a verified task in the last 24 hours, no title and no praise during a
  Penalty Window; when the window ends, one DM: "Window's over. Good boy for getting through it."
- Health, safety and emergency words always pass the gate untouched.
