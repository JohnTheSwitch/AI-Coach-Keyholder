"""Honesty and proof, slice 4 (owner, 2026-11-13): a kept bedtime commitment contradicted by messages that night becomes a question."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from core.config import Config
from infractions.signals import RULE_GROUPS, SignalKind
from proof_checks.contradictions import BedtimeContradictions, stated_bedtime
from tests.coach.test_desk import World

DAY = date(2026, 11, 13)


def utc(d: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(d, time(h, m), tzinfo=timezone.utc)


MORNING = utc(DAY + timedelta(days=1), 7)


@pytest.mark.parametrize("text, expected", [("Sleep by 23:00", (23, 0)), ("In bed before 11 pm", (23, 0)), ("Be in bed by 11", (23, 0)),
                                            ("Lights out at 10:30", (22, 30)), ("Sleep by 00:30", (0, 30)), ("Go to bed 30 minutes earlier", None)])
def test_the_stated_bedtime(text, expected) -> None:
    assert stated_bedtime(text) == expected


class C:
    def __init__(self, tmp_path, *, kept, messages) -> None:
        self.w = World(tmp_path)
        self.asked: list[dict] = []
        self.c = BedtimeContradictions(
            self.w.core, user_id="wearer", honesty_domain="honesty", kept_night=lambda since: kept,
            messages_between=lambda start, end: [(t, x) for t, x in messages if start <= t < end],
            to_utc=lambda d, t: datetime.combine(d, t, tzinfo=timezone.utc), morning_after=lambda d: utc(d + timedelta(days=1), 7),
            today=lambda now: now.date(), local_clock=lambda i: i.strftime("%H:%M"), raise_question=lambda **kw: self.asked.append(kw) or True)

    def outcomes(self):
        return [(r["commitment_id"], r["outcome"]) for r in self.w.rows("SELECT * FROM contradiction_checks")]


def kept(cid, text):
    return {"id": cid, "for_date": DAY.isoformat(), "text": text, "resolved_at": None}


def test_a_message_after_the_bedtime_is_asked_about_once(tmp_path) -> None:
    c = C(tmp_path, kept=[kept("c1", "Sleep by 23:00")], messages=[(utc(DAY, 22, 50), "night"), (utc(DAY, 23, 20), "still up")])
    assert c.c.run(utc(DAY, 23, 30)) == 0 and c.asked == []             # the night is not over yet
    assert c.c.run(MORNING) == 1
    assert "at 23:20 that night, after 23:00" in c.asked[0]["wearer_text"] and c.outcomes() == [("c1", "found")]
    assert c.c.run(MORNING + timedelta(hours=1)) == 0 and len(c.asked) == 1


def test_within_the_grace_or_a_health_message_never_counts(tmp_path) -> None:
    c = C(tmp_path, kept=[kept("c1", "Sleep by 23:00")], messages=[(utc(DAY, 23, 10), "ok"), (utc(DAY + timedelta(days=1), 2), "my back hurts badly")])
    c.c.run(MORNING)
    assert c.asked == [] and c.outcomes() == [("c1", "none")]


def test_a_bedtime_without_a_time_cannot_be_contradicted(tmp_path) -> None:
    c = C(tmp_path, kept=[kept("c1", "Go to bed 30 minutes earlier")], messages=[(utc(DAY, 23, 59), "hi")])
    c.c.run(MORNING)
    assert c.asked == [] and c.outcomes() == [("c1", "no_time")]


def test_a_contradiction_is_warning_only_in_honesty() -> None:
    assert RULE_GROUPS[SignalKind.CONTRADICTION] == "FALSE_REPORT"


def test_lock_claims_are_filed_under_honesty_by_default_but_not_for_additional_wearers(tmp_path) -> None:
    from system.tenant_factory import tenant_config
    from tests.system.test_tenant_factory import World as TenantWorld
    assert Config.lock_claim_trust_domain == "honesty"
    base = TenantWorld(tmp_path).factory._shared.config
    assert tenant_config(base, db_path=tmp_path / "t.sqlite", wearer_user_id="u").lock_claim_trust_domain == "routine"


def test_the_desk_lists_kept_bedtime_commitments_only(tmp_path) -> None:
    from coach.desk import Outcome
    w = World(tmp_path)
    for text in ("Sleep by 23:00", "Study for 30 minutes"):
        r = w.desk.submit("wearer", {"action": "propose_commitment", "text": text, "when": "tomorrow"}, utc(DAY, 9))
        w.desk.decide("wearer", r.proposal.id, accept=True, now=utc(DAY, 9))
    for row in w.rows("SELECT id FROM daily_commitments"):
        w.desk.record_outcome("wearer", row["id"], Outcome.KEPT, utc(DAY + timedelta(days=1), 22))
    assert [r["text"] for r in w.desk.get_kept_night_commitments(DAY)] == ["Sleep by 23:00"]
