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
| 5 | **Weekly review** (done 2026-11-14) | Sunday at 19:00 local: the week in plain facts (commitments kept / not kept / late, check-ins answered, spot checks passed, tokens earned) | Written by code, no model, no Trust scores; skipped during a vacation; `weekly review off` turns it off |
| 6 | **Self-hosting polish** (done 2026-11-14) | `setup tasks`: add the Tasks extension (community review on, no points, no time actions) to the owner's lock through the existing extension review with consent; `setup check` reads whether it is there | Never changes time or other extensions |
| 7 | **Multi-wearer parity 3-7** | For additional wearers, one at a time: exemptions and hygiene openings; the detectors and the judge; photo check-ins; the notification queue; confession handling | Same rules as the owner's; each wearer's own time zone already applies |
| 8 | **Honesty 5 and the coach for additional wearers** | The invitation discloses the coach and spot checks; `proof checks off` for the wearer; their Honesty domain created at acceptance | On by default for them once disclosed (owner, 2026-11-13) |
| 9 | **Live trial with a second wearer** | With the person the promo invites, after consultation | Needs that person: always asked |

## Brat taming (owner, 2026-11-14: "I want this system to keep me in check")

Chosen from the proposal built on the owner's own chat history (edging during a hygiene opening, days of silence, begging, arguing about goals).

| # | Slice | Decisions already in it |
|---|---|---|
| B1 | **Hygiene relock picture** (done 2026-11-14) | On the owner's lock only, while hygiene Trust is below 0.70 every hygiene opening ends with a verification picture; at 0.70 or above it switches off by itself. Never on another wearer's lock |
| B2 | **Lines** (done 2026-11-14) | Every confirmed incident: 20 lines for a Warning-level incident, 40 for one that opens or extends a Penalty Window; the sentence matches the rule broken; typed in the bot DM and counted exactly by code (no model). While lines are open, the next planned hygiene break waits; health access is never blocked |
| B3 | **Verified recovery tasks** (done 2026-11-14) | During a Penalty Window, a Chaster task with photo and code, community-checked like a spot check; each passed task takes 2 hours off the window (Recovery Credit, philosophy 3.4); one at a time (a spot check goes first) |
| B4 | **Release debt** (done 2026-11-14: open lines; recovery tasks stay voluntary, so they are not debt) (from the core-mcp README, `Downloads/README2.md`, owner 2026-11-14) | Consequences still owed (open lines, later open recovery tasks) must be paid before a token can be spent on a release; the release waits, nothing is lost. Not chosen from the same README: "loosening is hard", the random rule check, capped time-adding |

Not chosen (2026-11-14): the hygiene debrief question, "answer or freeze", the non-negotiating keyholder.

## Work queue (proposed 2026-11-14, built in this order without asking once the owner approves it)

Every item is built, fully tested, committed, deployed (bot restarted without a visible window) and pushed on its own. The decisions written
here are part of the approval.

| # | Item | Decisions already in it |
|---|---|---|
| Q1 | **Deploy the waiting batch** | Persona fixes (refusal retry + fixed line, no invented rules/signatures/records, never accuse without evidence, exact Iron Contract text) and lines on paper |
| Q2 | **Windowless restarts** | The bot is started without a console window; closing a window can no longer stop it |
| Q3 | **Persona check script** | `python -m scripts.persona_check`: fixed scenarios (begging, a confession, an honest report, a question about an unknown rule, a contract question) through the local model for all 15 personas; counts refusals, copied lines, invented facts and accusations. Run before and after every prompt change; never part of the test suite (the suite stays deterministic) |
| Q4 | **Weekly review shows brat taming** | Adds lines owed / done and recovery tasks verified (hours earned) to the Sunday review; plain facts, no Trust scores |
| Q5 | **Parity 5: exemptions and hygiene openings for additional wearers** | Same rules as the owner's, in the wearer's own database; an opening acts only on their CONFIRMED paired lock and only after the owner cleared that tenant's execution halt. The relock picture, lines and recovery tasks stay owner-only |
| Q6 | **Parity 6: detectors and the judge for additional wearers** | Hygiene misuse, missed check-ins and lock claims through their own signals and the same judge; incidents go to their own Penalty Engine |
| Q7 | **Parity 7: photo check-ins for additional wearers** | Off until the wearer sends `photo check-ins on` themselves |
| Q8 | **Parity 8: confession handling for additional wearers** | Same deterministic path as the owner's |
| Q9 | **Roadmap slice 8: Honesty 5 and the coach for additional wearers** | As written in slice 8 above (the invitation discloses the coach and spot checks; `proof checks off`; Honesty domain at acceptance) |
| Q10 | **Typing Lines: desk research only** | Read Chaster's and the partner's public documentation to find out whether a partner extension can be resent unchanged or driven by API. No write to any lock; the result is a written proposal |

Between items, the standing goal applies: read the logs and the bot chat for bugs; a bug fix that changes no rule is made without asking.

**Still asked every time, whatever this queue says:** anything that touches the owner's live lock in a new way (a new extension setting or
action), the live trial or anything involving another person, any rule or number not written here, anything touching the philosophy's hard
limits (a `critical_change`), and any live Chaster experiment (e.g. Typing Lines on a test lock).

**Standing goal (owner, 2026-11-13): make this system the best version of itself.** Between slices, Claude reads the logs and the bot's chat
history for bugs and design gaps, and proposes them with a recommendation; approved ones are added here.

Not on the list on purpose: the 24-hour cooling-off rule (the owner said not yet; Claude reminds when the system is more mature).
