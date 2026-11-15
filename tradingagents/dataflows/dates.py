"""Date parsing shared by the fork's data paths.

Every dated vendor call needs the same two answers: "is this a YYYY-MM-DD day?"
and "give it to me as a date". The fork's new modules used to repeat the
``datetime.strptime(...).date()`` try/except; this is the one place it lives.
"""

from __future__ import annotations

from datetime import date, datetime


def parse_day(value) -> date | None:
    """``value`` as a date, or None when it is not a YYYY-MM-DD day."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def require_day(value, label: str = "date") -> date:
    """``value`` as a date, raising ValueError naming ``label`` when it is not one."""
    day = parse_day(value)
    if day is None:
        raise ValueError(f"{label} must be YYYY-MM-DD, got {value!r}")
    return day
