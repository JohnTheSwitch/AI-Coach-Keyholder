"""
obedience/service.py

Daily obedience tasks (owner, 2026-11-14; work queue Q4b; migration 069): "a strict keyholder that would make me an obedient slave".

One task a day at a random moment in free time (never quiet hours or work hours), from a closed catalogue (`CATALOG`) that uses only the gear
the owner listed: metal plugs, the vibrating plug, two suction-cup dildos, two ropes, cuffs. No workout tasks (owner, 2026-11-17: erotic only;
the gym training plan is separate). It is shown as a Chaster task: a photo with Chaster's code, judged by the community. The photo shows what the text names and only that:
never his face or anything he could be recognised by.

  plan     on the day's first pass in free time, a random moment later that day is chosen (none left: no task that day)
  start    at that moment, when Chaster's one task slot is free (a spot check goes first), the task is assigned; PHOTO_DEADLINE to submit;
           the DM gets the task with one button: "I can't now (health or safety)"
  poll     verified -> passed; rejected, or nothing in time -> failed: the wearer is asked to explain (infractions path, the same judge);
           unexplained = OBEDIENCE_TASK_MISSED, a Warning-level incident (Trust and 20 lines)
  button   "I can't now (health or safety)" -> cancelled, no consequence (a stated need is believed, philosophy 3.9)

Plug ladder (owner, 2026-11-14: "Yes" to training up to his Wolf Rimfire S): about one erotic day in three is a plug task on the ladder
`PLUG_LADDER`, sizes from the shop pages. He moves up one rung only after LADDER_STEP verified wears on the current one, and only to a plug
he owns (`owned`); wearing time grows with each verified wear at a rung. Pain ends it with no consequence.

No task during a wear exemption (owner, 2026-11-14): nothing is planned or started while one is in force, and an open task is cancelled
without consequence when one begins.

Never: during a vacation, while the runtime is halted, or when the slot stays taken until the free time ends (the day is then skipped, no
consequence). Never anything public or outdoors, never strong pain; restraints only ones he can free himself from; times are capped.
"""

from __future__ import annotations

import logging
import random
import re
import uuid
from dataclasses import dataclass, replace as _replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable

from application.models import ActionSpec, OutgoingMessage
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso as _parse_iso
from notifications.queue import enqueue_in_transaction

logger = logging.getLogger("ai_coach_keyholder.obedience")

__all__ = ["ObedienceTasks", "Ports", "get_verified_training", "get_training_outcomes", "plan_extra_task", "credit_training", "plan_board_task", "get_obedience_state", "CATALOG", "Task", "PHOTO_DEADLINE", "WORKOUT_SHARE", "GEAR", "DEFAULT_OWNED", "ALL_GEAR"]

PHOTO_DEADLINE = timedelta(minutes=60)
# Owner, 2026-11-23: "I'll start myself and then we will continue after the restart". `run toy` or `run toy 45` in the DM starts the bot's
# session for the task he is doing now (the full task time, or the minutes he names, never more), like pressing "It's in: run it".
_RUN_TOY = re.compile(r"(run (the )?toy|run (the )?deep diver|spust hracku|spusť hračku)( (?P<min>\d{1,3})( ?(min|minutes|minut))?)?")
_EXTRA_TASK = re.compile(r"(extra (erotic )?task|ukol navic|úkol navíc)( (at|v) (?P<at>\d{1,2}:\d{2}))?")
VOTE_LIMIT = timedelta(hours=12)
CLOCK_SKEW = timedelta(minutes=2)
# Owner, 2026-11-17: "I don't remember approving that. I just wanted erotic tasks." The 1-in-4 workout task was Claude's own proposal (14 Nov),
# built without an answer. Obedience tasks are erotic only; his gym training plan (training:*) is separate and unchanged.
WORKOUT_SHARE = 0.0
PLAN_STEP = timedelta(minutes=15)
POSTPONE = timedelta(hours=2)            # "Someone's around": once per task (owner, 2026-11-14: he does not live alone)
# Owner, 2026-11-16 23:00: "Are you alone right now?" has no timer any more (it was 15 minutes, then the discreet version); it waits for his
# answer, quiet hours included, and is a missed task when still unanswered at midnight.
DISCRETION = "Do it in your room with the door closed, keep it quiet, and leave nothing lying around or visible afterwards."
SAFETY = ("Stop at once if anything hurts, bleeds, goes numb or feels wrong; that never counts against you. Never numbing lube, never "
          "overnight.")
ALONE, DISCREET = "alone", "discreet"
SURPRISE_SHARE = 0.2                     # owner, 2026-11-15 (research a): about 1 in 5 verified tasks...
SURPRISE_REWARD = "0.05"                 # ...adds this to the token pool, unannounced until it happens
PREP_AHEAD = timedelta(minutes=30)      # owner, 2026-11-15: the clean-up heads-up comes this long before an erotic task
# Owner, 2026-11-16 (queue A0): the heads-up goes out only when the task (chosen at the heads-up) goes in your ass; "may" when only one of
# its two versions (alone / not alone) does.
PREP_TEXT = ("Heads-up: your task comes in about 30 minutes, and it {certainty} a plug. Use the toilet now and clean up in the "
             "shower: lukewarm plain water, no soap. Without a douche, a thorough rinse is enough.")
RESTRAINT_SHORT = "Key and shears within reach."   # owner, 2026-11-16: after the stuck cuff, the one line that stays on every restraint task
ASK_KEY = "ask"                          # a planned task not chosen yet: chosen when he answers the alone question
BOTH = frozenset({ALONE, DISCREET})
ALONE_ONLY = frozenset({ALONE})
DISCREET_ONLY = frozenset({DISCREET})

# Owner, 2026-11-15: "build a set of tasks even for a gear I don't have yet and for each do some tag of what gear I need for it, some
# variations if I don't have certain gear and then a list of my gear and it would only use those I have things for." Every gear piece a task
# can need; he keeps the owned list in the DM (`gear`, `gear add blindfold`). DEFAULT_OWNED is his inventory of 2026-11-14 (the blindfold
# broke on 2026-11-15, so it is not owned until he adds it back).
GEAR: dict[str, str] = {
    "deep_diver": "gray Satisfyer Deep Diver (vibrating plug)",
    "bijou_large": "clear Crushious Bijou (metal plug)",
    "gildo_27": "glass Gildo No. 27 plug",
    "plug_joy_medium": "Plug & Joy medium plug",
    "rimfire_s": "Wolf Rimfire S plug",
    "dildo_purple": "purple RealRock dildo",
    "dildo_big": "big Lovetoy dildo",
    "rope_cotton": "red cotton rope",
    "rope_hemp": "natural hemp rope",
    "toy_cuffs": "fluffy cuffs",
    "leather_cuffs": "leather cuffs",
    "padlocks": "small padlocks with keys",
    "blindfold": "blindfold",
    "collar": "collar",
    "clamps": "nipple clamps",
    "gag": "Ouch! ball gag",
    "bondage_harness": "Hidden Desire bondage harness (collar, waist strap, velcro cuffs)",
    "airpods": "AirPods with noise cancellation",          # owner, 2026-11-15 (B5): sensory deprivation
    "leash": "leash",                                       # B6: waiting at the door (clips to the harness waist ring only)
}
DEFAULT_OWNED = frozenset({"airpods", "deep_diver", "bijou_large", "rimfire_s", "dildo_purple", "dildo_big", "rope_cotton", "rope_hemp", "toy_cuffs",
                           "gag"})
ALL_GEAR = frozenset(GEAR)
GEAR_ALIASES = {"gildo": "gildo_27", "plug_joy": "plug_joy_medium", "plug_and_joy": "plug_joy_medium", "rimfire": "rimfire_s",
                "bijou": "bijou_large", "diver": "deep_diver", "cuffs": "leather_cuffs", "padlock": "padlocks", "locks": "padlocks",
                "nipple_clamps": "clamps", "clamp": "clamps", "mask": "blindfold", "harness": "bondage_harness",
                "hidden_desire_harness": "bondage_harness", "ball_gag": "gag", "fluffy_cuffs": "toy_cuffs"}
# Leak audit (owner, 2026-11-16): the catalogue names are the owner's own items. An additional wearer sees each piece by its ROLE and may
# give it their own name (`gear add vibrating plug as black silicone plug`); task texts then use their name (NAME_VARIANTS, longest first).
# Only toys are renamed: restraints, the harness and the collar keep their names, because the safety notes are read from those words.
ROLE: dict[str, str] = {
    "deep_diver": "vibrating plug", "bijou_large": "large metal plug", "gildo_27": "glass plug", "plug_joy_medium": "medium plug",
    "rimfire_s": "small plug", "dildo_purple": "suction-cup dildo", "dildo_big": "big dildo", "rope_cotton": "soft rope",
    "rope_hemp": "hemp rope", "toy_cuffs": "fluffy cuffs", "leather_cuffs": "leather cuffs", "padlocks": "small padlocks",
    "blindfold": "blindfold", "collar": "collar", "clamps": "nipple clamps", "gag": "ball gag",
    "bondage_harness": "bondage harness (collar, waist strap, velcro cuffs)", "airpods": "noise-cancelling earphones", "leash": "leash",
}
NAME_VARIANTS: dict[str, tuple[str, ...]] = {
    "deep_diver": ("gray Satisfyer Deep Diver (vibrating plug)", "gray Satisfyer Deep Diver", "gray Deep Diver", "Satisfyer Deep Diver", "Deep Diver"),
    "bijou_large": ("clear Crushious Bijou (metal plug)", "clear Crushious Bijou", "Crushious Bijou", "clear Bijou", "Bijou"),
    "gildo_27": ("glass Gildo No. 27 plug", "glass Gildo No. 27", "Gildo No. 27", "Gildo"),
    "plug_joy_medium": ("Plug & Joy medium plug", "Plug & Joy medium", "Plug & Joy"),
    "rimfire_s": ("Wolf Rimfire S plug", "Wolf Rimfire S", "Rimfire"),
    "dildo_purple": ("purple RealRock dildo", "RealRock dildo"),
    "dildo_big": ("big Lovetoy dildo", "Lovetoy dildo"),
    "airpods": ("AirPods with noise cancellation", "AirPods"),
}
GEAR_ALIASES.update({role.replace(" ", "_"): key for key, role in ROLE.items() if "(" not in role})
GEAR_ALIASES.update({"bondage_harness": "bondage_harness", "earphones": "airpods", "vibrator_plug": "deep_diver", "vibrating_butt_plug": "deep_diver"})

IMPLIED = {"bondage_harness": frozenset({"collar"})}          # the harness has a collar: collar tasks work with it


def effective(owned) -> frozenset:
    """What tasks may use: his gear plus what a piece implies (the harness's collar)."""
    out = set(owned)
    for piece, more in IMPLIED.items():
        if piece in owned:
            out |= more
    return frozenset(out)


@dataclass(frozen=True)
class Task:
    key: str
    text: str                           # the Chaster task: what to do and exactly what the photo shows (plain, like "Show your lockbox")
    kind: str                           # erotic | workout
    needs: frozenset = frozenset()      # gear (GEAR keys) the task needs; given only when he owns all of it
    modes: frozenset = BOTH             # alone | discreet: which answers to "Are you alone right now?" it suits
    uses: frozenset = frozenset()       # main gear for "never two days in a row" (empty: the key's first word)
    dm_note: str = ""                   # said only in the DM, never in Chaster (owner, 2026-11-15: the gag)
    after: tuple = ()                   # (key prefix, n): given only after n verified tasks whose key starts with the prefix


PHOTO_BEHIND = "Photo from behind, waist down only."
# Owner, 2026-11-16 23:35 ("how am I supposed to take a picture with hands behind my head?"): said in the DM only, never in Chaster.
PHOTO_HOW = "For the photo, prop your phone up and use the self-timer; if you can't, one hand on the phone for the shot is fine."

