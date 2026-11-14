"""Time: parsing, zones, validity windows, scheduled-change overlays and opening hours.

Instants are UTC strings with second precision and a trailing Z, so they compare correctly as text.
Record datetimes are stored as local wall time; the zone comes from the record's `timezone` field
or, failing that, the site.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import SiteError

UTC = timezone.utc
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Abbreviations are ambiguous or wrong for half the year; point callers at the region name.
ABBREVIATIONS = {
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles",
    "MST": "America/Denver", "MDT": "America/Denver", "MT": "America/Denver",
    "CST": "America/Chicago", "CDT": "America/Chicago", "CT": "America/Chicago",
    "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
    "GMT": "Europe/London", "BST": "Europe/London", "CET": "Europe/Paris", "CEST": "Europe/Paris",
    "IST": "Asia/Kolkata", "JST": "Asia/Tokyo", "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
}


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        suggestion = ABBREVIATIONS.get(str(name).upper())
        hint = f"Use the region name '{suggestion}'." if suggestion else \
            "Use a region name such as 'America/Los_Angeles' or 'Europe/Madrid'."
        raise SiteError("bad_timezone", f"'{name}' is not a time zone region name.", hint)


def to_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_z(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def resolve_date(value: Any, tz: ZoneInfo, now: datetime) -> date:
    """A date from 'today', 'tomorrow', 'yesterday' or YYYY-MM-DD, in the given zone."""
    if isinstance(value, datetime):
        return value.astimezone(tz).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().lower()
    today = now.astimezone(tz).date()
    if text in ("today", "now"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    if text == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise SiteError("bad_date", f"'{value}' is not a date.", "Use YYYY-MM-DD, 'today' or 'tomorrow'.")


def resolve_instant(value: Any, tz: ZoneInfo, now: datetime) -> datetime:
    """An aware UTC instant from a keyword, a date (midnight in tz), or a local or offset datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        low = text.lower()
        if low == "now":
            return now.astimezone(UTC)
        if low in ("today", "tomorrow", "yesterday") or len(text) == 10:
            d = resolve_date(text, tz, now)
            return datetime.combine(d, time(0, 0), tzinfo=tz).astimezone(UTC)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise SiteError("bad_datetime", f"'{value}' is not a date or time.",
                            "Use YYYY-MM-DD or YYYY-MM-DDTHH:MM, optionally with an offset.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def normalize_local(value: Any, tz: ZoneInfo) -> str:
    """Store a datetime as local wall time in tz. Offset inputs are converted; naive ones are kept."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        try:
            if len(text) == 10:
                dt = datetime.combine(date.fromisoformat(text), time(0, 0))
            else:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise SiteError("bad_datetime", f"'{value}' is not a date and time.",
                            "Use YYYY-MM-DDTHH:MM, optionally with an offset.")
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat()


def local_to_z(local_iso: str, tz: ZoneInfo) -> str:
    return to_z(datetime.fromisoformat(local_iso).replace(tzinfo=tz))


# ---------------------------------------------------------------- scheduled changes

def effective(data: dict, schedules: list, now_z: str):
    """Apply scheduled changes that are in force at now_z.

    Returns (data, active, upcoming, ended). A schedule is a dict with set, start, end, announced, id.
    """
    out = dict(data)
    active, upcoming, ended = [], [], []
    for s in sorted(schedules, key=lambda s: (s["start"], s["id"])):
        if s["start"] > now_z:
            upcoming.append(s)
        elif s["end"] is not None and s["end"] <= now_z:
            ended.append(s)
        else:
            for field, value in s["set"].items():
                if value is None:
                    out.pop(field, None)
                else:
                    out[field] = value
            active.append(s)
    return out, active, upcoming, ended


# ---------------------------------------------------------------- opening hours

def check_intervals(intervals: Any, where: str) -> list:
    if not isinstance(intervals, list):
        raise SiteError("bad_hours", f"{where} must be a list of [open, close] pairs.",
                        'Example: [["08:00", "15:00"]]')
    out = []
    for pair in intervals:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise SiteError("bad_hours", f"{where} must be a list of [open, close] pairs.",
                            'Example: [["08:00", "15:00"]]')
        for t in pair:
            try:
                time.fromisoformat(str(t))
            except ValueError:
                raise SiteError("bad_hours", f"'{t}' in {where} is not a time.", "Use 24-hour HH:MM.")
        if str(pair[0]) >= str(pair[1]):
            raise SiteError("bad_hours", f"In {where}, opening time {pair[0]} is not before closing time "
                            f"{pair[1]}.", "For hours past midnight, end at 23:59 and add the next day.")
        out.append([str(pair[0])[:5], str(pair[1])[:5]])
    return out


def check_weekly(weekly: Any) -> dict:
    if not isinstance(weekly, dict):
        raise SiteError("bad_hours", "weekly must be an object keyed by weekday.",
                        'Example: {"mon": [["08:00", "15:00"]], "tue": [["08:00", "15:00"]]}')
    out = {}
    for day, intervals in weekly.items():
        if day not in WEEKDAYS:
            raise SiteError("bad_hours", f"'{day}' is not a weekday key.", "Use " + ", ".join(WEEKDAYS) + ".")
        out[day] = check_intervals(intervals, f"weekly.{day}")
    return out


def hours_on(hours: dict, closures: list, day: date, tz: ZoneInfo, now: datetime) -> dict:
    """Is this place open on `day`? Dated closures override the weekly pattern; the latest wins."""
    weekday = WEEKDAYS[day.weekday()]
    intervals = list((hours.get("weekly") or {}).get(weekday, []))
    reason: Optional[str] = None
    source = "weekly"
    iso = day.isoformat()
    for key, c in closures:
        start = c.get("start_date")
        end = c.get("end_date") or start
        if start and start <= iso <= end:
            source = f"closure:{key}"
            reason = c.get("reason")
            intervals = [] if c.get("closed", True) else list(c.get("intervals") or [])
    result = {
        "date": iso, "weekday": weekday, "timezone": tz.key,
        "open": bool(intervals), "intervals": intervals, "source": source,
    }
    if reason:
        result["reason"] = reason
    local_now = now.astimezone(tz)
    if local_now.date() == day:
        hhmm = local_now.strftime("%H:%M")
        result["open_now"] = any(a <= hhmm < b for a, b in intervals)
    return result
