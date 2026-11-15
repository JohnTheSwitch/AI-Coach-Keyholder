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
| Q1 | **Deploy the waiting batch** (done 2026-11-14) | Persona fixes (refusal retry + fixed line, no invented rules/signatures/records, never accuse without evidence, exact Iron Contract text) and lines on paper |
| Q2 | **Windowless restarts** (done 2026-11-14) | The bot is started without a console window; closing a window can no longer stop it |
| Q3 | **Persona check script** (done 2026-11-14: 0 problems in 75 replies) | `python -m scripts.persona_check`: fixed scenarios (begging, a confession, an honest report, a question about an unknown rule, a contract question) through the local model for all 15 personas; counts refusals, copied lines, invented facts and accusations. Run before and after every prompt change; never part of the test suite (the suite stays deterministic) |
| Q4 | **Weekly review shows brat taming** (done 2026-11-14) | Adds lines owed / done and recovery tasks verified (hours earned) to the Sunday review; plain facts, no Trust scores |
| Q4b | **Daily obedience tasks (erotic / BDSM)** (done 2026-11-14) (owner, 2026-11-14: "a strict keyholder that would make me an obedient slave") | One task a day at a random moment in free time (never quiet hours or work hours), from a closed catalogue that uses only what he owns: **butt plugs, restraints, a collar** (nipple clamps once he has them). Shown as a Chaster task: a photo with Chaster's code, judged by the community. **Mandatory:** not done in time or the photo rejected = a Warning-level incident (Trust and 20 lines). **Limits, never crossed:** nothing public or outdoors; no photo shows his face or anything he could be recognised by (the task text says what the photo shows and only that); no strong pain (no ballbusting); restraints only ones he can free himself from; wearing times capped. Chaster's one task slot: a spot check goes first, then the daily task, then a sheet of lines, then a recovery offer. Health, safety and a stated need always cancel a task without consequence |
| Q4c | **His training plan in the bot** (done 2026-11-15) (owner, 2026-11-15: "I built it to help me workout") | The plan from the workout chat (2026-11-14): Saturday full leg day (A and B alternate), Sunday full upper (supersets), one short session on Monday or Thursday (odd ISO week legs, even week upper; no legs Friday or Sunday). The session is that day's obedience task (it replaces the erotic one): exercises, sets and his current weights in the DM, proof = a Chaster photo of the day's training log with the code. **Full sessions are mandatory** (missed = asked to explain, unexplained = Warning and 20 lines); **the short session is optional** (the plan: "skip it and continue at the weekend"). **Short day:** the bot asks on Monday morning (Monday or Thursday); Thursday if he doesn't answer. **Progression tracked:** he sends top sets in the DM (`log squat 105 4x9`); every set at the top of the range = +2.5 kg next time (incline press to 22.5 kg); a deload week (half the sets) every 7 weeks; the return-from-break rule (squat 95 kg, -10 %, one set fewer) for the first two leg days |
| Q4d | **Gear tags and "Are you alone?"** (done 2026-11-15) | Every obedience task names the gear it needs, also gear not owned yet (blindfold, collar, clamps, leather cuffs, padlocks, Gildo, Plug & Joy); variations when a piece is missing (eyes closed instead of the blindfold, fluffy cuffs instead of leather); only owned gear is used; he keeps the list in the DM (`gear`, `gear add blindfold`, `gear remove blindfold`). Before an erotic task the bot asks "Are you alone right now?": alone = the full version (up to 30 minutes, two add-ons, self-bondage with locked cuffs), not alone = a discreet one (one add-on, 15 minutes at most, quiet); both mandatory; no answer in 15 minutes = discreet. Safety in both: never blindfolded while restrained, nothing around the neck but a collar that is never tied or locked to anything, key and shears within reach, clamps 10 minutes at most |
| Q4e | **Task limits talk** (done 2026-11-15) | The owner's own hard limits stay: nothing public, nothing identifying, no strong pain. Safety rules changed with him: **blindfolded in cuffs** allowed with the cuffs in front (hands can always reach the blindfold), 20 minutes at most, incl. cuffs + blindfold + the vibrating Deep Diver; **cuffs behind the back** start with the fluffy cuffs (release lever tested first), blindfolded behind the back only after 3 verified behind-back tasks; **restraint time** 30 minutes at most, **wear tasks** (plug, collar, rope harness under a shirt) 60 minutes to 2 hours while he does something else; **gag tasks** told only in the DM, never in Chaster's text, hands always free, 20 minutes at most; **rope**: a decorative chest harness may be blindfolded, rope on the limbs never; **Hidden Desire bondage harness** (his choice): cuffs clip only to the waist rings, never to the collar ring, velcro rips open; blindfolded in it only after 3 verified harness tasks; its collar counts for collar tasks. Still never: anything that can pull on the neck, a gag with restraints, ice or timer releases |
| Q4f | **Training earns tokens** (done 2026-11-15) | A training session verified in Chaster adds **+0.15** (full) or **+0.08** (short) to the token pool on a day without a confirmed incident (a perfect day adds about 0.27), and on a day with goals it counts as one more kept item. On a training day the coach proposes no separate workout commitment. Unverified (a vote that never ended) earns nothing. **Week bonus +0.10** on Sunday when every planned session of the week was verified (health-cancelled or slotless ones excused, at least two). **Progression +0.05** per weight increase earned through `log`, only on a day with a verified session, one per exercise, never at a lighter weight than before, at most +0.10 a day (owner, 2026-11-15: "it needs to actually reward me") |
| Q4g | **Tokens harder; conditioning on** (done 2026-11-15) | Progressive Conditioning switched on for the owner's database. Base thresholds 1.5 (standard) / 3.0 (advanced), factor +0.05 per token release up to 2.0. Days per token, gym / no gym: standard about 3.8 / 5.2 at first, 7.6 / 10.5 at the cap; advanced 7.6 / 10.5, then 15 / 21. No minimum gap between releases (owner's choice) |
| G1 | **Relock picture checked** (done 2026-11-15) | 30 minutes after a planned hygiene relock, while the picture is required: none = asked to explain, unexplained = HYGIENE_PICTURE_MISSING (Warning, 20 lines) |
| G2 | **Health openings and the task's health button** (done 2026-11-15) (owner, 2026-11-15) | The relock picture is checked after health openings too (it restricts nothing). Health openings and the obedience task's "health or safety" button are counted; from the 3rd in a rolling week the keyholder asks about the pattern, and the weekly review shows the counts. Never a penalty by itself |
| G3 | **Consent switches ask again after 24 h** (done 2026-11-15; `opt_outs/`; research sharing stays immediate as a privacy right) (owner, 2026-11-15) | While locked, switching photo check-ins (and similar opt-outs) off is not done at once: after 24 hours the bot asks "Do you still want it off?" and only a yes switches it off. Halt, leave, health and emergency stay immediate |
| G4 | **Settings changes are visible** (done 2026-11-15; `settings_watch/`) (owner, 2026-11-15) | The bot notices changed quiet/workday hours or time zone, a runtime halt and restarts with changed settings, asks about them in the DM and lists them in the weekly review |
| G5 | **False health or emergency claims** (owner, 2026-11-15; philosophy v1.17) | A claim is granted at once; it is reviewed only when stored facts contradict it (a wish for release shortly before, an opening far longer than the need, a pattern); the wearer is asked, the judge decides with one appeal; confirmed false = a full Incident in the Honesty domain (may open or extend a Penalty Window) |
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

