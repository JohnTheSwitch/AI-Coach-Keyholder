"""Multi-wearer parity, slice 2 (owner, 2026-11-13): an additional wearer's own time zone and public holidays."""

from __future__ import annotations

from pathlib import Path

from system.composition import window_config_from
from system.tenant_factory import tenant_config
from system.tenant_gateway import TEXTS
from system.wearer_locale import WearerLocale, WearerLocaleStore, normalize_country, valid_timezone
from tests.system.test_tenant_gateway import OWNER_DISCORD, G


def test_time_zones_are_canonical_iana_names() -> None:
    assert valid_timezone("Europe/Berlin") == "Europe/Berlin"
    assert valid_timezone(" europe/berlin ") == "Europe/Berlin"
    assert valid_timezone("utc") == "UTC"
    for bad in ("CET", "UTC+1", "Etc/GMT-1", "Mars/Olympus", "", "x" * 80, None):
        assert valid_timezone(bad) is None


def test_country_codes() -> None:
    assert normalize_country("de") == "DE" and normalize_country("none") == "" and normalize_country("ZZ") is None


def test_tenant_config_uses_the_wearers_settings_and_falls_back_to_the_owners(tmp_path) -> None:
    import dataclasses
    from tests.system.test_tenant_factory import World
    base = dataclasses.replace(World(tmp_path).factory._shared.config, app_timezone="Europe/Prague", holiday_country="CZ")
    plain = tenant_config(base, db_path=Path(tmp_path / "t.sqlite"), wearer_user_id="u")
    assert (plain.app_timezone, plain.holiday_country) == ("Europe/Prague", "CZ")
    own = tenant_config(base, db_path=Path(tmp_path / "t.sqlite"), wearer_user_id="u", locale=WearerLocale("America/New_York", "US"))
    assert (own.app_timezone, own.holiday_country) == ("America/New_York", "US")
    none = tenant_config(base, db_path=Path(tmp_path / "t.sqlite"), wearer_user_id="u", locale=WearerLocale(None, ""))
    assert (none.app_timezone, none.holiday_country) == ("Europe/Prague", "")
    assert window_config_from(own).tz.key == "America/New_York"
    assert window_config_from(none).holiday_country is None


class TestWearerCommands:
    def test_a_wearer_sets_their_zone_and_holidays_and_the_rebuilt_runtime_uses_them(self, tmp_path) -> None:
        g = G(tmp_path)
        tenant = g.member()
        assert "not set" in g.say("2000002", "timezone").text
        assert g.say("2000002", "timezone Mars/Olympus").text == TEXTS["timezone_invalid"]
        assert g.say("2000002", "Timezone America/New_York").text == TEXTS["timezone_set"].format("America/New_York")
        assert tenant.id not in g.supervisor.runtimes                           # dropped, rebuilt on the next reconcile / message
        assert g.say("2000002", "holidays zz").text == TEXTS["holidays_invalid"]
        assert g.say("2000002", "holidays us").text == TEXTS["holidays_set"].format("US")
        assert "America/New_York" in g.say("2000002", "timezone").text and "US" in g.say("2000002", "holidays").text
        runtime = g.w.factory.build(tenant.id)
        assert WearerLocaleStore(runtime.core).get() == WearerLocale("America/New_York", "US")
        assert g.say("2000002", "holidays none").text == TEXTS["holidays_set"].format("none")
        assert g.say("2000002", "holidays").text == TEXTS["holidays_show"].format("none")
        assert g.zero_calls == []

    def test_the_owner_is_pointed_to_the_configuration(self, tmp_path) -> None:
        g = G(tmp_path)
        assert g.say(OWNER_DISCORD, "timezone Europe/Berlin").text == TEXTS["locale_owner"]
        assert g.zero_calls == []

    def test_longer_sentences_still_reach_the_conversation(self, tmp_path) -> None:
        g = G(tmp_path)
        g.say(OWNER_DISCORD, "timezone is confusing me today")
        assert g.zero_calls == ["timezone is confusing me today"]
