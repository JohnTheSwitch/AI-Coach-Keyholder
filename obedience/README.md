# obedience

Daily obedience tasks (owner, 2026-11-14; work queue Q4b; migration 069): "a strict keyholder that would make me an obedient slave".

- **One task a day** at a random moment in free time (never quiet hours or work hours), chosen on the day's first pass in free time.
- **Closed catalogue** (`CATALOG`) using only the owner's own gear: metal plugs, the vibrating plug, two suction-cup dildos, the cotton and
  hemp ropes, the cuffs; about one day in four a workout task from his home gym (rack, pull-ups, push-ups, plank, training log).
- **Shown as a Chaster task:** a photo with Chaster's code, judged by the community, within 60 minutes. Every text says what the photo
  shows and only that: never his face or anything he could be recognised by.
- **Mandatory:** not done in time, late, or the photo rejected -> asked to explain (infractions path, the same judge); unexplained =
  `OBEDIENCE_TASK_MISSED`, a Warning-level incident (Trust and 20 lines: "I obey my keyholder's tasks.").
- **"I can't now (health or safety)"** cancels the task with no consequence (a stated need is believed, philosophy 3.9).
- **Limits, never crossed:** nothing public or outdoors; no strong pain; rope never around the neck, shears within reach, 20 minutes;
  cuffs with the key within reach; no gag task (a photo would show his face).
- **Chaster's one task slot:** a spot check goes first; a due obedience task goes before a sheet of lines; when the slot stays taken until
  free time ends, the day is skipped without consequence. A vote that never ends never counts against him.
- Never during a vacation or while the runtime is halted. The community review watch leaves these photos to this module.

Wiring: `system/obedience_wiring.py`; buttons through `system/dm_actions.ObedienceDm` (scope `obedience`). Table: `obedience_tasks`.
Events: `obedience.task_assigned`, `obedience.task_decided`.

**Plug ladder (owner, 2026-11-14).** About one erotic day in three is a plug task on `PLUG_LADDER` (sizes from the shop pages): Deep Diver
3.3 cm, clear Crushious Bijou 4.1 cm, glass Gildo No. 27 4.5 cm, Plug & Joy medium 5.5 cm, Wolf Rimfire S 6.0 cm. He moves up one rung only
after 3 verified wears on the current one, and only through plugs he owns (`owned`; the Gildo and the Plug & Joy are not bought yet, so the
ladder skips them until marked owned). Wearing time grows with each verified wear at a rung: 20, 30, 45, 60 minutes. Pain stops it with no
consequence.

**No task during a wear exemption (owner, 2026-11-14).** While a real wear exemption is in force (`ExemptionService.wear_exemption_in_force`,
kind `exemption`), nothing is planned or started, and an open task is cancelled with no consequence. A hygiene opening is NOT a wear
exemption: asking for one never cancels a task.

**Variety (owner, 2026-11-14: "make the tasks so that it's not repetitive").** Most erotic days are composed from a pose (kneeling,
all fours, forehead to the floor, facing a wall, corner, face down) and one or two add-ons (the vibrating Deep Diver, blindfold, cuffs,
thighs tied, 'locked' on the thigh): about 60 combinations, never blindfolded in restraints, one restraint at a time. Nothing is repeated
within 14 days and the main gear never repeats two days running (plug sessions get a rest day between them). Workouts rotate through nine
from his home gym; heavy lifts never with a plug.

**Plug-training rules from research (2026-11-14):** warm up on the size below; short sessions that grow slowly (20 -> 60 minutes);
water-based lube on silicone (Deep Diver) and PVC (Plug & Joy, Rimfire); never numbing lube; never overnight (tasks only run in free time);
stop at pain or bleeding, and a plug task cancelled for health sends the next one a rung back down.

**Gear tags (owner, 2026-11-15; migration 073).** Every task names the gear it needs (`Task.needs`, keys of `GEAR`), also gear he does
not own yet (blindfold, collar, clamps, leather cuffs, padlocks, Gildo No. 27, Plug & Joy medium). A task is given only when he owns all of
it. Composed add-ons have variations in order of preference (`ADDONS`): eyes closed when there is no blindfold, the fluffy cuffs when there
are no leather ones. He keeps the list in the DM: `gear` shows it, `gear add <piece>` / `gear remove <piece>` change it (table
`obedience_gear`; a piece with no row keeps `DEFAULT_OWNED`, his inventory of 2026-11-14 minus the broken blindfold). The plug ladder counts
only plugs on the list. Event: `obedience.gear_changed`.

**"Are you alone right now?" (owner, 2026-11-15; migration 073).** An erotic day is planned without a task (`ASK_KEY`). When its moment
comes and Chaster's slot is free, the bot asks (status `asking`): "I'm alone" gives the full version (up to 30 minutes, two add-ons,
self-bondage with locked leather cuffs, louder toys), "Not alone" a discreet one (one add-on, 15 minutes at most, the Deep Diver on its
lowest setting, nothing long to set up). Both are mandatory; no answer in 15 minutes = the discreet version. "I can't now (health or
safety)" works while being asked too. A training day is not asked. Safety lines in the DM follow the gear (`safety_notes`): key and shears
within reach and never blindfolded while restrained; the collar loose enough for two fingers and never tied, clipped or locked to anything;
clamps off after 10 minutes at most. Events: `obedience.alone_asked`, `obedience.alone_answered`.

**Limits talk (owner, 2026-11-15).** His own hard limits are unchanged (nothing public, nothing identifying, no strong pain); the
safety rules were agreed with him: blindfolded in cuffs only with the cuffs in front, 20 minutes at most (`blind_cuffs_vibe`: cuffs,
blindfold and the vibrating Deep Diver); behind the back (`cuffs_back`) only in the fluffy cuffs with the release lever tested first, and
blindfolded behind the back only after `BACK_BLIND_AFTER` (3) verified behind-back tasks; restraint time 30 minutes at most; wear tasks of
60 minutes to 2 hours without restraints while he does something else; the gag is never in Chaster's text, only in the DM (`Task.dm_note`,
"Only between us: ..."), never with restraints, 20 minutes at most; a decorative rope chest harness may be blindfolded, rope on the limbs
never. **Hidden Desire bondage harness** (`bondage_harness`, `gear add harness`): cuffs clip only to the waist rings, never to the collar
ring, the velcro rips open; `hd_blind` only after 3 verified harness tasks (`Task.after`); the harness's collar counts for collar tasks
(`IMPLIED`). Still never: anything that can pull on the neck, a gag with restraints, ice or timer releases.

**He does not live alone (owner, 2026-11-14; migration 070).** Every task message says: in your room with the door closed, quiet, nothing
left lying around or visible afterwards. "Someone's around (+2 h, once)" (was "Not alone right now") moves the photo deadline to 2 hours from the press, once per
task and never past free time; after that the task is done or it fails. No gag task; loud toys only in the alone version; the collar only inside a task.
