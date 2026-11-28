"""Owner, 2026-11-25: the model-down alert (system/model_watch.py)."""

from datetime import datetime, timedelta, timezone

from system.model_watch import ModelWatch, start_ollama_task, DOWN_TEXT, BACK_TEXT

T0 = datetime(2026, 11, 25, 6, 25, tzinfo=timezone.utc)


def _watch(answers, health=None):
    told, restarts = [], []
    w = ModelWatch(probe=lambda: next(answers), notify=lambda key, text: told.append((key, text)), restart=lambda: restarts.append(1) or "started",
                   health=health or (lambda: {}))
    return w, told, restarts


def test_down_30_minutes_restarts_once_tells_him_once_and_says_when_it_is_back() -> None:
    w, told, restarts = _watch(iter([False] * 10 + [True]))
    for minutes in range(0, 50, 5):
        assert w.check(T0 + timedelta(minutes=minutes)) is False
    assert restarts == [1]                                              # once, after 15 minutes
    assert [t for _k, t in told] == [DOWN_TEXT]                         # once, after 30 minutes
    assert w.check(T0 + timedelta(minutes=50)) is True
    assert [t for _k, t in told] == [DOWN_TEXT, BACK_TEXT]


def test_a_short_outage_says_nothing() -> None:
    w, told, restarts = _watch(iter([False, False, True]))
    w.check(T0)
    w.check(T0 + timedelta(minutes=5))
    assert w.check(T0 + timedelta(minutes=10)) is True and told == [] and restarts == []


def test_recent_real_calls_mean_no_probe() -> None:
    def never():
        raise AssertionError("probed")
        yield

    w, told, _ = _watch(never(), health=lambda: {"ok_at": T0 - timedelta(minutes=3), "error_at": None})
    assert w.check(T0) is True
    w, told, _ = _watch(never(), health=lambda: {"ok_at": T0 - timedelta(hours=2), "error_at": T0 - timedelta(minutes=1)})
    assert w.check(T0) is False                                         # a real call just failed


def test_ollama_is_started_only_when_nothing_listens() -> None:
    ran = []

    class Done:
        returncode = 0

    assert start_ollama_task(run=lambda *a, **k: ran.append(a) or Done(), listening=lambda h, p: True) == "running_but_failing" and ran == []
    assert start_ollama_task(run=lambda *a, **k: ran.append(a[0]) or Done(), listening=lambda h, p: False) == "started"
    assert ran == [["schtasks", "/Run", "/TN", "AI Coach Ollama"]]
