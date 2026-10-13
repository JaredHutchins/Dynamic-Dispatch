"""Plane Cycle boundaries as exact moments, with local-day defaults for plain dates."""

from datetime import date, datetime, time


def moment(value, *, end=False):
    """Read a timestamp unchanged, or the start/end of a plain date in DD's local zone."""
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.max if end else time.min).astimezone()
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()


def local_date(value):
    """The date a Cycle boundary falls on in DD's local zone, as Plane shows it."""
    return moment(value).date().isoformat() if value else None