# Owner, 2026-11-14: "make the tasks so that it's not repetitive". Most erotic days are COMPOSED from a pose and one or two add-ons, so there
# are dozens of combinations; a combination used in the last REPEAT_DAYS days is not used again, and the main gear never repeats two days in
# a row. The photo is always from behind, waist down: never his face.
# Alone (owner, 2026-11-15): up to 30 minutes, two add-ons, louder. Discreet: one add-on, at most 15 minutes, quiet.
POSES: tuple[tuple[str, str, int], ...] = (                # (key, words, max minutes alone)
    ("kneel", "kneeling upright, hands on thighs", 30),
    ("fours", "on all fours", 20),
    ("forehead", "kneeling, forehead to the floor", 20),
    ("wall", "facing a wall, hands behind your head", 20),
    ("corner", "kneeling in a corner, hands behind your back", 25),
    ("facedown", "lying face down, legs apart", 30),
)
DISCREET_MINUTES = 15
DEAF_MINUTES = 15                      # B5: sensory deprivation 15 minutes at most
CLAMP_MINUTES = 10
# Each add-on: variations in order of preference, (gear needed, words, modes). The first one he has gear for and that suits the mode is used;
# with none, the add-on is left out. A variation with no gear ("eyes closed") keeps a task alive while a piece is missing.
ADDONS: dict[str, tuple[tuple[frozenset, str, frozenset], ...]] = {
    "vibe": ((frozenset({"deep_diver"}), "Deep Diver in and vibrating", ALONE_ONLY),
             (frozenset({"deep_diver"}), "Deep Diver in, vibrating on its lowest setting", DISCREET_ONLY)),
    "blind": ((frozenset({"blindfold"}), "blindfolded", BOTH),
              (frozenset(), "eyes closed the whole time", BOTH)),
    "cuffs": ((frozenset({"leather_cuffs"}), "wrists in the leather cuffs, buckled together in front", BOTH),
              (frozenset({"toy_cuffs"}), "wrists cuffed in front, key within reach", BOTH)),
    # Owner, 2026-11-15: behind the back starts with the fluffy cuffs (the release lever opens them without the key).
    "cuffs_back": ((frozenset({"toy_cuffs"}), "fluffy cuffs behind your back, lever tested first", ALONE_ONLY),),
    # Owner, 2026-11-15: gag tasks, told by the bot only (never in Chaster's text); hands always free with it.
    "gag": ((frozenset({"gag"}), "", ALONE_ONLY),),
    "thighs": ((frozenset({"rope_cotton"}), "thighs tied with the red rope, shears within reach", BOTH),),
    "collar": ((frozenset({"collar"}), "wearing your collar", BOTH),),
    # Owner, 2026-11-15/16 (B5): sensory deprivation, never with cuffs behind the back, 15 minutes at most, hands free.
    "deaf": ((frozenset({"airpods"}), "AirPods in with noise cancellation on", BOTH),),
    "clamps": ((frozenset({"clamps"}), "clamps on your nipples", BOTH),),
}
# Owner, 2026-11-15 (limits talk): blindfolded in cuffs is fine with the cuffs in front (hands can always reach the blindfold) or, after
# BACK_BLIND_AFTER verified behind-back tasks, behind; never rope on the limbs while blindfolded; one restraint at a time; the gag only with
# hands free.
RESTRAINTS = {"cuffs", "cuffs_back", "thighs"}
_ADDON_BAN = {("deaf", "cuffs_back"), ("deaf", "gag"), ("blind", "thighs"), ("cuffs", "thighs"), ("cuffs", "cuffs_back"), ("cuffs_back", "thighs"),
              ("gag", "cuffs"), ("gag", "cuffs_back"), ("gag", "thighs")}
_POSE_BAN = {("cuffs", "wall"), ("cuffs", "corner"), ("thighs", "wall"), ("thighs", "fours"),
             ("cuffs_back", "wall"), ("cuffs_back", "corner"), ("cuffs_back", "fours")}   # hands are behind / legs must move
BACK_BLIND_AFTER = 3
BLIND_RESTRAINED_MINUTES = 20
GAG_MINUTES = 20
GAG_NOTE = "Ball gag in the whole time (only I know). Out at once if you feel sick or can't breathe freely through your nose."
REPEAT_DAYS = 14
# Owner, 2026-11-16 ("learning what you avoid", option 1): a task he avoided comes back more often. Avoided = postponed with "someone's
# around", or failed (missed, late, rejected) in the last AVOID_DAYS; the health-or-safety button, an exemption, no free slot and a vote
# that never ended NEVER count. Its parts (pose, add-ons, plug ladder, extra, workout) weigh AVOID_WEIGHT in the draw (never more: it is a
# cap, not a ratchet), and an avoided task may come back after AVOID_REPEAT_DAYS instead of REPEAT_DAYS.
AVOID_DAYS = 28
AVOID_WEIGHT = 2.0
AVOID_REPEAT_DAYS = 7

_DD, _BJ = frozenset({"deep_diver"}), frozenset({"bijou_large"})
EXTRAS: tuple[Task, ...] = (
    Task("dildo_kneel", "Kneeling with the purple RealRock dildo stuck to the floor in front of you. Photo of the dildo and your knees only.",
         "erotic", frozenset({"dildo_purple"})),
    Task("dildo_ride", "The purple RealRock dildo stuck to the floor and you riding it, with water-based lube. " + PHOTO_BEHIND,
         "erotic", frozenset({"dildo_purple"}), ALONE_ONLY),
    Task("dildo_size", "The big Lovetoy dildo next to your cage, for size. Photo of the two only.", "erotic",
         frozenset({"dildo_big"})),
    Task("rope_harness", "A chest harness in the red rope. Photo from the neck down, no face. Keep it 20 minutes, shears within reach.",
         "erotic", frozenset({"rope_cotton"}), ALONE_ONLY),
    Task("rope_ankles", "Your ankles tied with the hemp rope. Photo of your ankles only. Keep it 15 minutes, shears within reach.",
         "erotic", frozenset({"rope_hemp"})),
    Task("selfbond_cuffs", "Kneeling, wrists in the leather cuffs locked together in front, key within reach on the floor. " + PHOTO_BEHIND + " 20 minutes.",
         "erotic", frozenset({"leather_cuffs", "padlocks"}), ALONE_ONLY, frozenset({"cuffs"})),
    Task("selfbond_ankles", "Ankles buckled together in the leather cuffs, lying face down, key within reach. " + PHOTO_BEHIND + " 25 minutes.",
         "erotic", frozenset({"leather_cuffs"}), ALONE_ONLY, frozenset({"cuffs"})),
    Task("collar_kneel", "Kneeling in your collar, hands behind your back. Photo from the collar down, chin out of frame, no face. 15 minutes.",
         "erotic", frozenset({"collar"})),
    Task("collar_plug", "Your collar on and the clear Bijou in, on all fours. " + PHOTO_BEHIND + " Collar visible only from behind. 20 minutes.",
         "erotic", frozenset({"collar", "bijou_large"}), ALONE_ONLY, frozenset({"collar", "plug"})),
    Task("clamps_chest", "Clamps on your nipples, kneeling upright. Photo of your chest only, no face. 10 minutes, then off slowly.",
         "erotic", frozenset({"clamps"})),
    Task("clamps_vibe", "Clamps on your nipples, the Deep Diver vibrating in you, on all fours. Photo from the side, neck down only. 10 minutes.",
         "erotic", frozenset({"clamps", "deep_diver"}), ALONE_ONLY, frozenset({"clamps", "vibe"})),
    Task("blind_plug", "Blindfolded, kneeling, the clear Bijou in. " + PHOTO_BEHIND + " 20 minutes.", "erotic",
         frozenset({"blindfold", "bijou_large"}), ALONE_ONLY, frozenset({"blind", "plug"})),
    # Owner, 2026-11-15: the three together (cuffs in front so the blindfold can always come off, quick release in reach).
    Task("blind_cuffs_vibe", "Blindfolded, fluffy cuffs in front, the Deep Diver vibrating in you, kneeling. " + PHOTO_BEHIND + " 20 minutes.",
         "erotic", frozenset({"blindfold", "toy_cuffs", "deep_diver"}), ALONE_ONLY, frozenset({"blind", "cuffs", "vibe"})),
    Task("rope_harness_blind", "A chest harness in the red rope, blindfolded, kneeling. Photo from the neck down, no face. 20 minutes, shears within reach.",
         "erotic", frozenset({"rope_cotton", "blindfold"}), ALONE_ONLY, frozenset({"rope", "blind"})),
    Task("gag_kneel", "Kneeling upright, hands on your thighs, the Bijou in. " + PHOTO_BEHIND + " 20 minutes.", "erotic",
         frozenset({"gag", "bijou_large"}), ALONE_ONLY, frozenset({"gag", "plug"}), GAG_NOTE),
    # Owner, 2026-11-15: longer sessions, especially while doing something else (no restraints; restraint time stays 30 minutes at most).
    Task("wear_diver_long", "The Deep Diver in. Photo of the plug base only. Then keep it in 90 minutes while you do something else.",
         "erotic", _DD, BOTH, frozenset({"vibe"})),
    Task("wear_bijou_long", "The clear Bijou in. Photo of the plug base only. Then keep it in 60 minutes while you do something else.",
         "erotic", _BJ, ALONE_ONLY, frozenset({"plug"})),
    Task("wear_collar_long", "Your collar on. Photo from the collar down, chin out of frame, no face. Keep it on 2 hours.",
         "erotic", frozenset({"collar"}), ALONE_ONLY),
    Task("wear_rope_long", "A chest harness in the red rope under your shirt. Photo from the neck down, no face. Keep it on 60 minutes, shears within reach.",
         "erotic", frozenset({"rope_cotton"}), ALONE_ONLY, frozenset({"rope"})),
    # Owner, 2026-11-15: the Hidden Desire bondage harness. Cuffs clip only to the waist rings (a pull goes to the waist belt, not the neck);
    # the velcro rips open any time. Blindfolded in it only after HD_BLIND_AFTER verified harness tasks.
    Task("hd_front", "In the bondage harness, wrists in its cuffs clipped to the waist rings in front, kneeling. " + PHOTO_BEHIND + " 30 minutes.",
         "erotic", frozenset({"bondage_harness"}), ALONE_ONLY, frozenset({"harness"})),
    Task("hd_back", "In the bondage harness, wrists clipped to the waist rings behind you, kneeling. " + PHOTO_BEHIND + " 30 minutes.",
         "erotic", frozenset({"bondage_harness"}), ALONE_ONLY, frozenset({"harness"})),
    Task("hd_back_vibe", "In the bondage harness, wrists clipped behind you, the Deep Diver vibrating in you. " + PHOTO_BEHIND + " 20 minutes.",
         "erotic", frozenset({"bondage_harness", "deep_diver"}), ALONE_ONLY, frozenset({"harness", "vibe"})),
    Task("hd_blind", "In the bondage harness, blindfolded, wrists clipped to the waist rings in front, kneeling. " + PHOTO_BEHIND + " 20 minutes.",
         "erotic", frozenset({"bondage_harness", "blindfold"}), ALONE_ONLY, frozenset({"harness", "blind"}), after=("hd_", 3)),
    Task("hd_quiet", "The bondage harness on, cuffs open, kneeling quietly. Photo from the collar down, chin out of frame, no face. 15 minutes.",
         "erotic", frozenset({"bondage_harness"}), DISCREET_ONLY, frozenset({"harness"})),
    Task("plug_quiet", "The clear Bijou in, lying on your bed. Photo of the plug base only. Then keep it in 30 minutes at your desk.",
         "erotic", _BJ, DISCREET_ONLY, frozenset({"plug"})),
    # Owner, 2026-11-15/16 (B4, P7): prostate training, alone only; doing the session counts, an orgasm is not required, and a hands-free one
    # never counts as a release or touches tokens. At most once in REPEAT_DAYS like every fixed task.
    Task("prostate_training", "Prostate training: the Deep Diver in, lying down, hands off the cage. Photo of the plug base only. "
         "25 minutes, vibration your choice.", "erotic", frozenset({"deep_diver"}), ALONE_ONLY, frozenset({"plug"}),
         dm_note="Doing the session is what counts. A hands-free orgasm is allowed and is never counted as a release."),
    # B5: sensory deprivation, hands always free.
    Task("sensory_kneel", "Kneeling, blindfolded, AirPods in with noise cancellation on, hands on your thighs. " + PHOTO_BEHIND + " 15 minutes.",
         "erotic", frozenset({"blindfold", "airpods"}), BOTH, frozenset({"blind", "deaf"})),
    Task("sensory_kneel_eyes", "Kneeling, eyes closed the whole time, AirPods in with noise cancellation on, hands on your thighs. " + PHOTO_BEHIND
         + " 15 minutes.", "erotic", frozenset({"airpods"}), BOTH, frozenset({"blind", "deaf"})),
    # B6: waiting at the door on a leash: the harness waist ring to the door handle, never the collar.
    Task("door_wait", "Kneeling at the door, the leash from your harness waist ring to the door handle. Photo of the leash on the handle only. "
         "10 minutes.", "erotic", frozenset({"bondage_harness", "leash"}), ALONE_ONLY, frozenset({"harness", "leash"})),
)
WORKOUTS: tuple[Task, ...] = (
    Task("workout_squats", "The bar in your rack after 5 sets of squats. Photo of the bar and your legs only.", "workout"),
    Task("workout_pullups", "5 sets of pull-ups done, the last one with your vest. Photo of the bar and your hands only.", "workout"),
    Task("workout_pushups", "50 push-ups on your handles with the gray Deep Diver in. Photo from the side, neck down only.", "workout", _DD,
         uses=frozenset({"workout", "vibe"})),
    Task("workout_plank", "A 3-minute plank, cage visible. Photo from the side, neck down only.", "workout"),
    Task("workout_bike", "10 rounds of 20 s hard / 40 s easy on the air bike. Photo of the bike's display only.", "workout"),
    Task("workout_incline", "20 minutes of incline walking on the treadmill. Photo of the treadmill display only.", "workout"),
    Task("workout_calves", "4 sets of calf raises with the bar. Photo of the bar and your calves only.", "workout"),
    Task("workout_cable", "4 sets of cable rows on the Laubr. Photo of the stack and the handle only.", "workout"),
    Task("workout_log", "Workout done. Photo of the gym only.", "workout"),
)
# Owner, 2026-11-17: another wearer with no approved task that fits today gets only what their spot checks (which they agreed to) already
# ask: nothing they did not approve.
CUSTOM_FALLBACK = Task("custom:fallback", "Show your locked cage. Photo of the cage only.", "erotic")
CATALOG: tuple[Task, ...] = EXTRAS + WORKOUTS                # the fixed tasks (composed and ladder tasks are built when picked)
_BY_KEY = {t.key: t for t in CATALOG}


