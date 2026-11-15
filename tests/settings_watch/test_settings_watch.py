"""G4 (owner, 2026-11-15): changed settings and the runtime halt are asked about and listed in the weekly review."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from infrastructure.time_format import iso
from settings_watch.service import SettingsWatch, config_values
from tests.lines.test_lines import core

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
U = "wearer"


def _config(quiet="22:00"):
    return SimpleNamespace(app_timezone="Europe/Prague", quiet_hours_start=quiet, quiet_hours_end="07:00", workday_start="07:00",
                           workday_end="16:30", holiday_country="CZ", weekend_release_start="09:00")


def test_first_sighting_is_remembered_then_changes_are_asked_and_listed(tmp_path) -> None:
    c = core(tmp_path)
    state = {"config": _config(), "halt": "off"}
    watch = SettingsWatch(c, user_id=U, values=lambda: {**config_values(state["config"]), "halt": state["halt"]})
    assert watch.tick(NOW) == [] and watch.tick(NOW + timedelta(minutes=5)) == []
    state["config"], state["halt"] = _config("23:30"), "on"
    assert sorted(watch.tick(NOW + timedelta(hours=1))) == ["halt", "quiet_hours"]
    with c.transaction() as tx:
        texts = [r["text"] for r in tx.fetch_all("SELECT text FROM notifications WHERE kind = 'settings_change'")]
    assert any("quiet hours changed from 22:00-07:00 to 23:30-07:00" in t and "tell me why" in t for t in texts)
    assert any("runtime halt was switched on" in t for t in texts)
    changes = watch.get_changes_between(iso(NOW), iso(NOW + timedelta(days=1)))
    assert {(ch["key"], ch["new"]) for ch in changes} == {("halt", "on"), ("quiet_hours", "23:30-07:00")}
    assert watch.tick(NOW + timedelta(hours=2)) == []                      # asked once per change


def test_the_weekly_review_lists_them() -> None:
    from datetime import date
    from weekly_review.service import WeekFacts, render_week
    text = render_week(WeekFacts(start=date(2026, 11, 9), end=date(2026, 11, 16), settings_changed=("your quiet hours 22:00-07:00 -> 23:30-07:00",)))
    assert "Settings changed: your quiet hours 22:00-07:00 -> 23:30-07:00." in text
