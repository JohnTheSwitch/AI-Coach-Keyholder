# Roadmap (approved by the owner 2026-11-13)

The order of work after honesty and proof slice 3. **Owner's rule for this roadmap:** once approved, the slices below may be built, tested,
committed, deployed and pushed one after another without asking again. The decisions written into each slice are part of the approval.
Still asked every time: anything that touches the live lock in a new way, a consent given on the owner's behalf, a change to a number or rule
that is not written here, and anything involving another person.

| # | Slice | What | Decisions already in it |
|---|---|---|---|
| 1 | **Honesty 3: deploy** (done 2026-11-13) | Spot checks live; create the `honesty` Trust domain with the owner's consent | Starts at 0.75 (20 %); sleep and abstinence commitments and anything reaching into quiet hours are never checked |
| 2 | **Night commitments** (done 2026-11-13) | Bedtime and sleep commitments ("go to bed 30 minutes earlier") can never earn today: "Done?" comes at 21:30, before bed, and a report the next morning is late | Recognised by the same words as the spot-check exclusion; asked **the next morning** at the end of quiet hours ("Did you?"); the report window runs until 12:00 that morning; never spot-checked |
| 3 | **Honesty 4: contradictions** (done 2026-11-14) | Facts that contradict a report become a question (infractions path), never an automatic verdict | (a) a DM to the bot after the stated bedtime that night contradicts a kept bedtime commitment; (b) a Chaster task completed outside the time claimed (covered by the spot-check deadline, nothing else to build); (c) lock-claim Incidents move from `routine` to `honesty`. Warning only, like a failed spot check |
| 3b | **Personas made real** (done 2026-11-14) | The 15 personas chosen at onboarding all sound like one keyholder: the archetype never reaches the model and one fixed VOICE_GUIDE covers everyone. Each gets its own voice (a few lines plus two or three example lines) on top of a shared core; `persona` lists them, `persona <name>` switches after a confirmation | Owner, 2026-11-13: **all 15 are keyholders**, differing in how they hold the key. The shared core stays: frank about arousal and denial, no assistant phrases, warm and direct on health or distress, tone only and never authority. Switching changes the voice only, never rules, Trust or tokens |
| 4 | **Token economy report** (done 2026-11-14) | `python -m cli economy` (owner): tokens earned per day, days per token, what blocked earning (incidents, missed check-ins, late reports) over 7 / 14 / 30 days | Read-only. On **2026-11-27** a scheduled Claude task (`coach-token-pace-review`) reviews the pace and writes `docs/reviews/token-pace-2026-11-27.md`; a change to earning rates is then proposed, not made |
| 5 | **Weekly review** | Sunday at 19:00 local: the week in plain facts (commitments kept / not kept / late, check-ins answered, spot checks passed, tokens earned) | Written by code, no model, no Trust scores; skipped during a vacation; `weekly review off` turns it off |
| 6 | **Self-hosting polish** | `setup tasks`: add the Tasks extension (community review on, no points, no time actions) to the owner's lock through the existing extension review with consent; `setup check` reads whether it is there | Never changes time or other extensions |
| 7 | **Multi-wearer parity 3-7** | For additional wearers, one at a time: exemptions and hygiene openings; the detectors and the judge; photo check-ins; the notification queue; confession handling | Same rules as the owner's; each wearer's own time zone already applies |
| 8 | **Honesty 5 and the coach for additional wearers** | The invitation discloses the coach and spot checks; `proof checks off` for the wearer; their Honesty domain created at acceptance | On by default for them once disclosed (owner, 2026-11-13) |
| 9 | **Live trial with a second wearer** | With the person the promo invites, after consultation | Needs that person: always asked |

**Standing goal (owner, 2026-11-13): make this system the best version of itself.** Between slices, Claude reads the logs and the bot's chat
history for bugs and design gaps, and proposes them with a recommendation; approved ones are added here.

Not on the list on purpose: the 24-hour cooling-off rule (the owner said not yet; Claude reminds when the system is more mature).
