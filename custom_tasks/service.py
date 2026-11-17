"""
custom_tasks/service.py

Another wearer's own erotic tasks (owner, 2026-11-17: "ask them their limits and let me with you do them the tasks - it will be then send to
them for approval"). The built-in catalogue is written around the owner's own limits and gear, so another wearer never gets it:

1. The wearer gives their limits in the DM: `limits hard <text>`, `limits soft <text>`, `limits likes <text>`; `limits` shows them.
2. The owner reads them (`python -m cli tenants limits <tenant>`) and writes tasks with Claude (`python -m cli tenants task-propose ...`).
3. The wearer is told, reads them with `tasks` and decides each one: `task approve <n>` / `task reject <n>` (`task approve all`). An
   approved task can be rejected later at once (consent is withdrawn immediately).
4. Their daily erotic task is drawn only from their approved tasks, and `erotic tasks on` waits until at least one is approved.

The only writer of `wearer_limits` and `custom_tasks` (migration 093). Events: `custom_tasks.limits_set`, `custom_tasks.proposed`,
`custom_tasks.decided`. Not wired for the owner: his catalogue is his own, and a wearer must never be able to write their own tasks into a
database someone else keeps (a brat would write easy ones). A wearer running the bot on their own PC is that install's owner.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from notifications.queue import enqueue_in_transaction

__all__ = ["CustomTasks", "CustomTask", "LIMIT_FIELDS", "MODES", "NOT_YET"]

LIMIT_FIELDS = {"hard": "Hard limits (never)", "soft": "Soft limits (only carefully, or ask first)", "likes": "What you'd like"}
MODES = ("alone", "discreet", "both")
MAX_TEXT = 600
NOT_YET = ("Erotic tasks aren't available for you yet: your keyholder writes them from your limits, and you approve each one first. "
           "Set your limits with `limits hard ...`, `limits soft ...` and `limits likes ...`; you'll be told when tasks are ready to review.")
_LIMITS = re.compile(r"^limits?\s+(hard|soft|likes?)\s*:?\s*(.*)$", re.IGNORECASE | re.DOTALL)
_DECIDE = re.compile(r"^task\s+(approve|reject)\s+(all|\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class CustomTask:
    number: int
    text: str
    needs: frozenset
    modes: frozenset
    status: str

    @property
    def key(self) -> str:
        return f"custom:{self.number}"


def _row(r) -> CustomTask:
    modes = frozenset({"alone", "discreet"}) if r["modes"] == "both" else frozenset({r["modes"]})
    return CustomTask(int(r["number"]), r["text"], frozenset(n for n in (r["needs"] or "").split(",") if n), modes, r["status"])


class CustomTasks:
    def __init__(self, core: CoreDatabase, *, user_id: str) -> None:
        self._core = core
        self.user_id = user_id

    # ------------------------------------------------------------------ reads

    def limits(self) -> dict[str, str | None]:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT hard, soft, likes FROM wearer_limits WHERE user_id = ?", (self.user_id,))
        return {k: (row[k] if row else None) for k in LIMIT_FIELDS}

    def tasks(self, status: str | None = None) -> list[CustomTask]:
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT number, text, needs, modes, status FROM custom_tasks WHERE user_id = ? "
                                + ("AND status = ? " if status else "") + "ORDER BY number",
                                (self.user_id, status) if status else (self.user_id,))
        return [_row(r) for r in rows]

    def approved(self) -> list[CustomTask]:
        return self.tasks("approved")

    def not_yet(self, _user_id: str | None = None) -> str | None:
        """For the `erotic tasks on` switch: the reason it can't be switched on yet, or None."""
        return None if self.approved() else NOT_YET

    # ------------------------------------------------------------------ the owner's side (CLI)

    def propose(self, *, text: str, needs: frozenset = frozenset(), modes: str = "alone", now: datetime) -> int:
        """A task the owner wrote for this wearer; the wearer is told to review it. Returns its number."""
        text = " ".join((text or "").split())
        if not text or len(text) > MAX_TEXT:
            raise ValueError(f"a task needs a text of at most {MAX_TEXT} characters")
        if modes not in MODES:
            raise ValueError(f"modes must be one of {', '.join(MODES)}")

        def write(tx: Transaction, _s: object) -> int:
            number = int(tx.fetch_one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM custom_tasks WHERE user_id = ?", (self.user_id,))["n"])
            tx.execute("INSERT INTO custom_tasks (id, user_id, number, text, needs, modes, status, proposed_at) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)",
                       (str(uuid.uuid4()), self.user_id, number, text, ",".join(sorted(needs)), modes, _iso(now)))
            write_event(tx, DomainEvent(event_type="custom_tasks.proposed", source_module="custom_tasks", occurred_at=now,
                                        payload={"user_id": self.user_id, "number": number}))
            enqueue_in_transaction(tx, user_id=self.user_id, kind="custom_tasks", dedupe_key=f"custom_tasks:proposed:{number}", now=now,
                                   text="Your keyholder wrote a new task for you from your limits. Send `tasks` to read it, then "
                                        "`task approve <number>` or `task reject <number>`. Nothing is given to you before you approve it.")
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
        return None

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
            out.extend(f"{t.number}. {t.text}{_where(t)}" for t in waiting)
            out.append("`task approve <number>` or `task reject <number>` (or `task approve all`).")
        if approved:
            out.append(f"Approved ({len(approved)}): " + ", ".join(str(t.number) for t in approved)
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
