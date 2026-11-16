"""Proposal A (owner, 2026-11-16): "what is holding my release?" answered from facts, never by the model, never with a score."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schedule_windows.windows import WindowConfig
from system.release_status import ReleaseStatus, is_release_question
from tests.supervision.test_supervision import core  # noqa: F401

NOW = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)
CFG = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00", work_start="08:00", work_end="15:00",
                         holiday_country=None, weekend_release_start="10:00")


def status(held=(), tokens=1, end=NOW + timedelta(days=2, hours=16)):
    config = CFG
    return ReleaseStatus(user_id="u", holds=lambda: (list(held), tokens), supervision_domains=lambda: ["routine"] if "supervision_active" in held else [],
                         supervision_end=lambda d, now, future: end, window_config=config)


def test_the_questions_it_answers() -> None:
    for q in ("What is holding my release?", "what's holding my lock", "Why am I not unlocked?", "Am I under supervision?",
              "When will you unlock me?", "When does supervision end?", "co mi drží uvolnění?",
              "When will I get unlocked?", "Ok, why am I still locked?", "Why aren't I unlocked yet?", "Can I get released today?",
              "What is stopping me from being released?"):                 # the first two: live, 2026-11-16 19:32
        assert is_release_question(q), q
    for q in ("I like my lock", "what is holding my release and also tell me a story about dragons please", "hello"):
        assert not is_release_question(q), q


def test_supervision_with_a_token_ready_and_an_estimate() -> None:
    reply = status(held=["supervision_active"]).answer("u", "What is holding my release?", NOW)
    assert "Supervision" in reply.text and "Thursday morning" in reply.text and "a token ready" in reply.text
    assert "0." not in reply.text                                                    # never a score


def test_every_hold_and_nothing_held() -> None:
    reply = status(held=["penalty_window_active", "supervision_active", "release_debt"], tokens=0, end=None).answer("u", "why am i still locked", NOW)
    assert "penalty window" in reply.text and "Lines you owe" in reply.text and "every miss pushes it back" in reply.text
    assert "don't have a token" in reply.text
    free = status().answer("u", "What is holding my release?", NOW)
    assert free.text.startswith("Nothing holds your release") and "earn it on the day" in free.text


def test_only_the_owner_and_only_the_question() -> None:
    s = status(held=["supervision_active"])
    assert s.answer("someone else", "What is holding my release?", NOW) is None and s.answer("u", "hello", NOW) is None


def test_the_estimate_from_real_evidence(tmp_path, core) -> None:  # noqa: F811
    """The live shape of 16 Nov: misses up to 12 Nov, answers since; supervision would end once the misses leave the week."""
    from supervision.repository import SupervisionService
    from tests.supervision.test_short_term import bad, good
    from supervision.models import CHECK_IN_DOMAIN_WINDOW
    db = core
    for days in (6, 5, 4.5, 4):
        bad(db, NOW - timedelta(days=days), domain="d")
    for hours in (60, 40, 20, 5):
        good(db, NOW - timedelta(hours=hours), domain="d")
    svc = SupervisionService(tmp_path / "x.db", core=db, windows={"d": CHECK_IN_DOMAIN_WINDOW})
    future = [NOW + timedelta(hours=6 * k) for k in range(1, 40)]
    end = svc.get_estimated_end("d", NOW, future)
    assert end is not None and end == NOW + timedelta(days=2)       # the miss of 5 days ago leaves the week: 2 bad (0.12) vs 12 answers (0.24)
    assert svc.get_estimated_end("d", NOW, []) is None
