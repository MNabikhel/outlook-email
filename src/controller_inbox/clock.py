"""The clock CloseDesk uses: this computer's time zone, unless Setup picks another.

Mail is stored in UTC. "Today", due dates, and the times on screen use the zone resolved here.
``auto`` follows the computer, so a laptop that changes time zone is picked up on the next page.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

AUTO = "auto"
STATE_KEY = "timezone"

# The list Windows and Outlook show, by IANA name. The offset in front is worked out from the
# zone itself, so it stays right when a country changes its rules.
ZONES: tuple[tuple[str, str], ...] = (
    ("Etc/GMT+12", "International Date Line West"),
    ("Etc/GMT+11", "Coordinated Universal Time-11"),
    ("America/Adak", "Aleutian Islands"),
    ("Pacific/Honolulu", "Hawaii"),
    ("Pacific/Marquesas", "Marquesas Islands"),
    ("America/Anchorage", "Alaska"),
    ("Etc/GMT+9", "Coordinated Universal Time-09"),
    ("America/Tijuana", "Baja California"),
    ("Etc/GMT+8", "Coordinated Universal Time-08"),
    ("America/Los_Angeles", "Pacific Time (US & Canada)"),
    ("America/Phoenix", "Arizona"),
    ("America/Mazatlan", "La Paz, Mazatlan"),
    ("America/Denver", "Mountain Time (US & Canada)"),
    ("America/Whitehorse", "Yukon"),
    ("America/Guatemala", "Central America"),
    ("America/Chicago", "Central Time (US & Canada)"),
    ("Pacific/Easter", "Easter Island"),
    ("America/Mexico_City", "Guadalajara, Mexico City, Monterrey"),
    ("America/Regina", "Saskatchewan"),
    ("America/Bogota", "Bogota, Lima, Quito, Rio Branco"),
    ("America/Cancun", "Chetumal"),
    ("America/New_York", "Eastern Time (US & Canada)"),
    ("America/Port-au-Prince", "Haiti"),
    ("America/Havana", "Havana"),
    ("America/Indiana/Indianapolis", "Indiana (East)"),
    ("America/Grand_Turk", "Turks and Caicos"),
    ("America/Asuncion", "Asuncion"),
    ("America/Halifax", "Atlantic Time (Canada)"),
    ("America/Caracas", "Caracas"),
    ("America/Cuiaba", "Cuiaba"),
    ("America/La_Paz", "Georgetown, La Paz, Manaus, San Juan"),
    ("America/Santiago", "Santiago"),
    ("America/St_Johns", "Newfoundland"),
    ("America/Araguaina", "Araguaina"),
    ("America/Sao_Paulo", "Brasilia"),
    ("America/Cayenne", "Cayenne, Fortaleza"),
    ("America/Argentina/Buenos_Aires", "City of Buenos Aires"),
    ("America/Nuuk", "Greenland"),
    ("America/Montevideo", "Montevideo"),
    ("America/Punta_Arenas", "Punta Arenas"),
    ("America/Miquelon", "Saint Pierre and Miquelon"),
    ("America/Bahia", "Salvador"),
    ("Etc/GMT+2", "Coordinated Universal Time-02"),
    ("Atlantic/Azores", "Azores"),
    ("Atlantic/Cape_Verde", "Cabo Verde Is."),
    ("UTC", "Coordinated Universal Time"),
    ("Europe/London", "Dublin, Edinburgh, Lisbon, London"),
    ("Atlantic/Reykjavik", "Monrovia, Reykjavik"),
    ("Africa/Sao_Tome", "Sao Tome"),
    ("Africa/Casablanca", "Casablanca"),
    ("Europe/Berlin", "Amsterdam, Berlin, Bern, Rome, Stockholm, Vienna"),
    ("Europe/Budapest", "Belgrade, Bratislava, Budapest, Ljubljana, Prague"),
    ("Europe/Paris", "Brussels, Copenhagen, Madrid, Paris"),
    ("Europe/Warsaw", "Sarajevo, Skopje, Warsaw, Zagreb"),
    ("Africa/Lagos", "West Central Africa"),
    ("Asia/Amman", "Amman"),
    ("Europe/Bucharest", "Athens, Bucharest"),
    ("Asia/Beirut", "Beirut"),
    ("Africa/Cairo", "Cairo"),
    ("Europe/Chisinau", "Chisinau"),
    ("Asia/Damascus", "Damascus"),
    ("Asia/Hebron", "Gaza, Hebron"),
    ("Africa/Johannesburg", "Harare, Pretoria"),
    ("Europe/Kyiv", "Helsinki, Kyiv, Riga, Sofia, Tallinn, Vilnius"),
    ("Asia/Jerusalem", "Jerusalem"),
    ("Africa/Juba", "Juba"),
    ("Europe/Kaliningrad", "Kaliningrad"),
    ("Africa/Khartoum", "Khartoum"),
    ("Africa/Tripoli", "Tripoli"),
    ("Africa/Windhoek", "Windhoek"),
    ("Asia/Baghdad", "Baghdad"),
    ("Europe/Istanbul", "Istanbul"),
    ("Asia/Riyadh", "Kuwait, Riyadh"),
    ("Europe/Minsk", "Minsk"),
    ("Europe/Moscow", "Moscow, St. Petersburg"),
    ("Africa/Nairobi", "Nairobi"),
    ("Asia/Tehran", "Tehran"),
    ("Asia/Dubai", "Abu Dhabi, Muscat"),
    ("Europe/Astrakhan", "Astrakhan, Ulyanovsk"),
    ("Asia/Baku", "Baku"),
    ("Europe/Samara", "Izhevsk, Samara"),
    ("Indian/Mauritius", "Port Louis"),
    ("Europe/Saratov", "Saratov"),
    ("Asia/Tbilisi", "Tbilisi"),
    ("Europe/Volgograd", "Volgograd"),
    ("Asia/Yerevan", "Yerevan"),
    ("Asia/Kabul", "Kabul"),
    ("Asia/Tashkent", "Ashgabat, Tashkent"),
    ("Asia/Yekaterinburg", "Ekaterinburg"),
    ("Asia/Karachi", "Islamabad, Karachi"),
    ("Asia/Qyzylorda", "Qyzylorda"),
    ("Asia/Kolkata", "Chennai, Kolkata, Mumbai, New Delhi"),
    ("Asia/Colombo", "Sri Jayawardenepura"),
    ("Asia/Kathmandu", "Kathmandu"),
    ("Asia/Almaty", "Astana"),
    ("Asia/Dhaka", "Dhaka"),
    ("Asia/Omsk", "Omsk"),
    ("Asia/Yangon", "Yangon (Rangoon)"),
    ("Asia/Bangkok", "Bangkok, Hanoi, Jakarta"),
    ("Asia/Barnaul", "Barnaul, Gorno-Altaysk"),
    ("Asia/Hovd", "Hovd"),
    ("Asia/Krasnoyarsk", "Krasnoyarsk"),
    ("Asia/Novosibirsk", "Novosibirsk"),
    ("Asia/Tomsk", "Tomsk"),
    ("Asia/Shanghai", "Beijing, Chongqing, Hong Kong, Urumqi"),
    ("Asia/Irkutsk", "Irkutsk"),
    ("Asia/Singapore", "Kuala Lumpur, Singapore"),
    ("Australia/Perth", "Perth"),
    ("Asia/Taipei", "Taipei"),
    ("Asia/Ulaanbaatar", "Ulaanbaatar"),
    ("Australia/Eucla", "Eucla"),
    ("Asia/Chita", "Chita"),
    ("Asia/Tokyo", "Osaka, Sapporo, Tokyo"),
    ("Asia/Pyongyang", "Pyongyang"),
    ("Asia/Seoul", "Seoul"),
    ("Asia/Yakutsk", "Yakutsk"),
    ("Australia/Adelaide", "Adelaide"),
    ("Australia/Darwin", "Darwin"),
    ("Australia/Brisbane", "Brisbane"),
    ("Australia/Sydney", "Canberra, Melbourne, Sydney"),
    ("Pacific/Port_Moresby", "Guam, Port Moresby"),
    ("Australia/Hobart", "Hobart"),
    ("Asia/Vladivostok", "Vladivostok"),
    ("Australia/Lord_Howe", "Lord Howe Island"),
    ("Pacific/Bougainville", "Bougainville Island"),
    ("Asia/Srednekolymsk", "Chokurdakh"),
    ("Asia/Magadan", "Magadan"),
    ("Pacific/Norfolk", "Norfolk Island"),
    ("Asia/Sakhalin", "Sakhalin"),
    ("Pacific/Guadalcanal", "Solomon Is., New Caledonia"),
    ("Asia/Kamchatka", "Anadyr, Petropavlovsk-Kamchatsky"),
    ("Pacific/Auckland", "Auckland, Wellington"),
    ("Etc/GMT-12", "Coordinated Universal Time+12"),
    ("Pacific/Fiji", "Fiji"),
    ("Pacific/Chatham", "Chatham Islands"),
    ("Etc/GMT-13", "Coordinated Universal Time+13"),
    ("Pacific/Tongatapu", "Nuku'alofa"),
    ("Pacific/Apia", "Samoa"),
    ("Pacific/Kiritimati", "Kiritimati Island"),
)
_ZONE_NAMES = dict(ZONES)


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
    return _offset_text(_moment(when).astimezone(zone).utcoffset() or timedelta(0))


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


def zone_name(name: str) -> str:
    """The name Windows and Outlook use, such as ``Eastern Time (US & Canada)``."""
    if name in _ZONE_NAMES:
        return _ZONE_NAMES[name]
    region, _, city = name.rpartition("/")
    city = city.replace("_", " ")
    return f"{city} ({region.replace('_', ' ')})" if region else city


def standard_offset(zone: ZoneInfo, when: datetime | None = None) -> timedelta:
    """The zone's offset outside daylight saving time. Windows and Outlook label zones by this."""
    local = _moment(when).astimezone(zone)
    return (local.utcoffset() or timedelta(0)) - (local.dst() or timedelta(0))


