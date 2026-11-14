"""
training/plan.py

The owner's training plan as data (written 2026-11-14 in his workout chat; built in on 2026-11-15, work queue Q4c). Start weights, sets, rep
ranges and steps are his plan's; `unit` is how the load is counted ("kg", "kg/hand", "plates", "bw" = body weight, reps only).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Exercise", "EXERCISES", "SESSIONS", "ALIASES", "WARM_UP"]


@dataclass(frozen=True)
class Exercise:
    key: str
    name: str
    sets: int
    low: int
    high: int
    start: float
    step: float
    unit: str
    rest: str
    note: str = ""


EXERCISES: dict[str, Exercise] = {e.key: e for e in (
    Exercise("squat", "Back squat", 4, 8, 10, 105, 2.5, "kg", "3 min"),
    Exercise("bulgarian", "Bulgarian split squat (per leg)", 3, 8, 12, 20, 2.5, "kg/hand", "2 min", "then the vest"),
    Exercise("leg_ext", "Leg extension, 1 s hold at the top", 3, 12, 15, 2, 1, "plates", "90 s", "at least 2 large plates"),
    Exercise("leg_curl", "Leg curl (per leg)", 3, 10, 12, 3, 1, "plates", "90 s"),
    Exercise("calf", "Calf raise with the bar, 2 s hold at the bottom", 4, 12, 20, 80, 2.5, "kg", "60 s"),
    Exercise("rdl", "Romanian deadlift", 4, 6, 10, 90, 2.5, "kg", "3 min"),
    Exercise("front_squat", "Front squat", 3, 6, 10, 80, 2.5, "kg", "2.5 min"),
    Exercise("hip_thrust", "Hip thrust, 1 s hold at the top", 3, 8, 12, 110, 2.5, "kg", "2 min"),
    Exercise("nordic", "Nordic curl, as slow as you can down", 3, 3, 6, 0, 0, "bw", "2 min"),
    Exercise("seated_calf", "Seated calf raise", 4, 12, 20, 40, 2.5, "kg", "60 s", "weight on the knees"),
    Exercise("bench", "Bench press", 3, 5, 8, 75, 2.5, "kg", "2 min", "superset with pull-ups, ~30 s between"),
    Exercise("pullups", "Pull-ups", 3, 5, 8, 0, 0, "bw", "2 min", "as now"),
    Exercise("incline", "Incline dumbbell press", 3, 8, 12, 20, 2.5, "kg/hand", "90 s", "superset with cable rows"),
    Exercise("cable_row", "Cable row", 3, 8, 12, 34, 4, "kg", "90 s"),
    Exercise("triceps", "Triceps pushdown", 2, 8, 12, 18, 2, "kg", "60 s", "superset with curls"),
    Exercise("curl", "Biceps curl", 2, 8, 12, 14, 2, "kg", "60 s"),
    Exercise("lateral", "Lateral raise", 2, 12, 20, 14, 1, "kg", "60 s", "superset with decline crunches"),
    Exercise("crunch", "Decline crunch", 2, 12, 20, 0, 0, "bw", "60 s"),
)}

# (title, exercise keys). A and B alternate; short versions as in the plan.
SESSIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "legs_a": ("Leg day A: front of the thighs", ("squat", "bulgarian", "leg_ext", "leg_curl", "calf")),
    "legs_a_short": ("Leg day A, short (30 min)", ("squat", "calf")),
    "legs_b": ("Leg day B: back of the thighs and glutes", ("rdl", "front_squat", "hip_thrust", "nordic", "seated_calf")),
    "legs_b_short": ("Leg day B, short (30 min)", ("rdl", "hip_thrust")),
    "upper": ("Upper body in supersets (40-45 min)", ("bench", "pullups", "incline", "cable_row", "triceps", "curl", "lateral", "crunch")),
    "upper_short": ("Upper body, short (25 min): blocks 1 and 2", ("bench", "pullups", "incline", "cable_row")),
}
WARM_UP = {"upper": "Warm-up: one set of bench with 60 kg.", "upper_short": "Warm-up: one set of bench with 60 kg."}

# Words he may use in `log ...` (English and Czech).
ALIASES: dict[str, str] = {
    "squat": "squat", "back squat": "squat", "dřep": "squat", "drep": "squat",
    "bulgarian": "bulgarian", "bulgarian split squat": "bulgarian", "bulharský dřep": "bulgarian",
    "leg extension": "leg_ext", "leg ext": "leg_ext", "předkopávání": "leg_ext",
    "leg curl": "leg_curl", "zakopávání": "leg_curl",
    "calf": "calf", "calf raise": "calf", "výpony": "calf",
    "rdl": "rdl", "romanian deadlift": "rdl", "rumunský mrtvý tah": "rdl",
    "front squat": "front_squat", "přední dřep": "front_squat",
    "hip thrust": "hip_thrust",
    "nordic": "nordic", "nordic curl": "nordic",
    "seated calf": "seated_calf", "výpony vsedě": "seated_calf",
    "bench": "bench", "bench press": "bench",
    "pullups": "pullups", "pull-ups": "pullups", "pull ups": "pullups", "přítahy": "pullups",
    "incline": "incline", "incline press": "incline", "šikmé tlaky": "incline",
    "row": "cable_row", "cable row": "cable_row", "přítahy kladky": "cable_row",
    "triceps": "triceps", "pushdown": "triceps",
    "curl": "curl", "biceps": "curl", "biceps curl": "curl",
    "lateral": "lateral", "lateral raise": "lateral", "upažování": "lateral",
    "crunch": "crunch", "decline crunch": "crunch",
}
