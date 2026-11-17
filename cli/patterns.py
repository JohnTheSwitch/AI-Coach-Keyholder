"""
cli/patterns.py

`python -m cli patterns` (learning options 2 and 3, approved by the owner 2026-11-16: "report and propose only"). Read-only, for the owner
and Claude, never shown to the wearer and never acted on by the bot. A finding becomes a proposal the owner decides; nothing here changes a
rule, a task or a score.

Option 2, his patterns (the last 28 local days):
- check-ins missed or left insufficient, by weekday and by part of the day;
- the reasons that repeat (exemption categories, the discreet "someone's around" answer, postponed tasks);
- the days that go badly (confirmed findings by weekday).

Option 3, what works on him: after each consequence (lines, swats instead, a written request, a penalty window), was the next week clean
(no confirmed finding, no missed check-in)? Shown per kind. Praise and teasing are not measured: nothing in the database links a reply's
tone to what he did next (a later slice could, if the owner wants it).

A pattern is only called one with at least MIN_SAMPLES cases; below that the numbers are shown and nothing is proposed.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable

__all__ = ["patterns_report", "DAYS", "MIN_SAMPLES"]

DAYS = 28
MIN_SAMPLES = 3
CLEAN_AFTER = timedelta(days=7)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_PARTS = ((0, 9, "morning (before 9)"), (9, 13, "late morning (9-13)"), (13, 17, "afternoon (13-17)"), (17, 21, "evening (17-21)"),
          (21, 24, "night (21-24)"))
_MISS = ("missed", "insufficient")
_CONSEQUENCE = {None: "lines", "swats": "swats instead", "request": "a written request"}


def _t(value) -> datetime | None:
    if not value:
        return None
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _part(hour: int) -> str:
    return next(label for start, end, label in _PARTS if start <= hour < end)


def patterns_report(core, user_id: str, now: datetime, local: Callable[[datetime], datetime], *, counts_from: datetime | None = None) -> str:
    """`counts_from`: nothing before it is read (owner, 2026-11-17: the test lock before Fri 13 Nov does not count, as in supervision)."""
    since = max(now - timedelta(days=DAYS), counts_from) if counts_from else now - timedelta(days=DAYS)
    s = since.isoformat()
    with core.transaction() as tx:
        check_ins = tx.fetch_all("SELECT due_at, status FROM check_ins WHERE user_id = ? AND due_at >= ? AND status IN "
                                 "('answered', 'missed', 'insufficient')", (user_id, s))
        exemptions = tx.fetch_all("SELECT category, kind FROM wear_exemptions WHERE user_id = ? AND created_at >= ?", (user_id, s))
        tasks = tx.fetch_all("SELECT mode, postponed_at, status, outcome FROM obedience_tasks WHERE user_id = ? AND created_at >= ? "
                             "AND catalog_key NOT LIKE 'training:%'", (user_id, s))
        confirmed = tx.fetch_all("SELECT kind, decided_at FROM infraction_signals WHERE user_id = ? AND status = 'confirmed' AND decided_at >= ?",
                                 (user_id, s))
        all_misses = tx.fetch_all("SELECT due_at FROM check_ins WHERE user_id = ? AND status IN ('missed', 'insufficient') AND due_at >= ?",
                                  (user_id, s))
        lines = tx.fetch_all("SELECT mode, created_at FROM line_assignments WHERE user_id = ? AND created_at >= ?", (user_id, s))
        windows = tx.fetch_all("SELECT created_at FROM penalty_windows WHERE created_at >= ?", (s,))
    out = [f"Patterns since {local(since):%a %d %b} (at most {DAYS} days; read-only; a finding is a proposal for the owner, never a change).", ""]
    proposals: list[str] = []

    # ---- option 2: check-ins
    due_day, miss_day, due_part, miss_part = Counter(), Counter(), Counter(), Counter()
    for r in check_ins:
        t = local(_t(r["due_at"]))
        day, part = _WEEKDAYS[t.weekday()], _part(t.hour)
        due_day[day] += 1
        due_part[part] += 1
        if r["status"] in _MISS:
            miss_day[day] += 1
            miss_part[part] += 1
    out.append(f"Check-ins: {sum(miss_day.values())} of {len(check_ins)} missed or insufficient.")
    for label, due, miss in (("By weekday", due_day, miss_day), ("By part of the day", due_part, miss_part)):
        keys = [d for d in _WEEKDAYS if d in due] if label == "By weekday" else [p for *_x, p in _PARTS if p in due]
        out.append(f"  {label}: " + (", ".join(f"{k} {miss[k]}/{due[k]}" for k in keys) or "none"))
        for k in keys:
            if miss[k] >= MIN_SAMPLES and miss[k] * 2 >= due[k]:
                where = f"on {k}" if label == "By weekday" else f"in the {k}"
                proposals.append(f"Check-ins {where} are often missed ({miss[k]} of {due[k]}): something fixed there every week? "
                                 f"Ask him, then move that slot or keep it as it is.")

    # ---- option 2: reasons that repeat
    reasons = Counter(r["category"] for r in exemptions if r["kind"] == "exemption" and r["category"])
    discreet = sum(1 for r in tasks if r["mode"] == "discreet")
    postponed = sum(1 for r in tasks if r["postponed_at"])
    out.append("")
    out.append("Reasons used: exemptions " + (", ".join(f"{k} {n}" for k, n in reasons.most_common()) or "none")
               + f"; \"someone's around\" on {discreet} of {len(tasks)} tasks; {postponed} task(s) postponed.")
    for k, n in reasons.items():
        if n >= MIN_SAMPLES:
            proposals.append(f"The \"{k}\" exemption came {n} times in {DAYS} days: worth asking whether it is planned (then a standing "
                             f"arrangement) or a habit (then a look at it with him).")
    if discreet >= MIN_SAMPLES and discreet * 2 >= len(tasks):
        proposals.append(f"\"Someone's around\" on {discreet} of {len(tasks)} tasks: ask him when he is really alone and plan tasks there.")

    # ---- option 2: bad days
    bad = Counter(_WEEKDAYS[local(_t(r["decided_at"])).weekday()] for r in confirmed if r["decided_at"])
    out.append("Confirmed findings by weekday: " + (", ".join(f"{d} {bad[d]}" for d in _WEEKDAYS if bad[d]) or "none"))
    for d, n in bad.items():
        if n >= MIN_SAMPLES:
            proposals.append(f"{d} has {n} confirmed findings: ask him what that day looks like.")

    # ---- option 3: what works on him
    bad_times = [_t(r["decided_at"]) for r in confirmed if r["decided_at"]] + [_t(r["due_at"]) for r in all_misses]
    events = [(_CONSEQUENCE.get(r["mode"], "lines"), _t(r["created_at"])) for r in lines] + [("a penalty window", _t(r["created_at"])) for r in windows]
    after: dict[str, list[bool]] = {}
    for kind, at in events:
        if at + CLEAN_AFTER > now:
            continue                                                  # its week is not over yet
        after.setdefault(kind, []).append(not any(at < b <= at + CLEAN_AFTER for b in bad_times))
    out.append("")
    out.append("What came before a clean week (no confirmed finding, no missed check-in in the 7 days after):")
    if not after:
        out.append("  nothing to compare yet")
    for kind, clean in sorted(after.items()):
        out.append(f"  after {kind}: {sum(clean)} of {len(clean)} weeks clean")
    measured = {k: sum(v) / len(v) for k, v in after.items() if len(v) >= MIN_SAMPLES}
    if len(measured) >= 2:
        best = max(measured, key=measured.get)
        worst = min(measured, key=measured.get)
        if measured[best] - measured[worst] >= 0.3:
            proposals.append(f"A clean week follows {best} more often than {worst} ({measured[best]:.0%} vs {measured[worst]:.0%}): "
                             f"propose leaning on {best}.")
    out.append("  praise and teasing: not measured")

    out.append("")
    out.append("Proposals:" if proposals else f"Proposals: none yet (a pattern needs at least {MIN_SAMPLES} cases).")
    out.extend(f"- {p}" for p in proposals)
    return "\n".join(out)