def suits(task: Task, owned: frozenset | set, mode: str) -> bool:
    """He owns everything the task needs and it suits his answer to "Are you alone right now?"."""
    return task.needs <= owned and mode in task.modes


def _variation(addon: str, owned, mode: str) -> str | None:
    for needs, words, modes in ADDONS[addon]:
        if needs <= owned and mode in modes:
            return words
    return None


def task_parts(key: str) -> set[str]:
    """The parts of a task that avoidance is remembered by: a composed task's pose and add-ons, the plug ladder, a workout, or the key itself."""
    if key.startswith("pose:"):
        pose, _, addons = key[len("pose:"):].partition("+")
        return {f"pose:{pose}", *(f"addon:{a}" for a in addons.split("+") if a)}
    if key.startswith("plug:"):
        return {"plug"}
    if key.startswith(("workout_", "training:")):
        return {"workout"}
    return {key}


def compose(rng, banned: set[str], yesterday_gear: set[str], owned=ALL_GEAR, mode: str = ALONE, *, back_blind_ok: bool = False,
            avoided: frozenset = frozenset()) -> Task | None:
    """A pose plus add-ons (two at most when alone, one when not), never a combination in `banned`, never yesterday's main gear, and only
    variations he has the gear for. None when nothing new is left."""
    combos = []
    names = [a for a in ADDONS if _variation(a, owned, mode) is not None]
    options = [(a,) for a in names] + ([(a, b) for i, a in enumerate(names) for b in names[i + 1:]] if mode == ALONE else [])
    for pose, words, minutes in POSES:
        for addons in options:
            if any((a, b) in _ADDON_BAN or (b, a) in _ADDON_BAN for a in addons for b in addons if a != b):
                continue
            if {"blind", "cuffs_back"} <= set(addons) and not back_blind_ok:
                continue
            if any((a, pose) in _POSE_BAN for a in addons) or set(addons) & yesterday_gear:
                continue
            key = "pose:" + pose + "+" + "+".join(addons)
            if key not in banned:
                combos.append((key, words, minutes, addons))
    if not combos:
        return None
    weights = [AVOID_WEIGHT if task_parts(c[0]) & avoided else 1.0 for c in combos]
    key, words, minutes, addons = rng.choices(combos, weights=weights)[0] if avoided else rng.choice(combos)
    cap = min(minutes, DISCREET_MINUTES) if mode == DISCREET else minutes
    if "clamps" in addons:
        cap = min(cap, CLAMP_MINUTES)
    if "gag" in addons:
        cap = min(cap, GAG_MINUTES)
    if "deaf" in addons:
        cap = min(cap, DEAF_MINUTES)
    if "blind" in addons and set(addons) & RESTRAINTS:
        cap = min(cap, BLIND_RESTRAINED_MINUTES)
    minutes = rng.choice([m for m in (10, 15, 20, 25, 30) if m <= cap])
    parts = ", ".join(w for w in (_variation(a, owned, mode) for a in addons) if w)
    needs = frozenset().union(*(next(n for n, w, _m in ADDONS[a] if w == _variation(a, owned, mode)) for a in addons))
    shown = f"{words[:1].upper()}{words[1:]}, {parts}." if parts else f"{words[:1].upper()}{words[1:]}."
    return Task(key, f"{shown} {PHOTO_BEHIND} {minutes} minutes.", "erotic", needs, frozenset({mode}),
                dm_note=GAG_NOTE if "gag" in addons else "")


# Bug fix 2026-11-17 (owner: "never two anal sessions in two days ... yesterday and the day before?"): the plug ladder was remembered as "plug"
# and the Deep Diver add-on as "vibe", so a ladder day was followed by a vibe day. Anything in him is one gear for the two-day rule.
ANAL = frozenset({"plug", "vibe"})


def gear_of(key: str) -> set[str]:
    """The main gear a task used (for "never two days in a row"); a plug of any kind counts as all of ANAL."""
    if key.startswith("pose:"):
        gear = set(key.split("+")[1:])
    elif key.startswith("plug:"):
        gear = {"plug"}
    elif (task := _BY_KEY.get(key)) is not None and task.uses:
        gear = set(task.uses)
    else:
        gear = {key.split("_")[0]}
    return gear | ANAL if gear & ANAL else gear


# Owner, 2026-11-16 23:35: the voters in Chaster don't know his toys by name, so they see what each one is; whether it vibrates is not shown
# (a photo cannot show it; the DM keeps it).
_SHORT_NAMES = {"gray Satisfyer Deep Diver": "vibrating plug", "clear Crushious Bijou": "clear plug", "glass Gildo No. 27": "glass plug",
                "Plug & Joy medium": "plug", "Wolf Rimfire S": "large plug",
                "Deep Diver": "vibrating plug", "Bijou": "clear plug", "Gildo": "glass plug", "Plug & Joy": "plug", "Rimfire": "large plug"}
_VIBRATING = ("in and vibrating", " in, vibrating on its lowest setting")


def chaster_brief(text: str) -> str:
    """Owner, 2026-11-15 ("who doesn't use lube?"): what the voters in Chaster see is only what he does and what the photo shows. The DM keeps
    the full task (lube, warm-up, how long); Chaster drops those sentences and uses the plugs' short names."""
    import re
    for words in _VIBRATING:
        text = text.replace(words, " in" if words.startswith(" ") else "in")
    for long, short in _SHORT_NAMES.items():
        text = text.replace(long, short)
    text = text.replace("vibrating plug plug", "vibrating plug").replace("plug (vibrating plug)", "plug")
    text = re.sub(r",? with [^.]*lube", "", text)
    kept = []
    for s in re.split(r"(?<=\.)\s+", text.strip()):
        if not s or re.match(r"(then )?(keep it|warm up)", s, re.I):
            continue
        timed = re.match(r"(\d+)\s*(minutes?|hours?)\b(.*)", s, re.I)
        if timed:
            rest = timed.group(3).strip()
            if not rest or rest[0] in ",." or rest.lower().startswith(("then", "at most")):
                continue                                    # "10 minutes, then off slowly.": a duration only
            s = rest[0].upper() + rest[1:]                  # "20 minutes kneeling, ..." -> "Kneeling, ..."
        kept.append(s)
    return " ".join(kept) or text


_ANAL = ("plug", "deep diver", "bijou", "gildo", "rimfire", "riding")


def uses_anal(text: str) -> bool:
    """Owner, 2026-11-16 (A0): the task puts something in his ass (a plug or the Deep Diver, or riding a dildo)."""
    t = text.lower()
    return any(w in t for w in _ANAL)


TOY_OFFER = "Press \"It's in: run it\" once it's in and I'll control it myself; if I can't reach it, run it yourself as written."
TOY_STOPPED = "Stopped."
# Owner, 2026-11-23: a confirmation at the start of each session (with the check buzz).
TOY_STARTED = "Connected to your Deep Diver: you should have felt a short buzz. If you didn't, press Stop and check the toy and Intiface."
# Owner, 2026-11-23: "Pause" / "Continue" on a session the bot runs. The session's time waits, and the photo deadline moves by the time paused,
# never past midnight of the task's day. Pauses are not limited (the time left still has to be done).
TOY_PAUSED = ("Paused: the toy is off and its time waits for you. Your photo deadline moves by however long you pause, midnight at the "
              "latest.")
TOY_PROBLEM = ("I couldn't reach your Deep Diver (Intiface open on the PC, toy on and near it?). Fix that and press Try again (or send "
               "\"run toy\", or \"run toy 45\" for the minutes left), or run it yourself as the task says.")   # live, 2026-11-23: the Try again button (the first button was gone after one press)
TOY_DEFAULT_MINUTES = 20
TOY_ASK = "How was that?"
TOY_ANSWERS = (("toyweak", "Too weak"), ("toyright", "Right"), ("toymuch", "Too much"))


def uses_toy(text: str) -> bool:
    """Owner, 2026-11-17: the tasks the bot can run the vibration for: the ones with his Deep Diver in."""
    return "Deep Diver" in text


def task_minutes(text: str) -> int:
    """Owner, 2026-11-16 (A0): the task's own time (warm-up, holding and wearing times added up), for the photo deadline."""
    import re
    total = 0
    for n, unit in re.findall(r"(\d+)\s*(minutes?|hours?)", text):
        total += int(n) * (60 if unit.startswith("hour") else 1)
    return total


def is_restraint(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in ("rope", "cuff", "tied", "clipped"))


def safety_notes(text: str) -> str:
    """Safety lines for the DM, from what the task names (owner, 2026-11-15: in both modes)."""
    t = text.lower()
    notes = []
    if any(w in t for w in ("rope", "cuff", "tied", "clipped")):
        notes.append("Key or quick release and shears (or scissors) within reach the whole time; never rope on your limbs while blindfolded; "
                     "free yourself at once if anything tingles, goes numb or cold.")
    if "behind your back" in t and "fluffy" in t:
        notes.append("Test the fluffy cuffs' release lever before you put them on behind your back.")
    if "bondage harness" in t:
        notes.append("Clip the cuffs only to the waist rings, never to the collar ring; the velcro rips open any time.")
    if "collar" in t and "bondage harness" not in t:
        notes.append("The collar stays loose enough for two fingers and is never tied, clipped or locked to anything.")
    if "clamp" in t:
        notes.append("Clamps come off after 10 minutes at most, slowly, one at a time.")
    if "leash" in t:
        notes.append("The leash clips only to the harness waist ring, never the collar; the clip opens with one hand and shears are within reach.")
    return " ".join(notes)


@dataclass(frozen=True)
class Plug:
    key: str                                 # also its GEAR key: on the ladder only when he owns it
    name: str
    diameter_cm: float
    lube: str                                # what the plug's material takes


