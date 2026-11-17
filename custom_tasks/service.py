"""
custom_tasks/service.py

Another wearer's own erotic tasks (owner, 2026-11-17: "ask them their limits and let me with you do them the tasks - it will be then send to
them for approval"). The built-in catalogue is written around the owner's own limits and gear, so another wearer never gets it:

1. The wearer gives their limits in the DM: `limits hard <text>`, `limits soft <text>`, `limits likes <text>`; `limits` shows them.
2. The owner reads them (`python -m cli tenants limits <tenant>`) and writes tasks with Claude (`python -m cli tenants task-propose ...`).
3. The wearer is told, reads them with `tasks` and decides each one: `task approve <n>` / `task reject <n>` (`task approve all`). An
   approved task can be rejected later at once (consent is withdrawn immediately).
4. Their daily erotic task is drawn only from their approved tasks, and `erotic tasks on` waits until at least one is approved.

Workouts and temptations the same way (owner, 2026-11-17: "make everything including workouts and temptations available - of course after
setting up"):

- A training plan (`python -m cli tenants training-propose <tenant> --file plan.json`, format in training/plan.py `plan_from_json`). The
  wearer reads it with `training plan` and answers `training plan approve` / `training plan reject`; `workouts on` waits for an approved
  plan, and rejecting the approved plan later stops the sessions at once. A newer plan replaces the older one only when it is approved.
- Temptations (`python -m cli tenants task-propose <tenant> --temptation --text ...`): numbered with the tasks, approved the same way;
  `temptations on` waits for one, and only approved ones are ever offered.

The only writer of `wearer_limits`, `custom_tasks` (migrations 093, 094) and `wearer_training_plans` (094). Events: `custom_tasks.limits_set`,
`custom_tasks.proposed`, `custom_tasks.decided`, `custom_tasks.plan_proposed`, `custom_tasks.plan_decided`. Not wired for the owner: his catalogue is his own, and a wearer must never be able to write their own tasks into a
database someone else keeps (a brat would write easy ones). A wearer running the bot on their own PC is that install's owner.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from notifications.queue import enqueue_in_transaction

__all__ = ["CustomTasks", "CustomTask", "LIMIT_FIELDS", "MODES", "KINDS", "NOT_YET", "PLAN_NOT_YET", "TEMPTATION_NOT_YET"]

LIMIT_FIELDS = {"hard": "Hard limits (never)", "soft": "Soft limits (only carefully, or ask first)", "likes": "What you'd like"}
MODES = ("alone", "discreet", "both")
MAX_TEXT = 600
NOT_YET = ("Erotic tasks aren't available for you yet: your keyholder writes them from your limits, and you approve each one first. "
           "Set your limits with `limits hard ...`, `limits soft ...` and `limits likes ...`; you'll be told when tasks are ready to review.")
PLAN_NOT_YET = ("Workouts aren't available for you yet: your keyholder writes your training plan, and you approve it first "
                "(`training plan`). You'll be told when it's ready to review.")
TEMPTATION_NOT_YET = ("Temptations aren't available for you yet: your keyholder writes them from your limits, and you approve each one "
                      "first (`tasks`). You'll be told when one is ready to review.")
KINDS = ("task", "temptation")
_PLAN_WORDS = {"training plan": "show", "my training plan": "show", "training plan approve": "approve", "approve training plan": "approve",
               "training plan reject": "reject", "reject training plan": "reject"}
_LIMITS = re.compile(r"^limits?\s+(hard|soft|likes?)\s*:?\s*(.*)$", re.IGNORECASE | re.DOTALL)
_DECIDE = re.compile(r"^task\s+(approve|reject)\s+(all|\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class CustomTask:
    number: int
    text: str
    needs: frozenset
    modes: frozenset
    status: str
    kind: str = "task"

    @property
    def key(self) -> str:
        return f"custom:{self.number}"


def _row(r) -> CustomTask:
    modes = frozenset({"alone", "discreet"}) if r["modes"] == "both" else frozenset({r["modes"]})
    return CustomTask(int(r["number"]), r["text"], frozenset(n for n in (r["needs"] or "").split(",") if n), modes, r["status"],
                      r["kind"] if "kind" in r.keys() else "task")


class CustomTasks:
    def __init__(self, core: CoreDatabase, *, user_id: str) -> None:
        self._core = core
        self.user_id = user_id
        self._plan_cache: tuple[str, object] | None = None

    # ------------------------------------------------------------------ reads

    def limits(self) -> dict[str, str | None]:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT hard, soft, likes FROM wearer_limits WHERE user_id = ?", (self.user_id,))
        return {k: (row[k] if row else None) for k in LIMIT_FIELDS}

    def tasks(self, status: str | None = None, kind: str | None = None) -> list[CustomTask]:
        sql, args = "SELECT number, text, needs, modes, status, kind FROM custom_tasks WHERE user_id = ? ", [self.user_id]
        if status:
            sql, args = sql + "AND status = ? ", args + [status]
        if kind:
            sql, args = sql + "AND kind = ? ", args + [kind]
        with self._core.transaction() as tx:
            rows = tx.fetch_all(sql + "ORDER BY number", tuple(args))
        return [_row(r) for r in rows]

    def approved(self) -> list[CustomTask]:
        """Approved daily tasks (never a temptation)."""
        return self.tasks("approved", "task")

    def temptations(self) -> list[CustomTask]:
        """Approved temptations."""
        return self.tasks("approved", "temptation")

    def not_yet(self, _user_id: str | None = None) -> str | None:
        """For the `erotic tasks on` switch: the reason it can't be switched on yet, or None."""
        return None if self.approved() else NOT_YET

    def temptation_not_yet(self, _user_id: str | None = None) -> str | None:
        """For the `temptations on` switch."""
        return None if self.temptations() else TEMPTATION_NOT_YET

    def plan_not_yet(self, _user_id: str | None = None) -> str | None:
        """For the `workouts on` switch."""
        return None if self.training_plan() is not None else PLAN_NOT_YET

    def _plan_row(self, status: str):
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT id, number, plan_json FROM wearer_training_plans WHERE user_id = ? AND status = ? ORDER BY number DESC LIMIT 1",
                                (self.user_id, status))

    def training_plan(self):
        """The approved training plan (training.plan.Plan), or None. TrainingPlan asks this for every session."""
        from training.plan import plan_from_json
        row = self._plan_row("approved")
        if row is None:
            return None
        if self._plan_cache is None or self._plan_cache[0] != row["id"]:
            self._plan_cache = (row["id"], plan_from_json(json.loads(row["plan_json"])))
        return self._plan_cache[1]

    # ------------------------------------------------------------------ the owner's side (CLI)

    def propose(self, *, text: str, needs: frozenset = frozenset(), modes: str = "alone", now: datetime, kind: str = "task") -> int:
        """A task (or a temptation) the owner wrote for this wearer; the wearer is told to review it. Returns its number."""
        text = " ".join((text or "").split())
        if not text or len(text) > MAX_TEXT:
            raise ValueError(f"a task needs a text of at most {MAX_TEXT} characters")
        if modes not in MODES:
            raise ValueError(f"modes must be one of {', '.join(MODES)}")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        what = "temptation" if kind == "temptation" else "task"

        def write(tx: Transaction, _s: object) -> int:
            number = int(tx.fetch_one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM custom_tasks WHERE user_id = ?", (self.user_id,))["n"])
            tx.execute("INSERT INTO custom_tasks (id, user_id, number, text, needs, modes, status, proposed_at, kind) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
                       (str(uuid.uuid4()), self.user_id, number, text, ",".join(sorted(needs)), modes, _iso(now), kind))
            write_event(tx, DomainEvent(event_type="custom_tasks.proposed", source_module="custom_tasks", occurred_at=now,
                                        payload={"user_id": self.user_id, "number": number, "kind": kind}))
            enqueue_in_transaction(tx, user_id=self.user_id, kind="custom_tasks", dedupe_key=f"custom_tasks:proposed:{number}", now=now,
                                   text=f"Your keyholder wrote a new {what} for you from your limits. Send `tasks` to read it, then "
                                        "`task approve <number>` or `task reject <number>`. Nothing is given to you before you approve it.")
            return number

        return apply_transition(self._core, write=write)

    def propose_plan(self, data: dict, now: datetime) -> int:
        """A training plan the owner wrote for this wearer (validated by training.plan.plan_from_json); an older plan still waiting is
        replaced. The approved plan stays in force until this one is approved. Returns its number."""
        from training.plan import plan_from_json
        plan_from_json(data)                                              # raises ValueError with the reason

        def write(tx: Transaction, _s: object) -> int:
            number = int(tx.fetch_one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM wearer_training_plans WHERE user_id = ?", (self.user_id,))["n"])
            tx.execute("UPDATE wearer_training_plans SET status = 'replaced', decided_at = ? WHERE user_id = ? AND status = 'proposed'",
                       (_iso(now), self.user_id))
            tx.execute("INSERT INTO wearer_training_plans (id, user_id, number, plan_json, status, proposed_at) VALUES (?, ?, ?, ?, 'proposed', ?)",
                       (str(uuid.uuid4()), self.user_id, number, json.dumps(data), _iso(now)))
            write_event(tx, DomainEvent(event_type="custom_tasks.plan_proposed", source_module="custom_tasks", occurred_at=now,
                                        payload={"user_id": self.user_id, "number": number}))
            enqueue_in_transaction(tx, user_id=self.user_id, kind="custom_tasks", dedupe_key=f"custom_tasks:plan:{number}", now=now,
                                   text="Your keyholder wrote a training plan for you. Send `training plan` to read it, then `training plan approve` "
                                        "or `training plan reject`. No workout is given to you before you approve it.")
            return number

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ the wearer's side (DM)

    def command(self, user_id: str, text: str, now: datetime) -> str | None:
        """`limits`, `limits hard|soft|likes <text>`, `tasks`, `task approve|reject <n|all>`; None for any other message."""
        if user_id != self.user_id:
            return None
        said = " ".join((text or "").split())
        low = said.lower()
        if low in ("limits", "limit", "my limits"):
            return self._show_limits()
        if (m := _LIMITS.match(said)) is not None:
            return self._set_limit(m.group(1).lower(), m.group(2).strip(), now)
        if low in ("tasks", "my tasks", "task list"):
            return self._list()
        if (m := _DECIDE.match(low)) is not None:
            return self._decide(m.group(1) == "approve", m.group(2), now)
        if low.strip(" .!") in _PLAN_WORDS:
            what = _PLAN_WORDS[low.strip(" .!")]
            return self._show_plan() if what == "show" else self._decide_plan(what == "approve", now)
        return None

    def _show_plan(self) -> str:
        from training.plan import plan_from_json, plan_summary
        waiting, approved = self._plan_row("proposed"), self._plan_row("approved")
        if waiting is not None:
            head = "A training plan waiting for your decision" + (" (it would replace your current one)" if approved is not None else "") + ":\n"
            return (head + plan_summary(plan_from_json(json.loads(waiting["plan_json"])))
                    + "\n`training plan approve` or `training plan reject`.")
        if approved is not None:
            return ("Your training plan:\n" + plan_summary(self.training_plan())
                    + "\n`training plan reject` takes it back at any time (workouts then stop).")
        return "No training plan has been written for you yet. Your keyholder writes it, and you approve it before any workout."

    def _decide_plan(self, approve: bool, now: datetime) -> str:
        def write(tx: Transaction, _s: object) -> str | None:
            waiting = tx.fetch_one("SELECT id, number FROM wearer_training_plans WHERE user_id = ? AND status = 'proposed' ORDER BY number DESC LIMIT 1",
                                   (self.user_id,))
            target = waiting
            if target is None and not approve:
                target = tx.fetch_one("SELECT id, number FROM wearer_training_plans WHERE user_id = ? AND status = 'approved' ORDER BY number DESC LIMIT 1",
                                      (self.user_id,))
            if target is None:
                return None
            if approve:
                tx.execute("UPDATE wearer_training_plans SET status = 'replaced', decided_at = ? WHERE user_id = ? AND status = 'approved'",
                           (_iso(now), self.user_id))
            status = "approved" if approve else "rejected"
            tx.execute("UPDATE wearer_training_plans SET status = ?, decided_at = ? WHERE id = ?", (status, _iso(now), target["id"]))
            write_event(tx, DomainEvent(event_type="custom_tasks.plan_decided", source_module="custom_tasks", occurred_at=now,
                                        payload={"user_id": self.user_id, "number": int(target["number"]), "status": status}))
            return "waiting" if target is waiting else "approved"

        which = apply_transition(self._core, write=write)
        if which is None:
            return "There is no training plan waiting." if approve else "There is no training plan to reject."
        if approve:
            return "Approved: this is your training plan now. `workouts on` starts the sessions."
        return ("Rejected: that plan will never be given to you." if which == "waiting"
                else "Rejected: your training plan is taken back, and no more workouts are given to you.")

    def _show_limits(self) -> str:
        values = self.limits()
        rows = [f"- {label}: {values[k] or 'not given yet'}" for k, label in LIMIT_FIELDS.items()]
        return ("Your limits:\n" + "\n".join(rows) + "\nChange one with `limits hard <text>`, `limits soft <text>` or `limits likes <text>`. "
                "Your keyholder writes your tasks from these, and you approve every task before it can be given.")

    def _set_limit(self, field: str, value: str, now: datetime) -> str:
        field = "likes" if field.startswith("like") else field
        if not value:
            return f"Write it after the word: `limits {field} <text>`."

        def write(tx: Transaction, _s: object) -> None:
            tx.execute(f"INSERT INTO wearer_limits (user_id, {field}, updated_at) VALUES (?, ?, ?) "
                       f"ON CONFLICT (user_id) DO UPDATE SET {field} = excluded.{field}, updated_at = excluded.updated_at",
                       (self.user_id, value[:MAX_TEXT], _iso(now)))
            write_event(tx, DomainEvent(event_type="custom_tasks.limits_set", source_module="custom_tasks", occurred_at=now,
                                        payload={"user_id": self.user_id, "field": field}))

        apply_transition(self._core, write=write)
        return f"Saved. {LIMIT_FIELDS[field]}: {value[:MAX_TEXT]}"

    def _list(self) -> str:
        tasks = self.tasks()
        waiting = [t for t in tasks if t.status == "proposed"]
        approved = [t for t in tasks if t.status == "approved"]
        if not waiting and not approved:
            return "No tasks have been written for you yet. Your keyholder writes them from your limits (`limits`)."
        out = []
        if waiting:
            out.append("Waiting for your decision:")
            out.extend(f"{t.number}. {'Temptation: ' if t.kind == 'temptation' else ''}{t.text}{_where(t)}" for t in waiting)
            out.append("`task approve <number>` or `task reject <number>` (or `task approve all`).")
        if approved:
            out.append(f"Approved ({len(approved)}): " + ", ".join(f"{t.number}{' (temptation)' if t.kind == 'temptation' else ''}" for t in approved)
                       + ". `task reject <number>` takes one back at any time.")
        return "\n".join(out)

    def _decide(self, approve: bool, which: str, now: datetime) -> str:
        status = "approved" if approve else "rejected"

        def write(tx: Transaction, _s: object) -> list[int]:
            if which == "all":
                if not approve:
                    return []
                rows = tx.fetch_all("SELECT number FROM custom_tasks WHERE user_id = ? AND status = 'proposed'", (self.user_id,))
            else:
                allowed = ("proposed",) if approve else ("proposed", "approved")
                rows = tx.fetch_all(f"SELECT number FROM custom_tasks WHERE user_id = ? AND number = ? AND status IN ({','.join('?' * len(allowed))})",
                                    (self.user_id, int(which), *allowed))
            numbers = [int(r["number"]) for r in rows]
            for n in numbers:
                tx.execute("UPDATE custom_tasks SET status = ?, decided_at = ? WHERE user_id = ? AND number = ?", (status, _iso(now), self.user_id, n))
                write_event(tx, DomainEvent(event_type="custom_tasks.decided", source_module="custom_tasks", occurred_at=now,
                                            payload={"user_id": self.user_id, "number": n, "status": status}))
            return numbers

        if which == "all" and not approve:
            return "Reject them one at a time: `task reject <number>`."
        numbers = apply_transition(self._core, write=write)
        if not numbers:
            return "There is no such task waiting." if approve else "There is no such task to reject."
        done = ", ".join(str(n) for n in numbers)
        return (f"Approved: {done}. It can now be given to you." if approve
                else f"Rejected: {done}. It will never be given to you.")


def _where(task: CustomTask) -> str:
    if task.modes == frozenset({"alone"}):
        return " (only when you're alone)"
    if task.modes == frozenset({"discreet"}):
        return " (when someone's around)"
    return ""
