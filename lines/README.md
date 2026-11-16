# lines

Brat taming B2 (owner, 2026-11-14; migration 066). Every confirmed incident gets lines:

- **20** for a Warning-level incident (`penalty_warning.recorded`), **40** for one that opens or extends a Penalty Window
  (`extension.decision_recorded`, which now carries `rule_group_id`). One assignment per incident; only incidents confirmed after this
  was deployed (the outbox never replays published events).
- The sentence matches the rule broken (`SENTENCES`; anything new gets "I follow the rules I agreed to.").
- The wearer types them in the bot DM. **Code counts them, no model:** a line counts when it is the sentence exactly, ignoring case, spacing,
  a leading number ("12." / "12)") and the closing full stop. Several per message, across messages; extra lines go to the next open
  assignment, wrong lines are named and don't count. A message with no owed sentence in it is an ordinary message.
- **What waits while lines are open:** the next planned (discretionary) hygiene break (`exemptions/service.py`; the card offers only "It's a
  health need" and "Not now") and, as release debt (roadmap B4), spending a token on a release (`ReleaseGates.release_debt`, a hard veto like
  a Penalty Window; the token is kept). **A health need never waits** (philosophy 3.9), and a read failure holds nothing.

Table: `line_assignments` (written only by `service.py`). Events: `lines.assigned`, `lines.completed`. The single wearer only (the owner's
notice user); tenants are not wired.

**On paper (owner, 2026-11-14; migration 068).** When Chaster tasks work (the spot checks' setup), lines are handwritten: the oldest owed
sheet becomes a Chaster task "Show: 20 handwritten lines: <sentence>" (photo of the sheet with Chaster's code, community review). Verified ->
done; rejected (or the task taken off the lock) -> still owed, a new sheet is asked for. A sheet waits for Chaster's one task slot (a spot
check or recovery task goes first); once the oldest sheet has waited 3 hours without a task, typing the lines in the DM counts too, so owed
lines never hold hygiene forever. Typed lines otherwise get "Lines go on paper". The community review watch leaves these photos alone.
Chaster's own "Typing Lines" extension is a partner extension: with it on the lock the bot could no longer rewrite the extension list
(hygiene openings, the extension guard, the relock picture), so it is not used.


## Swats instead, and the written request (owner, 2026-11-15/16; autonomous queue B1, B2; migration 083)

- **Swats instead (B1):** every lines message has a "Swats instead" button: one light hand swat per two lines (`swats_for`), counted aloud,
  shown as a Chaster task "N light hand swats, counted aloud. Photo of the skin only." (community review). Not submitted within
  `SWATS_WITHIN` (2 hours) of being in Chaster, rejected or taken off: the lines come back unchanged (`mode` reset), never a new incident.
- **Before a release (B2):** the release task is his choice of three: release lines, swats, or a written request, never the one he did for
  the last release (`_release_choices`). Default: lines, or the written request when lines were last. The written request is two DM
  messages of at least `REQUEST_MIN_WORDS` (30) words each: why he earned it, then what he will do better; code counts words only. Shorter
  messages are ordinary messages. The "do better" text is shown in the Sunday review a week later (`get_promises_between`).
- `line_assignments.mode` (NULL = lines, 'swats', 'request'), `mode_chosen_at`, `request_why`, `request_better`. Buttons: scope `lines`.
