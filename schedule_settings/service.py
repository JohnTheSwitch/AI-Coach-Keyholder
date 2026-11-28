"""
schedule_settings/service.py

Per-day end of the school/work day (owner, 2026-11-25: "Per day end times, round it to 3 and 5 pm"). One WORKDAY_END for every weekday did
not fit his timetable (home at 15:00 on Mon/Thu/Fri, at 17:00 on Tue/Wed), so a weekday can have its own end. The wearer changes it in the
DM, with no `.env` edit:

    schedule                         what is set
    schedule mon thu fri 15:00       those days end at 15:00
    schedule tue wed default         back to WORKDAY_END

Table `schedule_work_ends` (migration 095), written only here; event `schedule_settings.work_end_changed`. The windows read the ends through
`work_ends_for(db_path)`: one shared, in-memory map per database, loaded once and updated by the command, so a change applies at once,
without a restart, everywhere a `WindowConfig` is used (`schedule_windows.windows.WindowConfig.end_of_work`). A day with no row uses
WORKDAY_END. Weekends and public holidays have no work time, so they cannot be set.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso

logger = logging.getLogger("ai_coach_keyholder.schedule_settings")

__all__ = ["WorkEnds", "work_ends_for", "quiet_ends_for", "ScheduleSettings", "DAYS", "WEEKEND_WAKE_LATEST"]

WEEKEND_WAKE_LATEST = time(12, 0)       # a weekend morning is quiet until at most noon

DAYS = ("mon", "tue", "wed", "thu", "fri")
_DAY_WORDS = {"mon": 0, "monday": 0, "po": 0, "tue": 1, "tuesday": 1, "ut": 1, "út": 1, "wed": 2, "wednesday": 2, "st": 2,
              "thu": 3, "thursday": 3, "ct": 3, "čt": 3, "fri": 4, "friday": 4, "pa": 4, "pá": 4,
              "weekdays": -1, "all": -1}
_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


class WorkEnds:
    """weekday (0 = Monday) -> the end of that day's work time; a missing day uses WORKDAY_END."""

    def __init__(self, ends: dict[int, time] | None = None) -> None:
        self._ends = dict(ends or {})
        self._lock = threading.Lock()

    def get(self, weekday: int) -> time | None:
        with self._lock:
            return self._ends.get(weekday)

    def all(self) -> dict[int, time]:
        with self._lock:
            return dict(self._ends)

    def _set(self, weekday: int, end: time | None) -> None:
        with self._lock:
            if end is None:
                self._ends.pop(weekday, None)
            else:
                self._ends[weekday] = end


_SHARED: dict[str, WorkEnds] = {}
_SHARED_QUIET: dict[str, WorkEnds] = {}
_SHARED_LOCK = threading.Lock()


def _read(db_path: str, query: str = "SELECT weekday, work_end FROM schedule_work_ends") -> dict[int, time]:
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:                                            # `with` would only commit; the file must be let go (tenant archival moves it)
            rows = conn.execute(query).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}                                   # no database or table yet: WORKDAY_END everywhere
    out: dict[int, time] = {}
    for weekday, end in rows:
        try:
            hh, mm = str(end).split(":")
            out[int(weekday)] = time(int(hh), int(mm))
        except (ValueError, TypeError):
            logger.warning("A stored work end could not be read; that day uses WORKDAY_END.")
    return out


def work_ends_for(db_path: str | Path) -> WorkEnds:
    """The shared map for this database (loaded on first use)."""
    key = str(Path(db_path).resolve())
    with _SHARED_LOCK:
        ends = _SHARED.get(key)
        if ends is None:
            ends = _SHARED[key] = WorkEnds(_read(key))
        return ends


def quiet_ends_for(db_path: str | Path) -> WorkEnds:
    """Owner, 2026-11-28: the shared map weekday (5 = Saturday, 6 = Sunday) -> the end of that morning's quiet hours."""
    key = str(Path(db_path).resolve())
    with _SHARED_LOCK:
        ends = _SHARED_QUIET.get(key)
        if ends is None:
            ends = _SHARED_QUIET[key] = WorkEnds(_read(key, "SELECT weekday, quiet_end FROM schedule_quiet_ends"))
        return ends


