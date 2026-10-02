"""The clock CloseDesk uses: this computer's time zone, unless Setup picks another.

Mail is stored in UTC. "Today", due dates, and the times on screen use the zone resolved here.
``auto`` follows the computer, so a laptop that changes time zone is picked up on the next page.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

AUTO = "auto"
STATE_KEY = "timezone"

# Windows TimeZoneKeyName -> IANA. The names are the ones ZoneInfo ships.
_WINDOWS_TO_IANA = {
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC-11": "Etc/GMT+11",
    "Aleutian Standard Time": "America/Adak",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Marquesas Standard Time": "Pacific/Marquesas",
    "Alaskan Standard Time": "America/Anchorage",
    "UTC-09": "Etc/GMT+9",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "UTC-08": "Etc/GMT+8",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Mazatlan",
    "Mountain Standard Time": "America/Denver",
    "Yukon Standard Time": "America/Whitehorse",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Easter Island Standard Time": "Pacific/Easter",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "Eastern Standard Time": "America/New_York",
    "Haiti Standard Time": "America/Port-au-Prince",
    "Cuba Standard Time": "America/Havana",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Turks And Caicos Standard Time": "America/Grand_Turk",
    "Paraguay Standard Time": "America/Asuncion",
    "Atlantic Standard Time": "America/Halifax",
    "Venezuela Standard Time": "America/Caracas",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns",
    "Tocantins Standard Time": "America/Araguaina",
    "E. South America Standard Time": "America/Sao_Paulo",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Greenland Standard Time": "America/Nuuk",
    "Montevideo Standard Time": "America/Montevideo",
    "Magallanes Standard Time": "America/Punta_Arenas",
    "Saint Pierre Standard Time": "America/Miquelon",
    "Bahia Standard Time": "America/Bahia",
    "UTC-02": "Etc/GMT+2",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "Jordan Standard Time": "Asia/Amman",
    "GTB Standard Time": "Europe/Bucharest",
    "Middle East Standard Time": "Asia/Beirut",
    "Egypt Standard Time": "Africa/Cairo",
    "E. Europe Standard Time": "Europe/Chisinau",
    "Syria Standard Time": "Asia/Damascus",
    "West Bank Standard Time": "Asia/Hebron",
    "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kyiv",
    "Israel Standard Time": "Asia/Jerusalem",
    "South Sudan Standard Time": "Africa/Juba",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Sudan Standard Time": "Africa/Khartoum",
    "Libya Standard Time": "Africa/Tripoli",
    "Namibia Standard Time": "Africa/Windhoek",
    "Arabic Standard Time": "Asia/Baghdad",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arab Standard Time": "Asia/Riyadh",
    "Belarus Standard Time": "Europe/Minsk",
    "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Astrakhan Standard Time": "Europe/Astrakhan",
    "Azerbaijan Standard Time": "Asia/Baku",
    "Russia Time Zone 3": "Europe/Samara",
    "Mauritius Standard Time": "Indian/Mauritius",
    "Saratov Standard Time": "Europe/Saratov",
    "Georgian Standard Time": "Asia/Tbilisi",
    "Volgograd Standard Time": "Europe/Volgograd",
    "Caucasus Standard Time": "Asia/Yerevan",
    "Afghanistan Standard Time": "Asia/Kabul",
    "West Asia Standard Time": "Asia/Tashkent",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "Pakistan Standard Time": "Asia/Karachi",
    "Qyzylorda Standard Time": "Asia/Qyzylorda",
    "India Standard Time": "Asia/Kolkata",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Kathmandu",
    "Central Asia Standard Time": "Asia/Almaty",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Omsk Standard Time": "Asia/Omsk",
    "Myanmar Standard Time": "Asia/Yangon",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Altai Standard Time": "Asia/Barnaul",
    "W. Mongolia Standard Time": "Asia/Hovd",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Tomsk Standard Time": "Asia/Tomsk",
    "China Standard Time": "Asia/Shanghai",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "Singapore Standard Time": "Asia/Singapore",
    "W. Australia Standard Time": "Australia/Perth",
    "Taipei Standard Time": "Asia/Taipei",
    "Ulaanbaatar Standard Time": "Asia/Ulaanbaatar",
    "Aus Central W. Standard Time": "Australia/Eucla",
    "Transbaikal Standard Time": "Asia/Chita",
    "Tokyo Standard Time": "Asia/Tokyo",
    "North Korea Standard Time": "Asia/Pyongyang",
    "Korea Standard Time": "Asia/Seoul",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
    "Tasmania Standard Time": "Australia/Hobart",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Lord Howe Standard Time": "Australia/Lord_Howe",
    "Bougainville Standard Time": "Pacific/Bougainville",
    "Russia Time Zone 10": "Asia/Srednekolymsk",
    "Magadan Standard Time": "Asia/Magadan",
    "Norfolk Standard Time": "Pacific/Norfolk",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Central Pacific Standard Time": "Pacific/Guadalcanal",
    "Russia Time Zone 11": "Asia/Kamchatka",
    "New Zealand Standard Time": "Pacific/Auckland",
    "UTC+12": "Etc/GMT-12",
    "Fiji Standard Time": "Pacific/Fiji",
    "Chatham Islands Standard Time": "Pacific/Chatham",
    "UTC+13": "Etc/GMT-13",
    "Tonga Standard Time": "Pacific/Tongatapu",
    "Samoa Standard Time": "Pacific/Apia",
    "Line Islands Standard Time": "Pacific/Kiritimati",
}


def is_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True


def computer_timezone() -> str:
    """IANA name of this computer's time zone. ``UTC`` when it cannot be named."""
    found = _named_zone()
    if found in {"Etc/UTC", "Etc/GMT", "GMT"}:
        return "UTC"
    return found if found and is_timezone(found) else "UTC"


