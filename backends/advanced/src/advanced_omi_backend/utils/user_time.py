"""Convert canonical UTC timestamps for user-facing presentation."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def in_user_timezone(value: datetime, timezone_name: str | None) -> datetime:
    """Return ``value`` in the user's IANA timezone.

    MongoDB returns naive datetimes even when an aware UTC value was stored, so a
    naive value at this boundary is UTC, not server-local time.
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(timezone_name or "UTC"))


def format_user_datetime(
    value: datetime, timezone_name: str | None, format_string: str
) -> str:
    """Format a timestamp for display, including the zone chosen by the caller."""

    return in_user_timezone(value, timezone_name).strftime(format_string)
