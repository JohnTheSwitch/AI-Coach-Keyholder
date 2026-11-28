"""
training/plan.py

The owner's training plan as data (written 2026-11-14 in his workout chat; built in on 2026-11-15, work queue Q4c). Start weights, sets, rep
ranges and steps are his plan's; `unit` is how the load is counted ("kg", "kg/hand", "plates", "bw" = body weight, reps only).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Exercise", "EXERCISES", "SESSIONS", "ALIASES", "WARM_UP", "Plan", "OWNER_PLAN", "SESSION_KEYS", "UNITS", "plan_from_json",
           "plan_summary", "DAY_NAMES"]


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
    Exercise("nordic", "Glute ham raise, as slow as you can down", 3, 3, 6, 0, 0, "bw", "2 min"),   # owner, 2026-11-28: his app's name
    Exercise("seated_calf", "Seated calf raise", 4, 12, 20, 40, 2.5, "kg", "60 s", "weight on the knees"),
    Exercise("bench", "Bench press", 3, 5, 8, 75, 2.5, "kg", "2 min", "superset with pull-ups, ~30 s between"),
    Exercise("pullups", "Pull-ups", 3, 5, 8, 0, 0, "bw", "2 min", "as now"),
    Exercise("incline", "Incline dumbbell press", 3, 8, 12, 20, 2.5, "kg/hand", "90 s", "superset with cable rows"),
    Exercise("cable_row", "Cable row", 3, 8, 12, 34, 4, "kg", "90 s"),
    Exercise("triceps", "Triceps pushdown", 2, 8, 12, 18, 4, "kg", "60 s", "superset with curls"),
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
    "nordic": "nordic", "nordic curl": "nordic", "glute ham raise": "nordic", "ghr": "nordic", "glute-ham raise": "nordic",
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


# ---------------------------------------------------------------------------- one plan per wearer (owner, 2026-11-17)
# Another wearer's plan is written by the owner with Claude (`python -m cli tenants training-propose`) and approved by the wearer before it
# can be given (custom_tasks/). It fills the same six sessions. Its days are its own (owner, 2026-11-17: "There has to be option for
# different training days"): one full leg day (A and B alternating), one full upper-body day and one or two candidate days for the optional
# short session (with two, the wearer is asked on the first which one; no answer means the second). The owner's: Saturday, Sunday, Mon/Thu.

SESSION_KEYS = ("legs_a", "legs_a_short", "legs_b", "legs_b_short", "upper", "upper_short")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
UNITS = ("kg", "kg/hand", "plates", "bw")


@dataclass(frozen=True)
class Plan:
    exercises: dict
    sessions: dict
    warm_up: dict
    aliases: dict
    proof: str = "Photo of the gym only."                # the Chaster photo after a session (voters see it)
    return_squat: float | None = None                    # the owner's plan: squat 95 kg on the first leg days back
    harder_bw: str = "slower"                            # how to make body-weight work harder once every set is at the top
    legs_day: int = 5                                    # weekday (Monday 0): the full leg day
    upper_day: int = 6                                   # the full upper-body day
    short_days: tuple = (0, 3)                           # the optional short session's candidate days, in week order (one or two)


OWNER_PLAN = Plan(EXERCISES, SESSIONS, WARM_UP, ALIASES, return_squat=95.0, harder_bw="slower, or the vest")


def plan_from_json(data: dict) -> Plan:
    """A wearer's plan from the JSON the owner writes. Raises ValueError with the reason when it is not usable.

    {"exercises": [{"key", "name", "sets", "low", "high", "start", "step", "unit", "rest", "note"?, "aliases"?: [...]}, ...],
     "sessions": {"legs_a": {"title", "exercises": [keys]}, ... all six},
     "warm_up": {"upper": "..."}?, "proof": "Photo of ... only."?}"""
    if not isinstance(data, dict):
        raise ValueError("the plan must be a JSON object")
    exercises, aliases = {}, {}
    for e in data.get("exercises") or []:
        try:
            ex = Exercise(str(e["key"]), str(e["name"]), int(e["sets"]), int(e["low"]), int(e["high"]), float(e["start"]),
                          float(e["step"]), str(e["unit"]), str(e["rest"]), str(e.get("note") or ""))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"an exercise is incomplete: {e!r}") from exc
        if ex.unit not in UNITS:
            raise ValueError(f"{ex.key}: unit must be one of {', '.join(UNITS)}")
        if not (1 <= ex.sets <= 10 and 1 <= ex.low <= ex.high <= 100 and ex.start >= 0 and ex.step >= 0):
            raise ValueError(f"{ex.key}: sets, reps, start or step out of range")
        exercises[ex.key] = ex
        for word in (ex.key, ex.key.replace("_", " "), ex.name.lower(), *(e.get("aliases") or [])):
            aliases[str(word).lower()] = ex.key
    if not exercises:
        raise ValueError("the plan has no exercises")
    sessions = {}
    for key in SESSION_KEYS:
        s = (data.get("sessions") or {}).get(key)
        if not isinstance(s, dict) or not s.get("title") or not s.get("exercises"):
            raise ValueError(f"session {key} is missing (all six are needed: {', '.join(SESSION_KEYS)})")
        unknown = [k for k in s["exercises"] if k not in exercises]
        if unknown:
            raise ValueError(f"session {key} names unknown exercises: {', '.join(map(str, unknown))}")
        sessions[key] = (str(s["title"]), tuple(s["exercises"]))
    warm_up = {k: str(v) for k, v in (data.get("warm_up") or {}).items() if k in SESSION_KEYS}
    proof = str(data.get("proof") or "Photo of the gym only.")
    if len(proof) > 80:
        raise ValueError("proof must be short (at most 80 characters)")
    schedule = data.get("schedule") or {}
    legs, upper = _day(schedule.get("legs", "saturday")), _day(schedule.get("upper", "sunday"))
    short = tuple(sorted(_day(d) for d in (schedule.get("short") or ["monday", "thursday"])))
    if not 1 <= len(short) <= 2 or len(set(short)) != len(short):
        raise ValueError("schedule.short must name one or two different days")
    if len({legs, upper, *short}) != 2 + len(short):
        raise ValueError("schedule: the leg day, the upper-body day and the short-session days must all be different")
    return Plan(exercises, sessions, warm_up, aliases, proof=proof, legs_day=legs, upper_day=upper, short_days=short)


def _day(name) -> int:
    word = str(name).strip().lower()
    for i, day in enumerate(DAY_NAMES):
        if word in (day.lower(), day.lower()[:3]):
            return i
    raise ValueError(f"unknown day '{name}' (use monday ... sunday)")


def _load(ex: Exercise, load: float) -> str:
    return {"kg": f"{load:g} kg", "kg/hand": f"{load:g} kg per hand", "plates": f"{load:g} large plates", "bw": "body weight"}[ex.unit]


def plan_summary(plan: Plan) -> str:
    """The plan as the wearer reads it before approving."""
    short = (f"One short session on {DAY_NAMES[plan.short_days[0]]} or {DAY_NAMES[plan.short_days[1]]} (you choose on "
             f"{DAY_NAMES[plan.short_days[0]]}; skipping it costs nothing)" if len(plan.short_days) == 2 else
             f"One short session on {DAY_NAMES[plan.short_days[0]]} (skipping it costs nothing)")
    lines = [f"{DAY_NAMES[plan.legs_day]}: the full leg day (A and B alternate). {DAY_NAMES[plan.upper_day]}: the full upper-body day. "
             f"{short}. Full sessions are mandatory. Proof in Chaster after each: " + plan.proof]
    for key in SESSION_KEYS:
        title, keys = plan.sessions[key]
        lines.append(f"**{title}**")
        if key in plan.warm_up:
            lines.append(plan.warm_up[key])
        for k in keys:
            ex = plan.exercises[k]
            lines.append(f"- {ex.name}: {ex.sets} x {ex.low}-{ex.high}, start {_load(ex, ex.start)}, rest {ex.rest}" + (f" ({ex.note})" if ex.note else ""))
    return "\n".join(lines)
