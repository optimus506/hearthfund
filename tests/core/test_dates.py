from datetime import date

import pytest

from hbudget.core.dates import add_months, clamp_day, days_in_month, shift_weekend, week_start
from hbudget.core.models import Weekend


def test_add_months_month_end_leap_and_normal_year():
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2029, 1, 31), 1) == date(2029, 2, 28)
    assert add_months(date(2029, 3, 31), 1) == date(2029, 4, 30)
    assert add_months(date(2029, 11, 15), 2) == date(2030, 1, 15)
    assert add_months(date(2029, 1, 15), -1) == date(2028, 12, 15)
    assert add_months(date(2029, 5, 31), 15) == date(2030, 8, 31)


def test_clamp_day():
    assert clamp_day(2029, 4, 31) == date(2029, 4, 30)
    assert clamp_day(2028, 2, 30) == date(2028, 2, 29)
    assert clamp_day(2029, 2, 29) == date(2029, 2, 28)
    assert clamp_day(2029, 1, 31) == date(2029, 1, 31)
    assert days_in_month(2028, 2) == 29
    with pytest.raises(ValueError):
        clamp_day(2029, 1, 0)
    with pytest.raises(ValueError):
        clamp_day(2029, 1, 32)


def test_shift_weekend():
    sat, sun, mon = date(2029, 9, 1), date(2029, 9, 2), date(2029, 9, 3)
    assert sat.weekday() == 5
    assert shift_weekend(sat, Weekend.BEFORE) == date(2029, 8, 31)
    assert shift_weekend(sun, Weekend.BEFORE) == date(2029, 8, 31)
    assert shift_weekend(sat, Weekend.AFTER) == mon
    assert shift_weekend(sun, Weekend.AFTER) == mon
    assert shift_weekend(sat, Weekend.NONE) == sat
    assert shift_weekend(mon, Weekend.BEFORE) == mon


def test_shift_weekend_across_year_boundary():
    # Saturday 2028-01-01 -> Friday 2027-12-31; Sunday 2028-12-31 -> Monday 2029-01-01
    assert shift_weekend(date(2028, 1, 1), Weekend.BEFORE) == date(2027, 12, 31)
    assert shift_weekend(date(2028, 12, 31), Weekend.AFTER) == date(2029, 1, 1)


def test_week_start_monday_and_sunday_weeks():
    wed = date(2029, 1, 3)
    assert week_start(wed) == date(2029, 1, 1)                 # Monday
    assert week_start(wed, 6) == date(2028, 12, 31)            # Sunday week, across the year
    assert week_start(date(2029, 1, 1)) == date(2029, 1, 1)
    with pytest.raises(ValueError):
        week_start(wed, 7)
