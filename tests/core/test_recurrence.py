from datetime import date

import pytest

from hbudget.core.models import Cadence, Kind, Schedule, Series, Weekend
from hbudget.core.recurrence import Occurrence, amount_for, dates, occurrences

D = date


def sched(cadence, start, **kw):
    return Schedule(cadence=cadence, start=start, **kw)


def test_once_in_and_out_of_range():
    s = sched(Cadence.ONCE, D(2029, 5, 10))
    assert dates(s, D(2029, 5, 1), D(2029, 5, 31)) == [D(2029, 5, 10)]
    assert dates(s, D(2029, 6, 1), D(2029, 6, 30)) == []


def test_weekly_with_interval_and_biweekly():
    w = sched(Cadence.WEEKLY, D(2029, 1, 1))
    assert dates(w, D(2029, 1, 1), D(2029, 1, 21)) == [D(2029, 1, 1), D(2029, 1, 8), D(2029, 1, 15)]
    w3 = sched(Cadence.WEEKLY, D(2029, 1, 1), interval=3)
    assert dates(w3, D(2029, 1, 1), D(2029, 2, 28)) == [D(2029, 1, 1), D(2029, 1, 22), D(2029, 2, 12)]
    b = sched(Cadence.BIWEEKLY, D(2029, 1, 5))
    assert dates(b, D(2029, 1, 10), D(2029, 2, 28)) == [D(2029, 1, 19), D(2029, 2, 2), D(2029, 2, 16)]


def test_start_after_window_start_keeps_first_occurrence():
    w = sched(Cadence.WEEKLY, D(2029, 1, 8))
    assert dates(w, D(2029, 1, 1), D(2029, 1, 10)) == [D(2029, 1, 8)]


def test_monthly_29th_30th_31st_in_short_months():
    for day, feb in ((29, D(2029, 2, 28)), (30, D(2029, 2, 28)), (31, D(2029, 2, 28))):
        s = sched(Cadence.MONTHLY, D(2029, 1, 1), day=day)
        got = dates(s, D(2029, 1, 1), D(2029, 4, 30))
        assert got[1] == feb
        assert got[3] == D(2029, 4, min(day, 30))
    leap = sched(Cadence.MONTHLY, D(2028, 1, 1), day=29)
    assert dates(leap, D(2028, 2, 1), D(2028, 2, 29)) == [D(2028, 2, 29)]


def test_monthly_drops_dates_before_start_and_defaults_day():
    s = sched(Cadence.MONTHLY, D(2029, 1, 23), day=5)
    occ = occurrences(s, D(2029, 1, 1), D(2029, 3, 31))
    assert [o.nominal for o in occ] == [D(2029, 2, 5), D(2029, 3, 5)]
    assert [o.index for o in occ] == [0, 1]
    assert dates(sched(Cadence.MONTHLY, D(2029, 1, 12)), D(2029, 1, 1), D(2029, 2, 28)) == [D(2029, 1, 12),
                                                                                          D(2029, 2, 12)]


def test_yearly_feb_29():
    s = sched(Cadence.YEARLY, D(2028, 2, 29))
    assert dates(s, D(2028, 1, 1), D(2030, 12, 31)) == [D(2028, 2, 29), D(2029, 2, 28), D(2030, 2, 28)]
    y = sched(Cadence.YEARLY, D(2029, 6, 1), month=3, day=10)
    assert dates(y, D(2029, 1, 1), D(2031, 12, 31)) == [D(2030, 3, 10), D(2031, 3, 10)]


def test_semimonthly_weekend_before_and_after():
    before = sched(Cadence.SEMIMONTHLY, D(2029, 3, 1), days=(15, 31), weekend=Weekend.BEFORE)
    # Mar 31 2029 is a Saturday -> Friday Mar 30; Apr 15 is a Sunday -> Friday Apr 13; Apr 31 -> Apr 30 (Monday)
    assert dates(before, D(2029, 3, 1), D(2029, 4, 30)) == [D(2029, 3, 15), D(2029, 3, 30), D(2029, 4, 13),
                                                          D(2029, 4, 30)]
    after = sched(Cadence.SEMIMONTHLY, D(2029, 3, 1), days=(15, 31), weekend=Weekend.AFTER)
    assert dates(after, D(2029, 3, 20), D(2029, 4, 20)) == [D(2029, 4, 2), D(2029, 4, 16)]