# Sizes from sexshop51.cz product pages and retailer specs (2026-11-14). A rung counts only when its plug is in his gear list.
# Research (2026-11-14): warm up on the size below, short sessions growing slowly, rest days between sessions, water-based lube on silicone
# and PVC, never numbing lube, never overnight, stop at pain or bleeding.
PLUG_LADDER: tuple[Plug, ...] = (
    Plug("deep_diver", "gray Satisfyer Deep Diver", 3.3, "water-based lube"),
    Plug("bijou_large", "clear Crushious Bijou", 4.1, "plenty of lube"),
    Plug("gildo_27", "glass Gildo No. 27", 4.5, "plenty of lube"),
    Plug("plug_joy_medium", "Plug & Joy medium", 5.5, "water-based lube"),
    Plug("rimfire_s", "Wolf Rimfire S", 6.0, "water-based lube"),
)
LADDER_STEP = 3                          # verified wears on a rung before the next one
LADDER_SHARE = 1 / 3                     # of erotic days
LADDER_MINUTES = (20, 30, 45, 60)        # wearing time by verified wears at the current rung


@dataclass(frozen=True, kw_only=True)
class Ports:
    lock_id: Callable[[datetime], str | None]
    halted: Callable[[], bool]
    paused: Callable[[datetime], bool]
    free_time: Callable[[datetime], bool]                  # the moment is in free time (not quiet, not work hours)
    today: Callable[[datetime], date]                      # the wearer's local date
    local_clock: Callable[[datetime], str]
    slot_taken: Callable[[], bool]                         # an open spot check holds Chaster's one task slot
    raise_failure: Callable[[dict, str, datetime], None]   # (row, reason, now): the infractions path
    in_exemption: Callable[[], bool] = lambda: False       # a wear exemption is in force: no tasks (owner, 2026-11-14)
    training: Callable[[date], object] | None = None       # Q4c: training.service.TrainingPlan.day_task (the plan's session, or None)
    training_tick: Callable[[datetime], None] | None = None  # Q4c: the Monday question about the short session
    feature_on: Callable[[str], bool] = lambda feature: True   # owner, 2026-11-16 (features/): "workouts", "erotic_tasks"
    midnight: Callable[[date], datetime] | None = None     # the end of a local day (owner, 2026-11-23: a toy pause moves a deadline up to it)


def get_verified_training(core: CoreDatabase, user_id: str, local_date: str) -> list[str]:
    """Read-only (owner, 2026-11-15: a training session verified in Chaster earns token progress): the catalog keys of that local day's
    training sessions the community verified ("training:upper", "training:short:upper_short", ...)."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT catalog_key FROM obedience_tasks WHERE user_id = ? AND local_date = ? AND status = 'passed' "
                            "AND outcome = 'verified' AND catalog_key LIKE 'training:%'", (user_id, local_date))
    return [r["catalog_key"] for r in rows]


def get_training_verified_since(core: CoreDatabase, user_id: str, since: str) -> bool:
    """Read-only (earn the release, owner 2026-11-15): a training session verified (or credited) at or after `since`."""
    with core.transaction() as tx:
        return tx.fetch_one("SELECT 1 FROM obedience_tasks WHERE user_id = ? AND status = 'passed' AND outcome = 'verified' "
                            "AND catalog_key LIKE 'training:%' AND decided_at >= ?", (user_id, since)) is not None


_STATE_WORDS = {"planned": "planned for later today", "asking": "asked whether he is alone", "assigned": "in Chaster, photo not sent yet",
                "submitted": "photo sent, the community vote is running", "passed": "verified", "failed": "not done or rejected",
                "cancelled": "cancelled, no consequence", "skipped": "skipped, no consequence"}


def get_obedience_today(core: CoreDatabase, user_id: str, local_date: str) -> list[dict]:
    """Read-only (bug fix 2026-11-15): today's tasks and where each stands, in plain words, for the conversation context."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT catalog_key, text, status, outcome FROM obedience_tasks WHERE user_id = ? AND local_date = ? ORDER BY slot",
                            (user_id, local_date))
    out = []
    for r in rows:
        # Bug fix 2026-11-17: "I wanna do a leg day today" got "that's not changing anything, you're going to do it": the short session is
        # optional (Q4c), so the model must know it, and that wanting to do it is not pushback.
        kind = ("optional short training session (his own choice: skipping it costs nothing; wanting to do it deserves encouragement)"
                if r["catalog_key"].startswith("training:short:") else
                "training session" if r["catalog_key"].startswith("training:") else (
            "task-board task" if r["catalog_key"].startswith("board:") else "obedience task"))
        state = _STATE_WORDS.get(r["status"], r["status"])
        if r["status"] == "passed" and r["outcome"] != "verified":
            state = "closed without a verdict, no consequence"
        out.append({"kind": kind, "task": chaster_brief(r["text"] or "") or "not chosen yet", "state": state})
    return out


def get_verified_task_ids_since(core: CoreDatabase, user_id: str, since: str) -> list[str]:
    """Read-only (protocol rung 3, forms of address): ids of tasks verified since an ISO time."""
    with core.transaction() as tx:
        return [r["id"] for r in tx.fetch_all("SELECT id FROM obedience_tasks WHERE user_id = ? AND status = 'passed' AND outcome = 'verified' "
                                              "AND decided_at >= ? AND catalog_key != 'kneel:evening'", (user_id, since))]


def get_surprise_rewards(core: CoreDatabase, user_id: str, lo: str, hi: str):
    """Read-only (research a, owner 2026-11-15): the surprise rewards of tasks verified in [lo, hi), as a Decimal."""
    from decimal import Decimal
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT surprise FROM obedience_tasks WHERE user_id = ? AND surprise IS NOT NULL AND decided_at >= ? AND decided_at < ?",
                            (user_id, lo, hi))
    return sum((Decimal(r["surprise"]) for r in rows), Decimal(0))


def get_training_outcomes(core: CoreDatabase, user_id: str, start: str, end: str) -> dict[str, dict]:
    """Read-only: local date -> {catalog_key, status, outcome} of the training sessions from `start` to `end` (inclusive local dates)."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT local_date, catalog_key, status, outcome FROM obedience_tasks WHERE user_id = ? AND local_date BETWEEN ? AND ? "
                            "AND catalog_key LIKE 'training:%'", (user_id, start, end))
    return {r["local_date"]: {"catalog_key": r["catalog_key"], "status": r["status"], "outcome": r["outcome"]} for r in rows}


def get_health_cancels(core: CoreDatabase, user_id: str, lo: str, hi: str) -> list[str]:
    """Read-only (G2, owner 2026-11-15): when a task was cancelled with "I can't now (health or safety)", in [lo, hi)."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT decided_at FROM obedience_tasks WHERE user_id = ? AND outcome = 'health_or_safety' AND decided_at >= ? AND decided_at < ?",
                            (user_id, lo, hi))
    return [r["decided_at"] for r in rows]


def plan_extra_task(core: CoreDatabase, user_id: str, day: date, planned_at: datetime, now: datetime, *, anytime: bool = False) -> str | None:
    """Owner, 2026-11-15 (migration 077; `python -m cli obedience-extra`): ONE extra erotic task for `day` (slot 1), beside the day's own task.
    It runs like any other: at `planned_at` he is asked "Are you alone right now?", then the task goes to Chaster when its one task slot is free
    (after the day's own task), photo and community review, same consequences. Skipped without consequence if his free time is over first.
    `anytime` (the owner's one-time exception, migration 078): it may start and run outside free time (quiet hours). None when an extra
    task already exists for that day."""
    row_id = str(uuid.uuid4())

    def write(tx: Transaction, _s: object) -> bool:
        done = tx.execute("INSERT OR IGNORE INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, slot, "
                          "anytime) VALUES (?, ?, ?, ?, '', ?, 'planned', ?, 1, ?)",
                          (row_id, user_id, day.isoformat(), ASK_KEY, _iso(planned_at), _iso(now), 1 if anytime else 0)).rowcount
        if done:
            write_event(tx, DomainEvent(event_type="obedience.extra_planned", source_module="obedience", occurred_at=now,
                                        payload={"obedience_task_id": row_id}))
        return bool(done)

    if not apply_transition(core, write=write):
        return None
    decision("obedience", "An extra obedience task was filed by the owner.", day=day.isoformat(), planned_at=_iso(planned_at))
    return row_id


def plan_board_task(core: CoreDatabase, user_id: str, day: date, key: str, text: str, now: datetime) -> str | None:
    """The task board (owner, 2026-11-15): a board task he starts now becomes an obedience task (slot 2 of the day; one a day), run the usual
    way: Chaster task, photo, community vote, the usual consequences. None when today's board slot is already used."""
    row_id = str(uuid.uuid4())

    def write(tx: Transaction, _s: object) -> bool:
        return tx.execute("INSERT OR IGNORE INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, slot) "
                          "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, 2)", (row_id, user_id, day.isoformat(), f"board:{key}", text, _iso(now), _iso(now))).rowcount == 1

    return row_id if apply_transition(core, write=write) else None


KNEEL_KEY = "kneel:evening"
KNEEL_TEXT = "Kneeling for the night. Photo from behind only."
KNEEL_NOTE = "Kneel for 5 minutes, then the photo. It's your protocol's evening duty: none counts as a missed check-in, nothing more."
KNEEL_START_BY = timedelta(minutes=20)    # not in Chaster by then (the task slot stayed taken): skipped, never missed


def plan_kneel_task(core: CoreDatabase, user_id: str, day: date, planned_at: datetime, now: datetime) -> str | None:
    """Protocol rung 6 (owner, 2026-11-15/16; queue A3): the evening kneel as an obedience task in slot 4, outside free time allowed. A miss
    is a missed check-in (the protocol's duty), never an obedience failure. None when today's is already planned."""
    row_id = str(uuid.uuid4())

    def write(tx: Transaction, _s: object) -> bool:
        return tx.execute("INSERT OR IGNORE INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, slot, anytime) "
                          "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, 4, 1)",
                          (row_id, user_id, day.isoformat(), KNEEL_KEY, KNEEL_TEXT, _iso(planned_at), _iso(now))).rowcount == 1

    return row_id if apply_transition(core, write=write) else None


def get_kneel_status(core: CoreDatabase, user_id: str, local_date: str) -> str | None:
    """Read-only (protocol rung 6): today's kneel task status, or None."""
    with core.transaction() as tx:
        row = tx.fetch_one("SELECT status FROM obedience_tasks WHERE user_id = ? AND local_date = ? AND catalog_key = ?", (user_id, local_date, KNEEL_KEY))
    return None if row is None else row["status"]


def get_obedience_state(core: CoreDatabase, row_id: str) -> tuple[str, str | None] | None:
    """Read-only: (status, outcome) of one obedience task."""
    with core.transaction() as tx:
        row = tx.fetch_one("SELECT status, outcome FROM obedience_tasks WHERE id = ?", (row_id,))
    return None if row is None else (row["status"], row["outcome"])


def credit_training(core: CoreDatabase, user_id: str, local_date: str, now: datetime) -> bool:
    """Owner, 2026-11-15 (`python -m cli obedience-credit-training`): a training session a wear exemption cancelled before its photo (the bug
    fixed the same day) counts as verified on the owner's word. Only such a row; True when one was credited."""
    def write(tx: Transaction, _s: object) -> str | None:
        row = tx.fetch_one("SELECT id FROM obedience_tasks WHERE user_id = ? AND local_date = ? AND catalog_key LIKE 'training:%' "
                           "AND status = 'cancelled' AND outcome = 'exemption'", (user_id, local_date))
        if row is None:
            return None
        tx.execute("UPDATE obedience_tasks SET status = 'passed', outcome = 'verified', review_status = 'owner_credit', decided_at = ? WHERE id = ?",
                   (_iso(now), row["id"]))
        write_event(tx, DomainEvent(event_type="obedience.training_credited", source_module="obedience", occurred_at=now,
                                    payload={"obedience_task_id": row["id"]}))
        return row["id"]

    ident = apply_transition(core, write=write)
    if ident:
        decision("obedience", "A training session cancelled by an exemption was credited as verified by the owner.", day=local_date)
    return bool(ident)