def on_daylight_time(zone: ZoneInfo, when: datetime | None = None) -> bool:
    return bool(_moment(when).astimezone(zone).dst())


def zone_option(name: str, when: datetime | None = None) -> str:
    """``(UTC-05:00) Eastern Time (US & Canada)``, written the way Windows and Outlook write it."""
    if name == "UTC":
        return f"(UTC) {zone_name(name)}"
    return f"({_long_offset(standard_offset(ZoneInfo(name), when))}) {zone_name(name)}"


def zone_choices(when: datetime | None = None, keep: str = "") -> tuple[tuple[str, str], ...]:
    """``(iana name, option label)`` from UTC-12:00 to UTC+14:00, like the Windows list.

    ``keep`` adds a saved zone that is not on the list, so a choice made by name still shows.
    """
    moment = _moment(when)
    names = [name for name, _ in ZONES]
    if keep and keep.lower() != AUTO and keep not in _ZONE_NAMES and is_timezone(keep):
        names.append(keep)
    rows = [(name, zone_option(name, moment)) for name in names]
    rows.sort(key=lambda row: (standard_offset(ZoneInfo(row[0]), moment), row[1]))
    return tuple(rows)


def _moment(when: datetime | None) -> datetime:
    moment = when or datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _long_offset(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else "+"
    hours, rem = divmod(abs(total), 3600)
    return f"UTC{sign}{hours:02d}:{rem // 60:02d}"


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
    return _from_tzlocal() or _from_localtime() or _matching_offset()


def _from_tzlocal() -> str:
    """tzlocal reads the Windows registry, macOS, and Linux settings, with the full Windows name map."""
    try:
        import tzlocal

        return tzlocal.get_localzone_name() or ""
    except Exception:
        return ""


def _from_localtime() -> str:
    path = Path("/etc/localtime")
    try:
        parts = path.resolve().parts
    except OSError:
        return ""
    if "zoneinfo" not in parts:
        return ""
    return "/".join(parts[parts.index("zoneinfo") + 1 :])


def _matching_offset() -> str:
    """A listed zone that keeps the same hours as the computer clock, when the computer gives no name."""
    now = datetime.now(timezone.utc)
    current = now.astimezone().utcoffset()
    standard = -timedelta(seconds=time.timezone)
    for name, _ in ZONES:
        zone = ZoneInfo(name)
        if now.astimezone(zone).utcoffset() == current and standard_offset(zone, now) == standard:
            return name
    return ""
