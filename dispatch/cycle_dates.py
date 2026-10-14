"""Plane Cycle boundaries as exact moments, with local-day defaults for plain dates."""

from datetime import date, datetime, time, timedelta, timezone


def moment(value, *, end=False):
    """Read a timestamp unchanged, or the start/end of a plain date in DD's local zone."""
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.max if end else time.min).astimezone()
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()


def local_date(value):
    """The date a Cycle boundary falls on in DD's local zone, as Plane shows it."""
    return moment(value).date().isoformat() if value else None


def same_minute(first, second):
    """Plane keeps Cycle ends to the minute, so finer differences don't count."""
    return (moment(first, end=True).replace(second=0, microsecond=0)
            == moment(second, end=True).replace(second=0, microsecond=0))


def early_end(start, now):
    """End at 23:59 yesterday locally, as Plane does, clamped to the Cycle's local start day."""
    day = max(moment(start).date(), now.date() - timedelta(days=1))
    return datetime.combine(day, time(23, 59)).astimezone().astimezone(timezone.utc).isoformat()
