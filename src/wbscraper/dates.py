"""Date normalisation to ISO 8601 (UTC). Returns None when a date is genuinely unavailable or implausible."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dateparser

_MIN_YEAR = 1995


def _plausible(dt: datetime, now: float | None = None) -> bool:
    if dt.year < _MIN_YEAR:
        return False
    limit = (now if now is not None else time.time()) + 86400
    return dt.timestamp() <= limit


def to_datetime(value: Any, now: float | None = None) -> datetime | None:
    if value is None or value == "":
        return None
    dt: datetime | None = None
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            ts = float(value)
            if ts > 1e11:  # milliseconds
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            s = str(value).strip()
            if not s:
                return None
            if s.replace(".", "", 1).isdigit():
                return to_datetime(float(s), now)
            dt = dateparser.parse(s)
    except (ValueError, OverflowError, OSError, TypeError, dateparser.ParserError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt if _plausible(dt, now) else None


def to_iso(value: Any, now: float | None = None, date_only: bool = False) -> str | None:
    dt = to_datetime(value, now)
    if dt is None:
        return None
    if date_only:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date_to_epoch(value: Any) -> int | None:
    dt = to_datetime(value)
    return int(dt.timestamp()) if dt else None