## Approved, not built yet (in order)

1. **Health shortcuts capped** (done 2026-11-15, with G2) (owner, 2026-11-15): health access stays immediate; a wear exemption auto-approved for health lasts at most
   24 hours, anything longer goes to the judge; a health answer to a detector question still excuses at once but is recorded, counted
   (from the 3rd in a week asked about) and reviewable under G5. Built together with G2-G5.
2. **G2-G5** above (health openings and the task's health button counted, consent switches ask again after 24 h, settings changes
   visible, false health / emergency claims).
3. **Judge on long emergencies** (owner, 2026-11-15): a health opening or emergency longer than a limit always goes to the judge, even
   without a contradicting fact. Limit: **3 hours** (owner, 2026-11-15).
4. **Earn the release** (owner, 2026-11-15): after a token is spent, the release is scheduled only once a release task is verified in
   Chaster (20 lines "I earn my release", a short obedience task, or on a training day the session itself). Rejected or missed: the token
   is kept and the task comes again the next day. Health and emergencies bypass it.
5. **Morning report** (owner, 2026-11-15): one DM with today's plan, any content, due by 12:00 on workdays / 13:00 at weekends, or 2 hours
   after the bot comes back if it was offline that morning (whichever is later); the bot asks for it on his first message of the day; none
   by the deadline = a missed check-in. Suspended during a wear exemption, a vacation or a halt. Code checks only that it arrived.
