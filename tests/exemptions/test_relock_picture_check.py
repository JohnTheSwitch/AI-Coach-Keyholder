"""Owner, 2026-11-15: after a planned hygiene opening, the relock picture is due within 30 minutes (while it is required); none = asked to
explain. A health opening is never checked; an unreadable Chaster never counts against him."""

from __future__ import annotations

from datetime import timedelta

from exemptions.service import RELOCK_PICTURE_GIVE_UP, RELOCK_PICTURE_WITHIN
from infractions.signals import RULE_GROUPS, SignalKind
from lines.service import sentence_for
from penalty_engine.repository import WARNING_ONLY_RULE_GROUPS
from tests.exemptions.test_exemptions import WEARER
from tests.exemptions.test_hygiene_requests import World
from tests.system.test_penalty_pipeline import NOW


def world(tmp_path, *, found=False, required=True, kind="hygiene"):
    w = World(tmp_path)
    w.asked, w.raised = [], []
    w.service.relock_pictures = lambda start, end: (w.asked.append((start, end)) or found)
    w.service.picture_required = lambda at: required
    w.service.raise_missing_picture = lambda row, now: w.raised.append(row["id"])
    with w.service._core.transaction() as tx:
        tx.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at, kind, minutes, relocked_at) VALUES ('h', ?, 'ended', ?, ?, 15, ?)",
                   (WEARER, NOW.isoformat(), kind, NOW.isoformat()))
    return w


def outcome(w):
    return w.q("SELECT picture_outcome FROM wear_exemptions WHERE id = 'h'")[0]["picture_outcome"]


def test_the_rule() -> None:
    assert RELOCK_PICTURE_WITHIN == timedelta(minutes=30)
    assert RULE_GROUPS[SignalKind.RELOCK_PICTURE_MISSING] == "HYGIENE_PICTURE_MISSING" and "HYGIENE_PICTURE_MISSING" in WARNING_ONLY_RULE_GROUPS
    assert sentence_for("HYGIENE_PICTURE_MISSING") == "I send my relock picture every time."


def test_no_picture_within_30_minutes_asks_him(tmp_path) -> None:
    w = world(tmp_path)
    w.service.tick(NOW + timedelta(minutes=20))
    assert w.asked == [] and outcome(w) is None                          # not yet due
    w.service.tick(NOW + timedelta(minutes=31))
    assert w.asked == [(NOW - timedelta(minutes=5), NOW + RELOCK_PICTURE_WITHIN)] and outcome(w) == "missing" and w.raised == ["h"]
    w.service.tick(NOW + timedelta(minutes=40))
    assert w.raised == ["h"]                                              # decided once


def test_a_picture_sent_or_not_required_is_fine(tmp_path) -> None:
    for i, kw in enumerate(({"found": True}, {"required": False}, {"kind": "hygiene_health", "found": True})):
        (tmp_path / str(i)).mkdir()
        w = world(tmp_path / str(i), **kw)
        w.service.tick(NOW + timedelta(minutes=31))
        assert w.raised == [] and outcome(w) == {0: "sent", 1: "not_required", 2: "sent"}[i]


def test_a_health_opening_is_checked_too(tmp_path) -> None:
    """G2 (owner, 2026-11-15): the relock picture is checked after a health opening as well; it restricts nothing, and a health
    explanation is excused by code on the infractions path."""
    w = world(tmp_path, kind="hygiene_health")
    w.service.tick(NOW + timedelta(minutes=31))
    assert len(w.raised) == 1


def test_unknown_never_counts(tmp_path) -> None:
    w = world(tmp_path, found=None)
    w.service.tick(NOW + timedelta(minutes=31))
    assert outcome(w) is None and w.raised == []
    w.service.tick(NOW + RELOCK_PICTURE_GIVE_UP + timedelta(minutes=1))
    assert outcome(w) == "unknown" and w.raised == []