def _parse_time(word: str) -> time | None:
    word = word.strip().lower().replace(".", ":")
    pm = word.endswith("pm")
    word = word.removesuffix("pm").removesuffix("am").strip()
    try:
        if ":" in word:
            hh, mm = word.split(":")
            t = time(int(hh), int(mm))
        else:
            t = time(int(word), 0)
    except ValueError:
        return None
    if pm and t.hour < 12:
        t = t.replace(hour=t.hour + 12)
    return t


class ScheduleSettings:
    def __init__(self, core: CoreDatabase, *, db_path: str | Path, user_id: str, default_end: time, valid: Callable[[time], str | None]) -> None:
        """`valid(end)`: None when `end` fits the day's layout (after the work start, room before quiet hours), else why not."""
        self._core, self._user, self._default, self._valid = core, user_id, default_end, valid
        self.ends = work_ends_for(db_path)
        self.quiet = quiet_ends_for(db_path)          # owner, 2026-11-28: weekend mornings quiet until his wake time
        # Owner, 2026-11-25 ("make it wait 24 hours"): `(user_id, key, now) -> reply | None` (opt_outs.request_off): a change that ends a
        # day EARLIER waits 24 hours while locked; None means it applies at once. A later end (less free time) is always at once.
        self.request_change: Callable[[str, str, datetime], str | None] | None = None

    def listing(self) -> str:
        ends = self.ends.all()
        lines = [f"{_NAMES[d]}: {ends[d].strftime('%H:%M') if d in ends else self._default.strftime('%H:%M') + ' (default)'}" for d in range(5)]
        wake = self.quiet.all()
        weekend = ("Weekend mornings are quiet until " + " / ".join(f"{('Saturday', 'Sunday')[d - 5]} {wake[d].strftime('%H:%M')}" for d in sorted(wake))
                   + ".\n") if wake else ""
        return ("Your school/work day ends:\n" + "\n".join(lines) + "\nWeekends and public holidays are free outside quiet hours.\n" + weekend +
                "Change it: `schedule mon thu fri 15:00`, or `schedule tue default`.")

    def command(self, user_id: str, text: str, now: datetime) -> str | None:
        """`schedule ...`; None for any other message."""
        words = " ".join((text or "").lower().replace(",", " ").split()).split()
        if user_id != self._user or not words or words[0] not in ("schedule", "rozvrh"):
            return None
        if len(words) == 1:
            return self.listing()
        days: list[int] = []
        for w in words[1:-1]:
            if w not in _DAY_WORDS:
                return f"I don't know the day \"{w}\". Use mon, tue, wed, thu, fri (or weekdays)."
            days.extend(range(5) if _DAY_WORDS[w] < 0 else [_DAY_WORDS[w]])
        if not days:
            return "Name the days and the time, for example `schedule mon thu fri 15:00`."
        last = words[-1]
        end = None if last in ("default", "reset") else _parse_time(last)
        if end is None and last not in ("default", "reset"):
            return f"I couldn't read the time \"{last}\". Use HH:MM, for example 15:00."
        if end is not None and (why := self._valid(end)) is not None:
            return why
        days = sorted(set(days))
        new_end = end or self._default
        earlier = [d for d in days if new_end < (self.ends.get(d) or self._default)]
        later = [d for d in days if d not in earlier]
        replies = []
        if earlier and self.request_change is not None:
            waiting = self.request_change(user_id, f"schedule:{','.join(map(str, earlier))}={end.strftime('%H:%M') if end else 'default'}", now)
            if waiting is not None:
                replies.append(waiting)
            else:
                later = sorted(later + earlier)
        else:
            later = sorted(later + earlier)
        changed = self._write(later, end, now) if later else []
        if changed:
            replies.insert(0, "Done.")
        if not replies:
            return "That's already set.\n" + self.listing()
        return "\n".join(replies) + "\n" + self.listing()

    @staticmethod
    def describe(change: str) -> str:
        """`0,3=14:00` -> "Monday and Thursday's school day to end at 14:00" (for opt_outs)."""
        days, _, value = change.partition("=")
        names = [_NAMES[int(d)] for d in days.split(",") if d.strip().isdigit() and int(d) < 5]
        who = " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " and " + names[-1]
        return f"{who}'s school day to end {'at your default time' if value == 'default' else 'at ' + value}"

    def apply_change(self, user_id: str, change: str, now: datetime) -> str:
        """A waiting change he confirmed after 24 hours (opt_outs)."""
        days, _, value = change.partition("=")
        end = None if value == "default" else _parse_time(value)
        picked = [int(d) for d in days.split(",") if d.strip().isdigit() and int(d) < 5]
        if user_id != self._user or not picked or (end is None and value != "default"):
            return "That change couldn't be read; nothing changed."
        if end is not None and (why := self._valid(end)) is not None:
            return why
        self._write(picked, end, now)
        return "Done.\n" + self.listing()

    def seed(self, ends: dict[int, time], now: datetime) -> list[int]:
        """Days with no row yet get these ends (his own choice is never overwritten). Returns the days set now."""
        missing = set(range(5)) - set(self.ends.all())
        done: list[int] = []
        for end in sorted(set(ends.values())):
            done += self._write(sorted(d for d, e in ends.items() if e == end and d in missing), end, now)
        return done

    def seed_quiet(self, ends: dict[int, time], now: datetime) -> list[int]:
        """Owner, 2026-11-28 ("quiet until 10:00"): Saturday/Sunday with no row yet get these wake times. Returns the days set now."""
        done: list[int] = []

        def write(tx: Transaction, _s: object) -> list[int]:
            for d, end in sorted(ends.items()):
                if d not in (5, 6) or not (time(0, 0) < end <= WEEKEND_WAKE_LATEST):
                    continue
                if tx.fetch_one("SELECT 1 FROM schedule_quiet_ends WHERE weekday = ?", (d,)):
                    continue
                tx.execute("INSERT INTO schedule_quiet_ends (weekday, quiet_end, changed_at) VALUES (?, ?, ?)", (d, end.strftime("%H:%M"), _iso(now)))
                write_event(tx, DomainEvent(event_type="schedule_settings.quiet_end_changed", source_module="schedule_settings", occurred_at=now,
                                            payload={"user_id": self._user, "weekday": d, "quiet_end": end.strftime("%H:%M"), "previous": None}))
                done.append(d)
            return done

        apply_transition(self._core, write=write)
        for d in done:
            self.quiet._set(d, ends[d])
        if done:
            decision("schedule", "Weekend mornings are quiet until his wake time.", days=done)
        return done

    def _write(self, days: list[int], end: time | None, now: datetime) -> list[int]:
        changed: list[int] = []

        def write(tx: Transaction, _s: object) -> list[int]:
            for d in days:
                old = tx.fetch_one("SELECT work_end FROM schedule_work_ends WHERE weekday = ?", (d,))
                new = end.strftime("%H:%M") if end is not None else None
                if (old["work_end"] if old else None) == new:
                    continue
                if new is None:
                    tx.execute("DELETE FROM schedule_work_ends WHERE weekday = ?", (d,))
                else:
                    tx.execute("INSERT INTO schedule_work_ends (weekday, work_end, changed_at) VALUES (?, ?, ?) ON CONFLICT (weekday) DO UPDATE "
                               "SET work_end = excluded.work_end, changed_at = excluded.changed_at", (d, new, _iso(now)))
                write_event(tx, DomainEvent(event_type="schedule_settings.work_end_changed", source_module="schedule_settings", occurred_at=now,
                                            payload={"user_id": self._user, "weekday": d, "work_end": new,
                                                     "previous": old["work_end"] if old else None}))
                changed.append(d)
            return changed

        apply_transition(self._core, write=write)
        for d in changed:
            self.ends._set(d, end)
        if changed:
            decision("schedule", "The end of the school/work day was changed.", days=[DAYS[d] for d in changed],
                     work_end=end.strftime("%H:%M") if end else "default")
        return changed