class ObedienceTasks:
    def __init__(self, core: CoreDatabase, *, user_id: str, chaster, ports: Ports, rng: random.Random | None = None) -> None:
        self._core = core
        self.user_id = user_id
        self._chaster = chaster
        self._ports = ports
        self.toy = None              # toy_control.session.ToySessions (owner, 2026-11-17): set by the bot for the owner only
        self.toy_levels = None       # toy_control.store.ToyLevels: his own level, moved by his answer after each session
        self._toy_paused: dict[str, datetime] = {}   # row id -> when he pressed Pause (owner, 2026-11-23); a restart ends the session anyway
        self._rng = rng or random.SystemRandom()
        # Owner, 2026-11-16 (multi-wearer): DEFAULT_OWNED is the owner's inventory; an additional wearer starts with nothing and adds gear.
        self.default_owned: frozenset = DEFAULT_OWNED
        self.generic_names = False         # an additional wearer: pieces are listed by ROLE, and their own names replace the owner's items
        # Owner, 2026-11-17: another wearer's erotic tasks are only the ones the owner wrote from their limits and they approved
        # (custom_tasks/); None for the owner, whose catalogue is his own.
        self.custom = None
        self.trust_domain: str | None = None        # owner, 2026-11-17: the check-in domain a verified task earns in (None: it earns nothing)

    # ------------------------------------------------------------------ plan and start

    def tick(self, now: datetime) -> None:
        if self._ports.in_exemption():
            self._cancel_open(now, "exemption")
            self._end_training_in_exemption(now)
            self.poll(now)                         # owner, 2026-11-15: a training session needs no cage, so its proof is still read
            return
        if self._ports.training_tick is not None and self._ports.feature_on("workouts"):
            try:
                self._ports.training_tick(now)
            except Exception:
                logger.exception("The training plan's pass failed; tasks go on.")
        self._plan(now)
        self._prep_notice(now)
        self._start_due(now)
        self.poll(now)

    def _plan(self, now: datetime) -> None:
        day = self._ports.today(now)
        if self._row_for(day) is not None or self._ports.paused(now) or not self._ports.free_time(now):
            return
        moments, at = [], now + PLAN_STEP
        while self._ports.today(at) == day:
            if self._ports.free_time(at) and self._ports.free_time(at + PHOTO_DEADLINE):
                moments.append(at)
            at += PLAN_STEP
        if not moments:
            return
        workouts_on, erotic_on = self._ports.feature_on("workouts"), self._ports.feature_on("erotic_tasks")
        training = self._ports.training(day) if self._ports.training is not None and workouts_on else None
        if training is None and not erotic_on:
            return                                  # owner, 2026-11-16 (features/): erotic tasks off and no training session today
        if training is not None:
            # Work queue Q4c: a training day's task is the plan's session, given at the start of free time so the whole evening is his.
            planned, task, details = moments[0], Task(training.key, training.text, "workout"), training.details
        else:
            # Owner, 2026-11-15: the task itself is chosen only when he answers "Are you alone right now?" (ASK_KEY until then).
            planned, task, details = self._rng.choice(moments), Task(ASK_KEY, "", "erotic"), None
        row_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT OR IGNORE INTO obedience_tasks (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, details) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
                       (row_id, self.user_id, day.isoformat(), task.key, task.text, _iso(planned), _iso(now), details))

        apply_transition(self._core, write=write)
        decision("obedience", "Today's obedience task was planned.", day=day.isoformat(), task=task.key, planned_at=_iso(planned))

    def today_open(self, now: datetime) -> bool:
        """Read-only: today's task is not out yet (none planned, or only planned), so an extra training session may still take its place."""
        row = self._row_for(self._ports.today(now))
        return row is None or row["status"] == "planned"

    def take_training_today(self, now: datetime) -> bool:
        """Bug fix 2026-11-16: "Today" for the short session came after today's task was planned (an erotic one). A task still only planned
        (not asked, not in Chaster) becomes the training session. True when it was switched."""
        day = self._ports.today(now)
        row = self._row_for(day)
        if row is None or row["status"] != "planned" or self._ports.training is None or not self._ports.feature_on("workouts"):
            return False
        training = self._ports.training(day)
        if training is None:
            return False

        def write(tx: Transaction, _s: object) -> bool:
            return bool(tx.execute("UPDATE obedience_tasks SET catalog_key = ?, text = ?, details = ?, planned_at = ? WHERE id = ? AND status = 'planned'",
                                   (training.key, training.text, training.details, _iso(min(now + PLAN_STEP, _parse_iso(row["planned_at"]))),
                                    row["id"])).rowcount)

        switched = bool(apply_transition(self._core, write=write))
        if switched:
            decision("obedience", "Today's planned task became the short training session.", task=training.key)
        return switched

    def _pick(self, day: date, mode: str = ALONE, owned: frozenset | None = None, *, erotic_only: bool = False) -> Task:
        task = self._pick_raw(day, mode, owned, erotic_only=erotic_only)
        return _replace(task, text=self.personalize(task.text)) if task is not None else task

    def labels(self) -> dict[str, str]:
        """The wearer's own names for their pieces (`gear add <piece> as <name>`)."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT gear, label FROM obedience_gear WHERE user_id = ? AND owned = 1 AND label IS NOT NULL AND label <> ''",
                                (self.user_id,))
        return {r["gear"]: r["label"] for r in rows}

    def personalize(self, text: str) -> str:
        """A task text with the wearer's own names for their toys in place of the owner's items (nothing changes without a label)."""
        try:
            labels = self.labels()
        except Exception:
            logger.exception("The gear names could not be read; the catalogue names are used.")
            return text
        for gear, label in labels.items():
            for variant in NAME_VARIANTS.get(gear, ()):
                text = text.replace(variant, label)
        return text

    def _gear_name(self, gear: str, labels: dict[str, str] | None = None) -> str:
        name = (labels or {}).get(gear)
        return name or (ROLE[gear] if self.generic_names else GEAR[gear])

    def _pick_raw(self, day: date, mode: str = ALONE, owned: frozenset | None = None, *, erotic_only: bool = False) -> Task:
        """Workout about one day in four; otherwise the plug ladder (never two days running: a rest day between plug sessions), a composed
        pose task, or one of the extras. Nothing used in the last REPEAT_DAYS days, never yesterday's main gear, only gear he owns, and only
        tasks that suit `mode` (alone: the full versions; discreet: quiet and short)."""
        owned = effective(self.owned_gear() if owned is None else owned)
        if self.custom is not None:
            return self._pick_custom(day, mode, owned)
        since = (day - timedelta(days=REPEAT_DAYS)).isoformat()
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT local_date, catalog_key FROM obedience_tasks WHERE user_id = ? AND local_date >= ? "
                                                  "AND local_date < ?", (self.user_id, since, day.isoformat()))]
        avoided_keys, avoided = self._avoided(day)
        comeback = (day - timedelta(days=AVOID_REPEAT_DAYS)).isoformat()
        # An avoided task may come back after AVOID_REPEAT_DAYS (unless it was given again since and done).
        banned = {r["catalog_key"] for r in rows if not (r["catalog_key"] in avoided_keys and r["local_date"] < comeback)}
        yesterday = (day - timedelta(days=1)).isoformat()
        yesterday_gear = set().union(*(gear_of(r["catalog_key"]) for r in rows if r["local_date"] == yesterday)) if rows else set()
        workouts = [t for t in WORKOUTS if suits(t, owned, mode)]
        workout_share = min(1.0, WORKOUT_SHARE * (AVOID_WEIGHT if "workout" in avoided else 1.0))
        # Owner, 2026-11-16 22:40: the extra erotic task (slot 1) drew "calf raises with the bar"; an extra task is erotic only.
        if not erotic_only and self._rng.random() < workout_share and "workout" not in yesterday_gear and self._ports.feature_on("workouts"):
            workouts = [t for t in workouts if not (gear_of(t.key) - {"workout"}) & yesterday_gear] or workouts
            return self._rng.choice([t for t in workouts if t.key not in banned] or workouts)
        roll = self._rng.random()
        if roll < min(0.5, LADDER_SHARE * (AVOID_WEIGHT if "plug" in avoided else 1.0)) and "plug" not in yesterday_gear:
            plug = self.ladder_task(owned)
            if plug is not None:
                return plug
        verified = self._verified_keys()

        def unlocked(task: Task) -> bool:
            return not task.after or sum(1 for k in verified if k.startswith(task.after[0])) >= task.after[1]

        back_blind_ok = sum(1 for k in verified if "cuffs_back" in k) >= BACK_BLIND_AFTER
        extras = [t for t in EXTRAS if suits(t, owned, mode) and unlocked(t)]
        fresh = [t for t in extras if t.key not in banned and not gear_of(t.key) & yesterday_gear]
        # Composed tasks most days; the fixed extras get a fair share, more of it when new gear unlocked many of them.
        if roll < 0.8 - 0.02 * len(fresh):
            composed = compose(self._rng, banned, yesterday_gear, owned, mode, back_blind_ok=back_blind_ok, avoided=avoided)
            if composed is not None:
                return composed
        if fresh:
            weights = [AVOID_WEIGHT if task_parts(t.key) & avoided else 1.0 for t in fresh]
            return self._rng.choices(fresh, weights=weights)[0] if avoided else self._rng.choice(fresh)
        return (compose(self._rng, banned, yesterday_gear, owned, mode) or compose(self._rng, set(), set(), owned, mode)
                or (self._rng.choice(extras) if erotic_only and extras else self._rng.choice(workouts)))

    def _pick_custom(self, day: date, mode: str, owned: frozenset) -> Task:
        """Another wearer (owner, 2026-11-17): only their approved tasks that suit the mode and their gear, not one used in the last
        REPEAT_DAYS days when another is left, never yesterday's. With none (all rejected later, or the gear for them removed): CUSTOM_FALLBACK,
        which needs nothing and shows only what their spot checks already show."""
        since = (day - timedelta(days=REPEAT_DAYS)).isoformat()
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT local_date, catalog_key FROM obedience_tasks WHERE user_id = ? AND local_date >= ? AND local_date < ?",
                                (self.user_id, since, day.isoformat()))
        recent = {r["catalog_key"] for r in rows}
        yesterday = {r["catalog_key"] for r in rows if r["local_date"] == (day - timedelta(days=1)).isoformat()}
        fits = [t for t in self.custom.approved() if mode in t.modes and t.needs <= owned and t.key not in yesterday]
        if not fits:
            return CUSTOM_FALLBACK
        task = self._rng.choice([t for t in fits if t.key not in recent] or fits)
        return Task(task.key, task.text, "erotic", task.needs, task.modes)

    def _avoided(self, day: date) -> tuple[set[str], frozenset]:
        """(task keys, their parts) he avoided in the AVOID_DAYS before `day`: postponed ("someone's around") or failed. Never the health or
        safety button, an exemption, a missing free slot or a vote that never ended."""
        since = (day - timedelta(days=AVOID_DAYS)).isoformat()
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT catalog_key FROM obedience_tasks WHERE user_id = ? AND local_date >= ? AND local_date < ? "
                                "AND (status = 'failed' OR (postponed_at IS NOT NULL AND COALESCE(outcome, '') <> 'health_or_safety'))",
                                (self.user_id, since, day.isoformat()))
        keys = {r["catalog_key"] for r in rows if r["catalog_key"] and r["catalog_key"] != ASK_KEY}
        return keys, frozenset(set().union(*(task_parts(k) for k in keys)) if keys else set())

    def _verified_keys(self) -> list[str]:
        with self._core.transaction() as tx:
            return [r["catalog_key"] for r in tx.fetch_all("SELECT catalog_key FROM obedience_tasks WHERE user_id = ? AND status = 'passed' "
                                                           "AND outcome = 'verified'", (self.user_id,))]

    def ladder_task(self, owned: frozenset | None = None) -> Task | None:
        """The next plug task on the ladder: the current rung's plug, a warm-up on the size below, the right lube, and a wearing time that grows
        with verified wears there. A plug task cancelled for health or safety sends the next one a rung back down. Only plugs in his gear list
        count; None when he has none."""
        owned = self.owned_gear() if owned is None else owned
        with self._core.transaction() as tx:
            passed = {r["catalog_key"]: r["n"] for r in tx.fetch_all(
                "SELECT catalog_key, COUNT(*) AS n FROM obedience_tasks WHERE user_id = ? AND status = 'passed' AND outcome = 'verified' "
                "AND catalog_key LIKE 'plug:%' GROUP BY catalog_key", (self.user_id,))}
            last = tx.fetch_one("SELECT catalog_key, outcome FROM obedience_tasks WHERE user_id = ? AND catalog_key LIKE 'plug:%' AND decided_at IS NOT NULL "
                                "ORDER BY decided_at DESC LIMIT 1", (self.user_id,))
        owned = [p for p in PLUG_LADDER if p.key in owned]
        if not owned:
            return None
        index = 0
        for i, plug in enumerate(owned):
            index = i
            if passed.get(f"plug:{plug.key}", 0) < LADDER_STEP:
                break
        if last is not None and last["outcome"] == "health_or_safety" and last["catalog_key"] == f"plug:{owned[index].key}" and index > 0:
            index -= 1                                               # it hurt last time: one rung back
        rung = owned[index]
        wears = passed.get(f"plug:{rung.key}", 0)
        minutes = LADDER_MINUTES[min(wears, len(LADDER_MINUTES) - 1)]
        warm = f" Warm up with the {owned[index - 1].name} for 10 minutes first." if index > 0 else ""
        return Task(f"plug:{rung.key}", f"Your {rung.name} in, with {rung.lube}.{warm} Photo of the plug base only. "
                                         f"Keep it in {minutes} minutes.", "erotic")

    def _cancel_open(self, now: datetime, outcome: str) -> None:
        with self._core.transaction() as tx:
            # Owner, 2026-11-15: a wear exemption never cancels a training session (it needs no cage; one was lost that way).
            # Owner, 2026-11-17 00:00: a health exemption at 23:57 cancelled a task whose photo was in since 23:33. A task with its photo in
            # is done; the vote decides it, an exemption never cancels it.
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM obedience_tasks WHERE user_id = ? AND status IN ('asking', 'assigned') "
                                                  "AND catalog_key NOT LIKE 'training:%'", (self.user_id,))]
        for row in rows:
            try:
                current = self._chaster.current_task(row["lock_id"]) if row["task_id"] and not self._ports.halted() else None
                if current is not None and current.get("id") == row["task_id"]:
                    self._chaster.unassign(row["lock_id"])
            except Exception:
                logger.exception("An obedience task could not be taken back in Chaster.")
            if self._close(row, "cancelled", outcome, now, text="Your task is cancelled: a wear exemption is in force. No consequence."):
                decision("obedience", "An obedience task was cancelled by a wear exemption; no consequence.", task=row["catalog_key"])

    def _end_training_in_exemption(self, now: datetime) -> None:
        """Owner, 2026-11-15: an exemption leaves a training session due; back before the end of free time, he still does it. Only when the
        exemption still holds as its deadline (the end of the day's free time) passes is it cancelled, quietly and without consequence. A photo
        already submitted is left to the vote."""
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM obedience_tasks WHERE user_id = ? AND status = 'assigned' AND catalog_key LIKE 'training:%' "
                                                  "AND deadline <= ?", (self.user_id, _iso(now)))]
        for row in rows:
            try:
                current = self._chaster.current_task(row["lock_id"]) if row["task_id"] and not self._ports.halted() else None
                if current is not None and current.get("id") == row["task_id"]:
                    self._chaster.unassign(row["lock_id"])
            except Exception:
                logger.exception("A training task could not be taken back in Chaster.")
            if self._close(row, "cancelled", "exemption", now):
                decision("obedience", "A training session ran out during a wear exemption: cancelled quietly, no consequence.", task=row["catalog_key"])

    def _prep_notice(self, now: datetime) -> None:
        """PREP_AHEAD before an erotic task its two versions are chosen (owner, 2026-11-16, A0), and the clean-up heads-up goes out only when
        one of them goes in his ass. Once per task; a training session gets none. Never a consequence."""
        import json
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT id, local_date, slot FROM obedience_tasks WHERE user_id = ? AND status = 'planned' AND catalog_key = ? "
                                                  "AND prepared IS NULL AND planned_at <= ? AND planned_at > ?",
                                                  (self.user_id, ASK_KEY, _iso(now + PREP_AHEAD), _iso(now)))]
        # Bug fix 2026-11-18 (live): the heads-up went out with no lock (the day after a release), for a task that could never be put in Chaster.
        # Without a lock, or while halted, it waits; the task itself waits the same way and is skipped without consequence when free time ends.
        if rows and (self._ports.halted() or self._ports.lock_id(now) is None):
            return
        for row in rows:
            day = date.fromisoformat(row["local_date"])
            picks = {m: (self._pick(day, m, erotic_only=True) if row.get("slot") == 1 else self._pick(day, m)) for m in (ALONE, DISCREET)}
            anal = [m for m, t in picks.items() if uses_anal(t.text)]
            stored = json.dumps({m: {"key": t.key, "text": t.text, "note": t.dm_note} for m, t in picks.items()})

            def write(tx: Transaction, _s: object, r=row) -> None:
                if tx.execute("UPDATE obedience_tasks SET prepared = ? WHERE id = ? AND prepared IS NULL", (stored, r["id"])).rowcount and anal:
                    enqueue_in_transaction(tx, user_id=self.user_id, kind="obedience_task", dedupe_key=f"obedience:prep:{r['id']}", now=now,
                                           text=PREP_TEXT.format(certainty="uses" if len(anal) == 2 else "may use"))

            apply_transition(self._core, write=write)
            decision("obedience", "Today's task versions chosen at the heads-up.", alone=picks[ALONE].key, discreet=picks[DISCREET].key,
                     heads_up=bool(anal))

    def _start_due(self, now: datetime) -> None:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM obedience_tasks WHERE user_id = ? AND status IN ('planned', 'asking') AND planned_at <= ?",
                                                  (self.user_id, _iso(now)))]
        for row in rows:
            try:
                self._start(row, now)
            except Exception:
                logger.exception("An obedience task could not start; it is retried.")

    def _start(self, row: dict, now: datetime) -> bool:
        """True when the task is in Chaster now."""
        anytime = bool(row.get("anytime"))                              # the owner's one-time exception to free time (migration 078)
        # Owner, 2026-11-16 23:00: no timer on "Are you alone right now?". Once asked, he may answer (and do the task) even in quiet hours;
        # still unanswered when the day ends = a missed task.
        asked = row["catalog_key"] == ASK_KEY and row["status"] == "asking"
        if asked and row.get("mode") is None and self._ports.today(now).isoformat() != row["local_date"] and not self._ports.paused(now):
            if self._close(row, "failed", "unanswered", now):
                self._ports.raise_failure(row, "unanswered", now)
                decision("obedience", "The alone question was still unanswered at midnight: a missed task.", day=row["local_date"])
            return False
        if (self._ports.today(now).isoformat() != row["local_date"] or (not anytime and not asked and not self._ports.free_time(now + PHOTO_DEADLINE))
                or self._ports.paused(now)):
            self._close(row, "skipped", "no_free_slot", now)            # the free time is over: no task today, no consequence
            return False
        if row["catalog_key"] == KNEEL_KEY and now > _parse_iso(row["planned_at"]) + KNEEL_START_BY:
            self._close(row, "skipped", "no_free_slot", now)            # rung 6: the slot stayed taken, so the kneel is skipped, never missed
            return False
        lock_id = self._ports.lock_id(now)
        if lock_id is None or self._ports.halted() or self._ports.slot_taken() or self._chaster.current_task(lock_id) is not None:
            return False                                                # tried again next pass
        key, text, mode, note = row["catalog_key"], row["text"], row.get("mode"), (KNEEL_NOTE if row["catalog_key"] == KNEEL_KEY else "")
        if key == ASK_KEY:
            if row["status"] == "planned":
                self._ask(row, now)
                return False
            if mode is None:
                return False                                            # waits for his answer (no timer since 2026-11-16)
            prepared = self._prepared(row, mode)
            task = prepared or (self._pick(self._ports.today(now), mode, erotic_only=True) if row.get("slot") == 1
                                else self._pick(self._ports.today(now), mode))
            key, text, note = task.key, task.text, task.dm_note
        task_id = self._chaster.assign(lock_id, (text if row.get("details") else chaster_brief(text))[:200])   # training texts are brief already
        deadline = now + PHOTO_DEADLINE + timedelta(minutes=task_minutes(text))   # owner, 2026-11-16 (A0): the task's own time + 1 hour
        if row.get("details"):                                          # a training session: the log photo is due by the end of free time
            at = now + PHOTO_DEADLINE
            while self._ports.today(at + PLAN_STEP).isoformat() == row["local_date"] and self._ports.free_time(at + PLAN_STEP):
                at += PLAN_STEP
            deadline = at
        full = False
        if row.get("details"):
            message = (f"Today's training:\n{row['details']}\nWhen you're done, in Chaster: {text}\nDue by {self._ports.local_clock(deadline)}. "
                       f"{SAFETY}")
            actions = (ActionSpec(action_id=f"obedience:cannot:{row['id']}", label="I can't now (health or safety)"),)
        else:
            version = {ALONE: "the full version, you're alone", DISCREET: "a discreet version"}.get(mode, "")
            head = f"Your task for today is in Chaster now ({version}):" if version else "Your task for today is in Chaster now:"
            # Owner, 2026-11-16 (A0b): the standing reminders in full at most once a week; a restraint task always keeps one short line.
            full = self._reminder_due(now)
            extra = safety_notes(text) if full else (RESTRAINT_SHORT if is_restraint(text) else "")
            if PHOTO_BEHIND in text:
                note = f"{note} {PHOTO_HOW}".strip()
            message = (f"{head}\n{text}\n" + (f"Only between us: {note}\n" if note else "")
                       + f"Photo with Chaster's code by {self._ports.local_clock(deadline)}." + ((f" {SAFETY}" if key.startswith("workout_") else f" {DISCRETION} {SAFETY}") if full else "")
                       + (f" {extra}" if extra else ""))
            actions = (ActionSpec(action_id=f"obedience:later:{row['id']}", label="Someone's around (+2 h, once)"),
                       ActionSpec(action_id=f"obedience:cannot:{row['id']}", label="I can't now (health or safety)"))
            if self.toy is not None and uses_toy(text):            # owner, 2026-11-17: the bot runs the Deep Diver once he says it's in
                message += f" {TOY_OFFER}"
                actions = (ActionSpec(action_id=f"obedience:toy:{row['id']}", label="It's in: run it"),) + actions

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET status = 'assigned', lock_id = ?, task_id = ?, deadline = ?, catalog_key = ?, text = ?, mode = ? "
                              "WHERE id = ? AND status IN ('planned', 'asking')",
                              (lock_id, task_id, _iso(deadline), key, text, mode, row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.task_assigned", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"], "catalog_key": key, "mode": mode}))
                if not row.get("details") and full:
                    from reminders.service import mark_in_transaction
                    mark_in_transaction(tx, self.user_id, "obedience_safety", now)
                enqueue_in_transaction(tx, user_id=self.user_id, kind="obedience_task", dedupe_key=f"obedience:task:{row['id']}" + (":full" if '"upgraded"' in (row.get("prepared") or "") else ""), now=now, immediate=True,
                                       text=message, actions=actions)
            return bool(done)

        if apply_transition(self._core, write=write):
            decision("obedience", "Today's obedience task was assigned in Chaster.", task=key, mode=mode, deadline=_iso(deadline))
            return True
        return False

    def _reminder_due(self, now: datetime) -> bool:
        from reminders.service import due_in_transaction
        try:
            with self._core.transaction() as tx:
                return due_in_transaction(tx, self.user_id, "obedience_safety", now)
        except Exception:
            logger.exception("Could not read when the safety reminder was last shown; it is shown.")
            return True

    @staticmethod
    def _prepared(row: dict, mode: str) -> Task | None:
        """The version chosen at the heads-up for this answer (A0), or None (chosen now, as before)."""
        import json
        try:
            got = json.loads(row["prepared"]).get(mode) if row.get("prepared") else None
        except (ValueError, TypeError):
            return None
        return Task(got["key"], got["text"], "erotic", dm_note=got.get("note") or "") if got else None

    def _ask(self, row: dict, now: datetime) -> None:
        """Owner, 2026-11-15: before the task, "Are you alone right now?". Alone = the full version, not alone = a discreet one; both mandatory."""
        text = (f"Your task for today is ready. Are you alone right now? Alone gets the full version, not alone a discreet one; both are "
                f"mandatory. Answer when you can, even in quiet hours; unanswered at midnight counts as a missed task.")
        actions = (ActionSpec(action_id=f"obedience:alone:{row['id']}", label="I'm alone"),
                   ActionSpec(action_id=f"obedience:discreet:{row['id']}", label="Not alone"),
                   ActionSpec(action_id=f"obedience:cannot:{row['id']}", label="I can't now (health or safety)"))

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET status = 'asking', asked_at = ? WHERE id = ? AND status = 'planned'",
                              (_iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.alone_asked", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"]}))
                enqueue_in_transaction(tx, user_id=self.user_id, kind="obedience_task", dedupe_key=f"obedience:ask:{row['id']}", now=now, immediate=True,
                                       text=text, actions=actions)
            return bool(done)

        if apply_transition(self._core, write=write):
            decision("obedience", "The wearer was asked whether he is alone before today's task.", day=row["local_date"])

    def _answer(self, row: dict, mode: str, now: datetime) -> str:
        if mode == ALONE and row["status"] == "assigned" and row.get("mode") == DISCREET and not row.get("details"):
            return self._upgrade(row, now)
        if row["status"] != "asking" or row.get("mode"):
            return "Already answered."

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET mode = ? WHERE id = ? AND status = 'asking' AND mode IS NULL", (mode, row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.alone_answered", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"], "mode": mode}))
            return bool(done)

        if not apply_transition(self._core, write=write):
            return "Already answered."
        decision("obedience", "The wearer answered the alone question.", mode=mode)
        started = False
        try:
            started = self._start(self._row(row["id"]), now)
        except Exception:
            logger.exception("The obedience task could not start after the answer; it is retried.")
        head = "Alone: the full version." if mode == ALONE else "Not alone: a discreet version."
        return f"{head} It's in Chaster now." if started else f"{head} It comes as soon as Chaster's task slot is free."

    def _upgrade(self, row: dict, now: datetime) -> str:
        """Owner, 2026-11-16 22:40: the discreet version went out because the alone question went unanswered for 15 minutes; a late "I'm alone"
        swaps it for the full version while no photo has been sent. The discreet task is taken back in Chaster and the full one goes out with
        its own fresh deadline. Never the other way round (alone -> discreet stays as answered)."""
        if self._ports.halted():
            return "Already answered."
        try:
            current = self._chaster.current_task(row["lock_id"]) if row["task_id"] else None
            if current is None or current.get("id") != row["task_id"]:
                return "Already answered."                              # a photo is in, or Chaster no longer shows it: nothing to swap
            self._chaster.unassign(row["lock_id"])
        except Exception:
            logger.exception("The discreet task could not be taken back in Chaster; no swap.")
            return "I couldn't swap it right now. Do the discreet version."

        import json
        try:
            marked = json.dumps({**json.loads(row["prepared"]), "upgraded": True}) if row.get("prepared") else json.dumps({"upgraded": True})
        except (ValueError, TypeError):
            marked = json.dumps({"upgraded": True})

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET status = 'asking', mode = ?, catalog_key = ?, task_id = NULL, deadline = NULL, prepared = ? "
                              "WHERE id = ? AND status = 'assigned' AND mode = ?", (ALONE, ASK_KEY, marked, row["id"], DISCREET)).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.alone_answered", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"], "mode": ALONE, "late": True}))
            return bool(done)

        if not apply_transition(self._core, write=write):
            return "Already answered."
        decision("obedience", "A late 'I'm alone' swapped the discreet task for the full version.", task=row["catalog_key"])
        started = False
        try:
            started = self._start(self._row(row["id"]), now)
        except Exception:
            logger.exception("The full version could not start after the swap; it is retried.")
        return ("Alone: the full version instead. It's in Chaster now." if started
                else "Alone: the full version instead. It comes as soon as Chaster's task slot is free.")

    # ------------------------------------------------------------------ gear list (owner, 2026-11-15)

    def owned_gear(self) -> frozenset:
        """His gear: DEFAULT_OWNED, changed by his own `gear add` / `gear remove`."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT gear, owned FROM obedience_gear WHERE user_id = ?", (self.user_id,))
        owned = set(self.default_owned)
        for r in rows:
            (owned.add if r["owned"] else owned.discard)(r["gear"])
        return frozenset(owned & ALL_GEAR)

    # Owner, 2026-11-16: "gear remove shouldn't work instantly". `remove_wait(user_id, piece, now)` returns the reply when the removal
    # waits (opt_outs, while locked) or None to apply it at once; `add_hook(user_id, piece, now)` withdraws a waiting removal.
    remove_wait: Callable[[str, str, datetime], str | None] | None = None
    add_hook: Callable[[str, str, datetime], object] | None = None
    _AT_ONCE = ("broken", "broke", "lost", "never_had", "never_owned", "dont_have", "don't_have", "not_mine")

    def extra_task_command(self, user_id: str, text: str, now: datetime) -> str | None:
        """`extra task` or `extra task at 21:00` (owner, 2026-11-16: "I don't have a workout today, so I feel it's only right I have a proper
        erotic task"): ONE extra erotic task today (slot 1), run like any other (alone question, Chaster, photo, vote, the usual
        consequences). Only in free time, with the photo hour still inside it; never during an exemption, a pause or with erotic tasks off.
        None when the message is not this request."""
        said = " ".join((text or "").lower().split()).strip(" .!?")
        match = _EXTRA_TASK.fullmatch(said)
        if user_id != self.user_id or match is None:
            return None
        if not self._ports.feature_on("erotic_tasks"):
            return "Erotic tasks are switched off for you."
        if self._ports.in_exemption() or self._ports.paused(now):
            return "Not now: tasks are paused (an exemption or a vacation)."
        day = self._ports.today(now)
        at = now + timedelta(minutes=5)
        if match.group("at"):
            wanted = match.group("at").zfill(5)
            probe = now
            while self._ports.today(probe) == day and self._ports.local_clock(probe) != wanted:
                probe += timedelta(minutes=1)
            if self._ports.today(probe) != day:
                return f"{wanted} has already passed today."
            at = max(at, probe)
        if not (self._ports.free_time(at) and self._ports.free_time(at + PHOTO_DEADLINE)):
            return "That's outside your free time (the photo hour has to fit in it too). Pick an earlier time."
        if plan_extra_task(self._core, self.user_id, day, at, now) is None:
            return "You've already had your extra task today. One a day."
        return (f"Good. An extra task at {self._ports.local_clock(at)}: I'll ask whether you're alone, then it goes to Chaster. "
                "It counts like any task: a photo, the vote, and the usual consequences if it isn't done.")

    def toy_command(self, user_id: str, text: str, now: datetime):
        """`run toy [minutes]`: the bot runs the Deep Diver for his assigned Deep Diver task. None when the message is not this request."""
        said = " ".join((text or "").lower().split()).strip(" .!?")
        match = _RUN_TOY.fullmatch(said)
        if user_id != self.user_id or match is None:
            return None
        if self.toy is None:
            return "I can't run the toy for you."
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM obedience_tasks WHERE user_id = ? AND status = 'assigned' "
                                                  "ORDER BY created_at DESC", (self.user_id,))]
        row = next((r for r in rows if uses_toy(r["text"] or "")), None)
        if row is None:
            return "There's no Deep Diver task to run right now."
        minutes = int(match.group("min")) if match.group("min") else None
        if minutes is not None and minutes < 1:
            return "Give me at least one minute."
        return self._run_toy(row, now, minutes=minutes)

    def gear_command(self, user_id: str, text: str, now: datetime) -> str | None:
        """`gear`, `gear add <piece>`, `gear remove <piece>` (`... broken` / `lost` / `never had`: at once); None for any other message."""
        words = (text or "").strip().lower().split()
        if not words or words[0] != "gear" or user_id != self.user_id:
            return None
        if len(words) == 1:
            return self._gear_list()
        if words[1] not in ("add", "remove") or len(words) < 3:
            return "Use `gear`, `gear add <piece>` or `gear remove <piece>`."
        rest, label = words[2:], None
        if words[1] == "add" and "as" in rest[1:]:
            cut = rest.index("as", 1)
            label = " ".join(text.strip().split()[2 + cut + 1:]).strip()[:60] or None     # their own words, case kept
            rest = rest[:cut]
        name = "_".join(rest).replace("-", "_").replace("&", "and").replace("\u2019", "'")
        reason = next((r for r in self._AT_ONCE if name.endswith("_" + r)), None) if words[1] == "remove" else None
        if reason is not None:
            name = name[: -len(reason) - 1]
        gear = name if name in GEAR else GEAR_ALIASES.get(name)
        if gear is None:
            return f"I don't know that piece. Pieces: {', '.join(sorted(ROLE.values() if self.generic_names else GEAR))}."
        owned = 1 if words[1] == "add" else 0
        if owned and self.add_hook is not None:
            try:
                self.add_hook(user_id, gear, now)
            except Exception:
                logger.exception("A waiting gear removal could not be withdrawn; the piece is added anyway.")
        if not owned and reason is None and gear in self.owned_gear() and self.remove_wait is not None:
            try:
                waiting = self.remove_wait(user_id, gear, now)
            except Exception:
                logger.exception("Could not read whether the removal waits; it waits (never a loophole by accident).")
                waiting = "That can't be changed right now. Try again later."
            if waiting is not None:
                return waiting
        return self.set_gear(gear, owned, now, reason=reason, label=label)

    def set_gear(self, gear: str, owned: int, now: datetime, *, reason: str | None = None, label: str | None = None) -> str:
        """The change itself (also opt_outs' "Yes, remove it")."""

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO obedience_gear (user_id, gear, owned, changed_at, label) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT (user_id, gear) DO UPDATE SET owned = excluded.owned, changed_at = excluded.changed_at, "
                       "label = COALESCE(excluded.label, label)", (self.user_id, gear, owned, _iso(now), label))
            write_event(tx, DomainEvent(event_type="obedience.gear_changed", source_module="obedience", occurred_at=now,
                                        payload={"user_id": self.user_id, "gear": gear, "owned": bool(owned), "reason": reason}))

        apply_transition(self._core, write=write)
        decision("obedience", "The wearer changed his gear list.", gear=gear, owned=bool(owned), reason=reason)
        name = label or self._gear_name(gear, self.labels())
        if owned:
            return f"Added: {name}. Tasks that need it can come now."
        return f"Removed: {name}. Tasks that need it wait until you add it back."

    def _gear_list(self) -> str:
        owned, labels = self.owned_gear(), self.labels()
        have = ", ".join(self._gear_name(g, labels) for g in GEAR if g in owned) or "nothing"
        missing = ", ".join(self._gear_name(g) for g in GEAR if g not in owned) or "nothing"
        how = "Change it with `gear add <piece>` or `gear remove <piece>`."
        if self.generic_names:
            how = "Change it with `gear add <piece>` (or `gear add <piece> as <your name for it>`) or `gear remove <piece>`."
        return f"Your gear: {have}.\nTasks waiting for: {missing}.\n{how}"

    # ------------------------------------------------------------------ poll

    def poll(self, now: datetime) -> int:
        with self._core.transaction() as tx:
            rows = [dict(r) for r in tx.fetch_all("SELECT * FROM obedience_tasks WHERE user_id = ? AND status IN ('assigned', 'submitted')",
                                                  (self.user_id,))]
        decided = 0
        for row in rows:
            try:
                decided += self._poll_one(row, now)
            except Exception:
                logger.exception("Polling an obedience task failed; it is retried.")
        return decided

    def _poll_one(self, row: dict, now: datetime) -> int:
        deadline = _parse_iso(row["deadline"])
        entry = self._chaster.task_entry(row["lock_id"], row["task_id"])
        if entry is None or entry.get("status") == "removed":
            if now <= deadline + CLOCK_SKEW:
                return 0
            try:
                current = self._chaster.current_task(row["lock_id"])
                if current is not None and current.get("id") == row["task_id"] and not self._ports.halted():
                    self._chaster.unassign(row["lock_id"])
            except Exception:
                logger.exception("A missed obedience task could not be taken back; it is still decided.")
            return self._fail(row, "missed", now)
        submitted = _parse_iso(entry["createdAt"]) if isinstance(entry.get("createdAt"), str) else None
        if submitted is not None and submitted > deadline + CLOCK_SKEW:
            return self._fail(row, "late", now)
        review = entry.get("peerVerification") if isinstance(entry.get("peerVerification"), dict) else {}
        status = review.get("status")
        if row["status"] == "assigned" or row["review_status"] != status:
            self._update(row["id"], status="submitted", submitted_at=_iso(submitted) if submitted else None, review_status=status)
        if status == "verified":
            # Training and board tasks pay their own rewards, so they never draw a surprise.
            surprise = (not row["catalog_key"].startswith(("training:", "board:", "kneel:"))) and self._rng.random() < SURPRISE_SHARE
            text = ("Your task was verified. Good. And a surprise: +" + SURPRISE_REWARD + " toward your next token."
                    if surprise else "Your task was verified. Good.")
            if self._close(row, "passed", "verified", now, text=text, surprise=SURPRISE_REWARD if surprise else None) and surprise:
                decision("obedience", "Surprise reward for a verified task.", task=row["catalog_key"])
            return 1
        if status == "rejected":
            return self._fail(row, "rejected", now)
        if submitted is not None and now > submitted + VOTE_LIMIT:
            self._close(row, "passed", "unknown", now)                  # a vote that never ends never counts against him
            return 1
        return 0

    def _fail(self, row: dict, reason: str, now: datetime) -> int:
        if row["catalog_key"] == KNEEL_KEY:                              # rung 6: the protocol's duty decides it (a missed check-in), never an incident
            self._close(row, "failed", reason, now)
            return 1
        if row["catalog_key"].startswith("training:short:"):             # Q4c: the short session is optional, missing it costs nothing
            if self._close(row, "skipped", reason, now):
                decision("obedience", "The optional short training session was skipped; no consequence.", task=row["catalog_key"])
            return 1
        if self._close(row, "failed", reason, now):
            self._ports.raise_failure(row, reason, now)
            decision("obedience", "An obedience task failed; the wearer is asked to explain.", task=row["catalog_key"], reason=reason)
        return 1

    # ------------------------------------------------------------------ the wearer's button

    def press(self, *, user_id: str, row_id: str, action: str, now: datetime) -> str:
        row = self._row(row_id)
        if row is None or row["user_id"] != user_id:
            return "That isn't yours to decide."
        if action == "toystop":
            stopped = self.toy is not None and self.toy.stop()
            moved = self._toy_unpause(row, now)                 # a pause that ends with Stop still moves the deadline
            return (TOY_STOPPED + moved) if stopped else "Nothing is running."
        if action == "toypause":
            if self.toy is None or not self.toy.pause():
                return "Nothing is running."
            self._toy_paused[row["id"]] = now
            decision("obedience", "The wearer paused the toy.", task=row["catalog_key"])
            return OutgoingMessage(text=TOY_PAUSED, actions=self._toy_buttons(row["id"], paused=True))
        if action == "toygo":
            if self.toy is None or not self.toy.resume():
                return "Nothing is paused."
            moved = self._toy_unpause(row, now)
            decision("obedience", "The wearer continued the toy.", task=row["catalog_key"])
            return OutgoingMessage(text="Running again." + moved, actions=self._toy_buttons(row["id"]))
        if action in ("toyweak", "toyright", "toymuch"):
            return self._toy_feedback(row, action, now)
        if action not in ("cannot", "later", "alone", "discreet", "toy"):
            return "That isn't one of the options."
        if row["status"] not in ("planned", "asking", "assigned", "submitted"):
            return "That task is already decided."
        if action == "toy":
            return self._run_toy(row, now)
        if action == "later":
            return self._postpone(row, now)
        if action in ("alone", "discreet"):
            return self._answer(row, ALONE if action == "alone" else DISCREET, now)
        try:
            if row["task_id"] and not self._ports.halted():
                current = self._chaster.current_task(row["lock_id"])
                if current is not None and current.get("id") == row["task_id"]:
                    self._chaster.unassign(row["lock_id"])
        except Exception:
            logger.exception("A cancelled obedience task could not be taken back in Chaster.")
        self._close(row, "cancelled", "health_or_safety", now)
        decision("obedience", "An obedience task was cancelled for health or safety; no consequence.", task=row["catalog_key"])
        return "Cancelled, no consequence. Look after yourself first."

    def _run_toy(self, row: dict, now: datetime, *, minutes: int | None = None):
        """Owner, 2026-11-17: "It's in: run it" on an assigned task that uses the Deep Diver. A session shaped for the task, built around his
        own level (`self.toy_levels`), and afterwards "Too weak / Right / Too much" moves that level."""
        if self.toy is None or row["status"] != "assigned" or not uses_toy(row["text"] or ""):
            return "There's nothing to run for this task."
        from toy_control.session import MAX_MINUTES, profile_for
        full = min(task_minutes(row["text"] or "") or TOY_DEFAULT_MINUTES, MAX_MINUTES)
        minutes = full if minutes is None else min(minutes, full)      # `run toy 45`: what is left of the task, never more than all of it
        mode = row["mode"] or DISCREET
        profile = profile_for(row["catalog_key"] or "", mode, minutes)
        levels = self.toy_levels
        level = levels.level() if levels is not None else 0.5
        user_id, row_id = self.user_id, row["id"]

        def tell(key: str, text: str, actions=()) -> None:
            def write(tx: Transaction, _s: object) -> None:
                enqueue_in_transaction(tx, user_id=user_id, kind="obedience_task", dedupe_key=f"obedience:{key}:{row_id}:{_iso(now)}",
                                       now=now, immediate=True, text=text, actions=actions)
            apply_transition(self._core, write=write)

        def done() -> None:
            if levels is not None:
                tell("toy_done", TOY_ASK, tuple(ActionSpec(action_id=f"obedience:{a}:{row_id}", label=label) for a, label in TOY_ANSWERS))

        if not self.toy.start(minutes, mode, profile=profile, base=level, on_problem=lambda text: tell("toy_problem", TOY_PROBLEM, (ActionSpec(action_id=f"obedience:toy:{row_id}", label="Try again"),)),
                              on_done=done, on_started=lambda: tell("toy_started", TOY_STARTED, self._toy_buttons(row_id))):
            return OutgoingMessage(text="It's already running.", actions=self._toy_buttons(row_id, paused=bool(getattr(self.toy, "paused", lambda: False)())))
        if levels is not None:
            levels.record_start(task_ref=row_id, mode=mode, profile=profile, level=level, now=now)
        decision("obedience", "The bot is running the toy for a task.", task=row["catalog_key"], minutes=minutes, mode=mode, profile=profile)
        return OutgoingMessage(text=f"Running it for {minutes} minutes; I decide how strong and when. Your task goes on as written. "
                               "Pause or Stop if you need to; the toy's own button always works too.",
                               actions=self._toy_buttons(row_id))

    @staticmethod
    def _toy_buttons(row_id: str, *, paused: bool = False) -> tuple:
        first = ActionSpec(action_id=f"obedience:toygo:{row_id}", label="Continue") if paused else \
            ActionSpec(action_id=f"obedience:toypause:{row_id}", label="Pause")
        return (first, ActionSpec(action_id=f"obedience:toystop:{row_id}", label="Stop"))

    def _toy_unpause(self, row: dict, now: datetime) -> str:
        """Owner, 2026-11-23: the photo deadline moves by the time paused, never past midnight of the task's day. Returns the sentence that
        tells him the new deadline, or "" when nothing moved."""
        since = self._toy_paused.pop(row["id"], None)
        fresh = self._row(row["id"])
        if since is None or fresh is None or fresh["status"] != "assigned" or not fresh["deadline"] or now <= since:
            return ""
        old = _parse_iso(fresh["deadline"])
        day = date.fromisoformat(fresh["local_date"])
        end = (self._ports.midnight(day) if self._ports.midnight is not None
               else datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=timezone.utc))
        deadline = min(old + (now - since), max(old, end))
        if deadline <= old:
            return ""

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET deadline = ? WHERE id = ? AND status = 'assigned' AND deadline = ?",
                              (_iso(deadline), row["id"], fresh["deadline"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.task_deadline_moved", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"], "deadline": _iso(deadline), "reason": "toy_paused"}))
            return bool(done)

        if not apply_transition(self._core, write=write):
            return ""
        decision("obedience", "A toy pause moved the photo deadline.", task=row["catalog_key"], deadline=_iso(deadline))
        return f" Photo now due by {self._ports.local_clock(deadline)}."

    def _toy_feedback(self, row: dict, action: str, now: datetime) -> str:
        from toy_control.store import FEEDBACK
        if self.toy_levels is None or self.toy_levels.feedback(task_ref=row["id"], answer=FEEDBACK[action], now=now) is None:
            return "Already noted."
        return {"toyweak": "Noted: stronger next time.", "toyright": "Noted. I'll keep it about there and keep nudging it up.",
                "toymuch": "Noted: gentler next time."}[action]

    def _postpone(self, row: dict, now: datetime) -> str:
        """Once per task: the photo deadline moves to POSTPONE from now. A second press, or one after a photo was sent, changes nothing."""
        if row["postponed_at"]:
            return "You already postponed this one. Do it now, or it counts as missed."
        if row["status"] != "assigned" or not row["deadline"]:
            return "This task can't be postponed now."
        deadline = now + POSTPONE + timedelta(minutes=task_minutes(row["text"] or ""))   # A0: the task still needs its own time
        if not self._ports.free_time(deadline):
            return "Postponing would push it past your free time. Do it now, or it counts as missed."

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET deadline = ?, postponed_at = ? WHERE id = ? AND status = 'assigned' AND postponed_at IS NULL",
                              (_iso(deadline), _iso(now), row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.task_postponed", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"], "deadline": _iso(deadline)}))
            return bool(done)

        if not apply_transition(self._core, write=write):
            return "You already postponed this one. Do it now, or it counts as missed."
        decision("obedience", "An obedience task was postponed once (not alone).", task=row["catalog_key"], deadline=_iso(deadline))
        return f"Postponed once. The photo is now due by {self._ports.local_clock(deadline)}, and there's no second postponement."

    # ------------------------------------------------------------------ reads

    def is_obedience_task(self, task_id: str | None) -> bool:
        if not task_id:
            return False
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM obedience_tasks WHERE task_id = ?", (task_id,)) is not None

    def has_open_task(self) -> bool:
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM obedience_tasks WHERE user_id = ? AND status IN ('asking', 'assigned', 'submitted')", (self.user_id,)) is not None

    def due_now(self, now: datetime) -> bool:
        """A planned task whose moment has come: lines and recovery offers wait for it (the slot order after a spot check)."""
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM obedience_tasks WHERE user_id = ? AND status IN ('planned', 'asking') AND planned_at <= ?",
                                (self.user_id, _iso(now))) is not None

    # ------------------------------------------------------------------ internals

    def _row_for(self, day: date) -> dict | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM obedience_tasks WHERE user_id = ? AND local_date = ? AND slot = 0", (self.user_id, day.isoformat()))
        return None if row is None else dict(row)

    def _row(self, row_id: str) -> dict | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM obedience_tasks WHERE id = ?", (row_id,))
        return None if row is None else dict(row)

    def _close(self, row: dict, status: str, outcome: str, now: datetime, *, text: str | None = None, surprise: str | None = None) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE obedience_tasks SET status = ?, outcome = ?, decided_at = ?, surprise = COALESCE(?, surprise) "
                              "WHERE id = ? AND status IN ('planned', 'asking', 'assigned', 'submitted')",
                              (status, outcome, _iso(now), surprise, row["id"])).rowcount
            if done:
                write_event(tx, DomainEvent(event_type="obedience.task_decided", source_module="obedience", occurred_at=now,
                                            payload={"obedience_task_id": row["id"], "status": status, "outcome": outcome,
                                                     "trust_domain": self.trust_domain}))
                if text:
                    enqueue_in_transaction(tx, user_id=self.user_id, kind="obedience_task", dedupe_key=f"obedience:decided:{row['id']}", now=now,
                                           immediate=True, text=text)
            return bool(done)

        return apply_transition(self._core, write=write)

    def _update(self, row_id: str, **fields) -> None:
        def write(tx: Transaction, _s: object) -> None:
            sets = ", ".join(f"{k} = ?" for k in fields)
            tx.execute(f"UPDATE obedience_tasks SET {sets} WHERE id = ?", (*fields.values(), row_id))

        apply_transition(self._core, write=write)

    async def loop(self, clock, *, interval_seconds: float = 120.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.tick, clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The obedience-task pass failed; it continues.")
            await asyncio.sleep(interval_seconds)
