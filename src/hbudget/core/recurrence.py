"""Occurrences of a schedule and the amount of each one (ARCHITECTURE section 5)."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

from .dates import clamp_day, shift_weekend
from .models import Cadence, Schedule, Series
from .money import Cents

__all__ = ['Occurrence', 'amount_for', 'dates', 'occurrences']


@dataclass(frozen=True, slots=True)
class Occurrence:
    nominal: date      # before the weekend shift: decides range, month and identity
    actual: date       # after the weekend shift: stored as the row's due date
    index: int         # occurrence number counted from schedule.start (0 = first)


def _nominal_dates(s: Schedule) -> Iterator[date]:
    """Every nominal date from schedule.start on, in order, without an end (callers stop)."""
    if s.cadence is Cadence.ONCE:
        yield s.start
        return
    if s.cadence in (Cadence.WEEKLY, Cadence.BIWEEKLY):
        step = timedelta(days=14 if s.cadence is Cadence.BIWEEKLY else 7 * max(1, s.interval))
        d = s.start
        while True:
            yield d
            d += step
    if s.cadence is Cadence.MONTHLY:
        day = s.day or s.start.day
        y, m = s.start.year, s.start.month
        while True:
            d = clamp_day(y, m, day)
            if d >= s.start:
                yield d
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if s.cadence is Cadence.SEMIMONTHLY:
        if not s.days or len(set(s.days)) != 2:
            raise ValueError('semimonthly needs two different days')
        first, second = sorted(s.days)
        y, m = s.start.year, s.start.month
        while True:
            a, b = clamp_day(y, m, first), clamp_day(y, m, second)
            for d in (a,) if a == b else (a, b):   # 30th and 31st both clamp in February: one date
                if d >= s.start:
                    yield d
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if s.cadence is Cadence.YEARLY:
        month = s.month or s.start.month
        day = s.day or s.start.day
        y = s.start.year
        while True:
            d = clamp_day(y, month, day)
            if d >= s.start:
                yield d
            y += 1
    raise ValueError('unknown cadence %r' % s.cadence)


def occurrences(schedule: Schedule, start: date, end: date) -> list[Occurrence]:
    """Occurrences whose nominal date is in [start, end] (and not after schedule.end), sorted.

    The weekend shift is applied after clamping; if two shifted dates collide, the first is kept.
    """
    out: list[Occurrence] = []
    last_actual: date | None = None
    for index, nominal in enumerate(_nominal_dates(schedule)):
        if nominal > end or (schedule.end is not None and nominal > schedule.end):
            break
        actual = shift_weekend(nominal, schedule.weekend)
        collided = actual == last_actual
        last_actual = actual
        if nominal < start or collided:
            continue
        out.append(Occurrence(nominal, actual, index))
    return out


def dates(schedule: Schedule, start: date, end: date) -> list[date]:
    """The actual (shifted) dates of occurrences()."""
    return [o.actual for o in occurrences(schedule, start, end)]


def amount_for(series: Series, occ: Occurrence) -> Cents:
    """Magnitude for one occurrence: cycle amounts, else the seasonal amount of the nominal month, else the amount."""
    if series.cycle_amounts:
        return series.cycle_amounts[occ.index % len(series.cycle_amounts)]
    for month, amount in series.month_amounts:
        if month == occ.nominal.month:
            return amount
    return series.amount