def test_semimonthly_shift_across_month_and_year():
    s = sched(Cadence.SEMIMONTHLY, D(2028, 1, 1), days=(1, 15), weekend=Weekend.BEFORE)
    occ = occurrences(s, D(2028, 1, 1), D(2028, 1, 31))
    assert occ[0] == Occurrence(D(2028, 1, 1), D(2027, 12, 31), 0)


def test_semimonthly_30th_31st_collapse():
    # Feb: both clamp to the 28th -> one date
    s = sched(Cadence.SEMIMONTHLY, D(2029, 2, 1), days=(30, 31))
    assert dates(s, D(2029, 2, 1), D(2029, 2, 28)) == [D(2029, 2, 28)]
    # Mar 2030: Sat 30th and Sun 31st both shift to Fri 29th -> keep the first
    b = sched(Cadence.SEMIMONTHLY, D(2030, 3, 1), days=(30, 31), weekend=Weekend.BEFORE)
    occ = occurrences(b, D(2030, 3, 1), D(2030, 3, 31))
    assert [(o.nominal, o.actual) for o in occ] == [(D(2030, 3, 30), D(2030, 3, 29))]


def test_semimonthly_needs_two_distinct_days():
    with pytest.raises(ValueError):
        occurrences(sched(Cadence.SEMIMONTHLY, D(2029, 1, 1), days=(15, 15)), D(2029, 1, 1), D(2029, 2, 1))
    with pytest.raises(ValueError):
        occurrences(sched(Cadence.SEMIMONTHLY, D(2029, 1, 1)), D(2029, 1, 1), D(2029, 2, 1))


def test_window_edges_use_the_nominal_date():
    # nominal inside, actual outside: Sunday Dec 31 2028 shifts to Monday Jan 1 2029
    s = sched(Cadence.MONTHLY, D(2028, 12, 1), day=31, weekend=Weekend.AFTER)
    assert occurrences(s, D(2028, 12, 1), D(2028, 12, 31)) == [Occurrence(D(2028, 12, 31), D(2029, 1, 1), 0)]
    # nominal outside, actual inside: Saturday Dec 1 2029 shifts to Friday Nov 30 but is not a November occurrence
    m = sched(Cadence.MONTHLY, D(2029, 1, 1), day=1, weekend=Weekend.BEFORE)
    assert [o.nominal for o in occurrences(m, D(2029, 11, 1), D(2029, 11, 30))] == [D(2029, 11, 1)]


def test_schedule_end_inclusive_on_nominal():
    s = sched(Cadence.MONTHLY, D(2029, 1, 1), day=10, end=D(2029, 3, 10))
    assert dates(s, D(2029, 1, 1), D(2029, 12, 31)) == [D(2029, 1, 10), D(2029, 2, 10), D(2029, 3, 10)]


def test_index_is_stable_across_windows():
    s = sched(Cadence.WEEKLY, D(2029, 1, 1))
    a = occurrences(s, D(2029, 1, 1), D(2029, 2, 28))
    b = occurrences(s, D(2029, 2, 1), D(2029, 2, 28))
    assert {o.nominal: o.index for o in a if o.nominal >= D(2029, 2, 1)} == {o.nominal: o.index for o in b}


def series(**kw):
    base = dict(id='lawn', name='Lawn care', kind=Kind.BILL, amount=10000,
                schedule=sched(Cadence.MONTHLY, D(2029, 1, 1), day=31, weekend=Weekend.AFTER))
    base.update(kw)
    return Series(**base)


def test_amount_for_base_month_and_cycle():
    s = series()
    occ = occurrences(s.schedule, D(2029, 1, 1), D(2029, 12, 31))
    assert {amount_for(s, o) for o in occ} == {10000}
    seasonal = series(month_amounts=((3, 12000), (4, 12000)))
    # Mar 31 2029 is a Saturday -> Monday Apr 2, but the nominal month (March) decides
    mar = next(o for o in occ if o.nominal.month == 3)
    assert mar.actual == D(2029, 4, 2)
    assert amount_for(seasonal, mar) == 12000
    feb = next(o for o in occ if o.nominal.month == 2)
    assert amount_for(seasonal, feb) == 10000
    cycle = series(cycle_amounts=(5000, 3500), month_amounts=((3, 1),))
    assert [amount_for(cycle, o) for o in occ[:4]] == [5000, 3500, 5000, 3500]
