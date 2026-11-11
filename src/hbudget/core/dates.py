"""Date helpers: month arithmetic, clamping, weekend shifts and week starts (ARCHITECTURE section 3)."""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from .models import Weekend


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def clamp_day(year: int, month: int, day: int) -> date:
    """The given day of the month, or the month's last day if the month is shorter (31 -> 30, Feb 30 -> 28/29)."""
    if not 1 <= day <= 31:
        raise ValueError('day must be 1..31, got %d' % day)
    return date(year, month, min(day, days_in_month(year, month)))


def add_months(d: date, n: int) -> date:
    """Same day n months later (or earlier), clamped to the month end: Jan 31 + 1 -> Feb 28 or 29."""
    index = d.year * 12 + d.month - 1 + n
    year, month = divmod(index, 12)
    return clamp_day(year, month + 1, d.day)


def shift_weekend(d: date, rule: Weekend) -> date:
    """Saturday/Sunday -> the Friday before (BEFORE) or the Monday after (AFTER); weekdays are unchanged."""
    wd = d.weekday()
    if wd < 5 or rule is Weekend.NONE:
        return d
    if rule is Weekend.BEFORE:
        return d - timedelta(days=wd - 4)
    return d + timedelta(days=7 - wd)


def week_start(d: date, first_weekday: int = 0) -> date:
    """The first day of d's week; first_weekday uses Python numbering (0 = Monday, 6 = Sunday)."""
    if not 0 <= first_weekday <= 6:
        raise ValueError('first_weekday must be 0..6')
    return d - timedelta(days=(d.weekday() - first_weekday) % 7)
