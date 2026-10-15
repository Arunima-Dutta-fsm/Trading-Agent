"""Time helpers. All stored timestamps are timezone-aware UTC ISO strings.

Market logic uses New York time (America/New_York). Report file names use the
report timezone from strategy.json (default Asia/Kolkata, IST) so the students
see their own local time in the folder.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_ny(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return dt.astimezone(NY)


def report_stamp(dt: datetime, tz_name: str) -> str:
    """DDMMYYYY-HH-MM. The brief asks for DDMMYYYY-HH:MM, but ':' is not allowed in
    Windows file names, so the colon becomes '-'."""
    local = dt.astimezone(ZoneInfo(tz_name))
    return local.strftime("%d%m%Y-%H-%M")


def fmt_local(dt: datetime, tz_name: str) -> str:
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%d %b %Y %H:%M %Z")