6. **From the keyholding research** (owner, 2026-11-15, all approved; docs/research/keyholding_and_training.md):
   a. **Surprise rewards:** about 1 in 5 verified obedience tasks adds +0.05 to the pool, unannounced until it happens.
   b. **No debate with brat lines:** pushback on a rule gets one short reply restating the rule and the consequence, nothing more that turn.
   c. **Protocol ladder:** one new rung after the current one held 14 days with at most 1 miss, announced a day ahead, never removed.
      Duty rungs: a miss = a missed check-in. Manner rungs: no penalty, the bot acts only when redone properly; health and emergency
      always bypass. Rungs: (1) morning report; (2) asking properly, with a reason, for a hygiene opening, an exemption or a release;
      (3) thank the keyholder within 2 hours of each verified task; (4) form of address at the start of messages (title asked when the
      rung comes; commands and buttons exempt); (5) weekly reflection in the Sunday review (obeyed / pushed back / one aim);
      (6) evening kneel, 5 minutes before quiet hours, a Chaster photo from behind (skipped during an exemption).
   d. **Wellbeing question** in the Sunday review, free answer, never scored; a hard answer gets a calm check, never a consequence.
   e. **Photo check-ins scale with lock length** (more on week-plus locks).
   f. **Skin-check reminder** in every hygiene opening; redness, numbness or broken skin means staying unlocked to heal, no consequence.
7. **Q5-Q10** above (parity for additional wearers, slice 8, Typing Lines desk research).

## Ideas (not approved; each needs the owner's yes)

- **The bot controls the Deep Diver** through Intiface Central (Satisfyer support; first try the PC's built-in Bluetooth, the owner will
  test it (2026-11-15, Claude reminds him); he does not want to buy a dongle; the toy then pairs with the PC instead of the phone app): random teasing, a buzz when a task is overdue, edging the bot runs.
  First step: a test that it pairs and responds. A new kind of action on his body, so asked first.
- **Satisfyer Top Secret** worn over the cage, driven the same way (only after the Deep Diver test works).
- **Typing Lines** extension: only after desk research shows the bot could keep rewriting the lock's extensions with it on.

## Shopping list (most important first; only gear the bot can use)

Tasks for each piece are already in the catalogue; after buying, send `gear add <piece>` and they start coming.

1. **Safety shears** (rope and cuff safety; until then, scissors within reach).
2. **New blindfold** (Ouch!, about 139 Kč): `gear add blindfold`.
3. **Hidden Desire Extreme Multiple Bondage Harness** (799 Kč; collar + waist strap with 8 D-rings + velcro cuffs; owner's choice
   2026-11-15, replaces a separate collar): `gear add harness`.
4. **Adjustable nipple clamps** (Taboom, about 389 Kč): `gear add clamps`.
5. **Leather cuffs** (Ouch!, about 389 Kč) and **two small padlocks** (for locked self-bondage, keys within reach): `gear add leather cuffs`,
   `gear add padlocks`.
6. **Gildo No. 27** (about 589 Kč, plug ladder 4.5 cm): `gear add gildo`.
7. **Plug & Joy medium** (about 255 Kč, plug ladder 5.5 cm): `gear add plug joy`.
8. **CSR Bluetooth dongle**: only if the Deep Diver control idea is approved.
9. **Satisfyer Top Secret** (about 1,299 Kč): only after the Deep Diver test works.

## Declined (2026-11-14; not to be proposed again without new reasons)

"Loosening is hard" governance, the random rule check, capped time-adding (from the core-mcp README); the hygiene debrief question,
"answer or freeze", the non-negotiating keyholder; a smaller cage as a punishment (smaller is more comfortable for him); the electro cage
and other session-only gear the bot cannot use; ManCage one-time locks (they don't fit his cage). 2026-11-15: begging pushes the next
release back (could spiral); a spent token not always meaning an orgasm (ruined / cleaning only); a stronger penalty for a missed training
session (an unexplained miss already costs the whole day's pool progress, never below zero); a minimum gap between releases.

**Standing goal (owner, 2026-11-13): make this system the best version of itself.** Between slices, Claude reads the logs and the bot's chat
history for bugs and design gaps, and proposes them with a recommendation; approved ones are added here.

Not on the list on purpose: the 24-hour cooling-off rule (the owner said not yet; Claude reminds when the system is more mature).