def effective_timezone(choice: str | None) -> str:
    """``auto`` or a blank choice follows the computer. Anything else must be an IANA name."""
    text = (choice or "").strip()
    if not text or text.lower() == AUTO:
        return computer_timezone()
    return text if is_timezone(text) else computer_timezone()


def apply_saved_timezone(settings, store) -> None:
    """The Setup page choice wins over the environment. Blank means leave the environment as it is."""
    saved = (store.get_state(STATE_KEY) or "").strip()
    if not saved:
        return
    settings.timezone = saved if saved.lower() == AUTO or is_timezone(saved) else AUTO


def set_timezone(store, settings, choice: str) -> None:
    """Remember ``auto`` or an IANA name, and use it on this running copy."""
    text = (choice or "").strip()
    if not text or text.lower() == AUTO:
        store.set_state(STATE_KEY, AUTO)
        settings.timezone = AUTO
        return
    if not is_timezone(text):
        raise ValueError(f"Unknown time zone {text!r}")
    store.set_state(STATE_KEY, text)
    settings.timezone = text


def offset_label(zone: ZoneInfo, when: datetime | None = None) -> str:
    """How far the zone is from UTC, the way people say it: ``UTC-1``, ``UTC+0``, ``UTC+5:30``.

    West of Greenwich is negative. This is not the ``Etc/GMT`` name, which flips the sign.
    """
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return _offset_text(moment.astimezone(zone).utcoffset() or timedelta(0))


def format_when(value: str, zone: ZoneInfo | None = None) -> str:
    """``Sep 22 · 16:00`` in ``zone``. A stamp with no offset is UTC, which is how mail is stored."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if zone is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(zone)
    return parsed.strftime("%b %d · %H:%M")


def timezone_groups(when: datetime | None = None) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Zones grouped by their offset at ``when``, from UTC-12 toward UTC+14.

    Each entry is ``(offset label, ((iana name, option label), ...))``. The option
    reads ``(UTC-1) Atlantic/Cape_Verde``, so a zone behind UTC is a minus, not an
    ``Etc/GMT`` plus.
    """
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    names = sorted(zone for zone in available_timezones() if "/" in zone and not zone.startswith("Etc/"))
    buckets: dict[timedelta, list[tuple[str, str]]] = {}
    for name in ["UTC", *names]:
        zone = ZoneInfo(name)
        delta = moment.astimezone(zone).utcoffset() or timedelta(0)
        buckets.setdefault(delta, []).append((name, f"({offset_label(zone, moment)}) {name}"))
    return tuple(
        (_offset_text(delta), tuple(sorted(rows, key=lambda item: item[1])))
        for delta, rows in sorted(buckets.items())
    )


def _offset_text(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    return f"UTC{sign}{hours}"


def _named_zone() -> str:
    forced = os.environ.get("TZ", "").strip()
    if forced and is_timezone(forced):
        return forced
    named = _from_localtime() or _from_windows()
    return named


def _from_localtime() -> str:
    path = Path("/etc/localtime")
    try:
        parts = path.resolve().parts
    except OSError:
        return ""
    if "zoneinfo" not in parts:
        return ""
    return "/".join(parts[parts.index("zoneinfo") + 1 :])


def _from_windows() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation"
        ) as key:
            windows_name, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except OSError:
        return ""
    return _WINDOWS_TO_IANA.get(str(windows_name), "")
