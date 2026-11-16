"""Time zone for every time kubesupport shows people (RCA timelines, reports).

TIMEZONE is an IANA name such as "Asia/Kolkata" or "Europe/London"; default UTC.
Kubernetes timestamps are UTC; they are converted for display only.
"""
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("kubesupport.time")


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown TIMEZONE '%s'; using UTC", name)
        return timezone.utc


TIMEZONE = os.getenv("TIMEZONE", "UTC").strip() or "UTC"
ZONE = _zone(TIMEZONE)


def parse(value) -> datetime | None:
    """A datetime, or a string as the Kubernetes client prints one; None when missing."""
    if isinstance(value, datetime):
        dt = value
    elif value and str(value) not in ("None", ""):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt(value) -> str:
    """'2026-11-16 16:24 IST' in the configured zone; '' when the value is missing."""
    dt = parse(value)
    return dt.astimezone(ZONE).strftime("%Y-%m-%d %H:%M %Z") if dt else ""


def now() -> datetime:
    return datetime.now(ZONE)
