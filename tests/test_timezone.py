"""Times follow this computer unless Setup pins another zone."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from controller_inbox.clock import (
    _WINDOWS_TO_IANA,
    apply_saved_timezone,
    computer_timezone,
    effective_timezone,
    format_when,
    is_timezone,
    offset_label,
    set_timezone,
    timezone_groups,
)
from controller_inbox.config import Settings
from controller_inbox.web import create_app

STAMP = "2026-09-22T16:00:00+00:00"


def test_the_computer_zone_is_a_real_zone():
    assert is_timezone(computer_timezone())
    assert effective_timezone("auto") == computer_timezone()
    assert effective_timezone("") == computer_timezone()
    assert effective_timezone("not-a-zone") == computer_timezone()
    assert effective_timezone("America/Chicago") == "America/Chicago"


def test_windows_names_map_to_zones_that_exist():
    assert _WINDOWS_TO_IANA["Eastern Standard Time"] == "America/New_York"
    assert _WINDOWS_TO_IANA["Central Standard Time"] == "America/Chicago"
    assert _WINDOWS_TO_IANA["Pacific Standard Time"] == "America/Los_Angeles"
    for name in _WINDOWS_TO_IANA.values():
        assert is_timezone(name), name


def test_a_utc_stamp_is_shown_in_the_chosen_zone():
    assert format_when(STAMP, ZoneInfo("America/New_York")) == "Sep 22 · 12:00"
    assert format_when(STAMP, ZoneInfo("America/Chicago")) == "Sep 22 · 11:00"
    assert format_when(STAMP, ZoneInfo("America/Los_Angeles")) == "Sep 22 · 09:00"
    assert format_when("2026-09-22T16:00:00", ZoneInfo("America/Chicago")) == "Sep 22 · 11:00"
    assert format_when("", ZoneInfo("UTC")) == ""


def test_auto_is_the_default_and_a_bad_name_falls_back(tmp_path):
    assert Settings(_env_file=None).timezone == "auto"
    assert Settings(timezone="Nope", _env_file=None).timezone == "auto"
    pinned = Settings(data_dir=tmp_path, timezone="America/Denver", _env_file=None)
    assert pinned.tz == ZoneInfo("America/Denver")


def test_a_saved_choice_wins_and_auto_follows_the_computer(settings, store):
    set_timezone(store, settings, "America/Chicago")
    assert settings.tz == ZoneInfo("America/Chicago")
    fresh = Settings(data_dir=settings.data_dir, inbox_dir=settings.inbox_dir, timezone="America/New_York", _env_file=None)
    apply_saved_timezone(fresh, store)
    assert fresh.timezone == "America/Chicago"

    set_timezone(store, settings, "auto")
    assert settings.timezone == "auto"
    assert settings.tz == ZoneInfo(computer_timezone())
    try:
        set_timezone(store, settings, "Mars/Olympus")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown zone should be refused")


def test_setup_shows_the_computer_zone_and_a_saved_zone_changes_the_clock(settings, store):
    store.set_state("last_overnight_at", STAMP)
    client = TestClient(create_app(settings, store))
    page = client.get("/settings")
    assert page.status_code == 200
    assert "This computer" in page.text
    assert "America/New_York" in page.text
    assert 'value="America/New_York" selected' in page.text
    assert "Sep 22 · 12:00" in client.get("/").text

    saved = client.post("/settings/timezone", data={"zone": "America/Los_Angeles"}, follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"].endswith("#timezone")
    follow = client.get("/settings?notice=timezone")
    assert "Saved. Times" in follow.text
    assert 'value="America/Los_Angeles" selected' in follow.text
    assert "Sep 22 · 09:00" in client.get("/").text
    assert "Sep 22 · 12:00" not in client.get("/").text

    back = client.post("/settings/timezone", data={"zone": "auto"}, follow_redirects=True)
    assert back.status_code == 200
    assert 'value="auto" selected' in back.text
    shown = format_when(STAMP, ZoneInfo(computer_timezone()))
    assert shown in client.get("/").text
    assert client.post("/settings/timezone", data={"zone": "Mars/Olympus"}).status_code == 400


def test_offsets_west_of_greenwich_are_negative():
    winter = datetime(2026, 1, 15, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert offset_label(ZoneInfo("Atlantic/Cape_Verde"), winter) == "UTC-1"
    assert offset_label(ZoneInfo("America/New_York"), winter) == "UTC-5"
    assert offset_label(ZoneInfo("America/New_York"), summer) == "UTC-4"
    assert offset_label(ZoneInfo("America/Chicago"), summer) == "UTC-5"
    assert offset_label(ZoneInfo("Europe/Paris"), winter) == "UTC+1"
    assert offset_label(ZoneInfo("Asia/Kolkata"), winter) == "UTC+5:30"
    assert offset_label(ZoneInfo("UTC"), winter) == "UTC+0"
    labels = [label for label, _zones in timezone_groups(winter)]
    assert labels.index("UTC-1") < labels.index("UTC+0") < labels.index("UTC+1")
    cape_verde = dict(dict(timezone_groups(winter))["UTC-1"])
    assert cape_verde["Atlantic/Cape_Verde"] == "(UTC-1) Atlantic/Cape_Verde"


def test_setup_shows_the_offset_as_a_minus_when_the_zone_is_behind_utc(settings, store):
    client = TestClient(create_app(settings, store))
    page = client.get("/settings").text
    label = offset_label(ZoneInfo("America/New_York"))
    assert label.startswith("UTC-")
    assert 'value="America/New_York" selected' in page
    assert f"({label}) America/New_York" in page
    assert "Showing <b>" in page and label in page


def test_a_stamp_with_z_matches_the_offset_form():
    zulu = datetime(2026, 1, 15, 18, 30, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert format_when(zulu, ZoneInfo("America/New_York")) == "Jan 15 · 13:30"
