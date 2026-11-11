from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from hbudget.core.ledger import generate
from hbudget.core.models import Budget, Cadence, Horizon, Kind, LedgerRow, Status, Weekend
from hbudget.io.infer import infer_series


def _rows(name: str, dates: list[date], amount: int = 10000, kind: Kind = Kind.BILL,
          status: Status | None = None, start: int = 1) -> list[LedgerRow]:
    sign = -1 if kind is Kind.INCOME else 1
    return [LedgerRow(id='r%d' % (start + i), series_id=None, name=name, kind=kind, amount=sign * amount,
                      due_date=d, order_date=d, seq=start + i, status_override=status) for i, d in enumerate(dates)]


def _one(rows: list[LedgerRow], **kw: object):  # type: ignore[no-untyped-def]
    series, out = infer_series(rows, **kw)  # type: ignore[arg-type]
    assert len(series) == 1
    return series[0], out


def test_weekly_and_biweekly() -> None:
    s, out = _one(_rows('Food', [date(2029, 1, 6) + timedelta(weeks=k) for k in range(6)]))
    assert s.schedule.cadence is Cadence.WEEKLY and s.schedule.start == date(2029, 1, 6)
    assert out[0].id == 'food:2029-01-06' and out[0].series_id == 'food'
    s, _ = _one(_rows('Pay', [date(2029, 1, 5) + timedelta(weeks=2 * k) for k in range(5)], kind=Kind.INCOME))
    assert s.schedule.cadence is Cadence.BIWEEKLY and s.kind is Kind.INCOME and s.amount == 10000


def test_monthly_on_the_31st_clamps_in_short_months() -> None:
    dates = [date(2029, 1, 31), date(2029, 2, 28), date(2029, 3, 31), date(2029, 4, 30)]
    s, _ = _one(_rows('Card', dates))
    assert s.schedule.cadence is Cadence.MONTHLY and s.schedule.day == 31


def test_monthly_29th_in_february_of_a_leap_year() -> None:
    s, _ = _one(_rows('Loan', [date(2028, 1, 29), date(2028, 2, 29), date(2028, 3, 29)]))
    assert s.schedule.day == 29


def test_semimonthly_income_with_weekend_shift_across_year_end() -> None:
    # 1st and 15th, Saturday/Sunday paid the Friday before: 2028-10-01 is a Sunday -> Friday 2028-09-29;
    # 2029-01-01 is a Monday, 2028-12-31 not used; 2029-04-01 is a Sunday -> 2029-03-30.
    dates = [date(2028, 9, 29), date(2028, 10, 13), date(2028, 11, 1), date(2028, 11, 15), date(2028, 12, 1),
             date(2028, 12, 15), date(2029, 1, 1), date(2029, 1, 15)]
    s, out = _one(_rows('Pay', dates, kind=Kind.INCOME))
    assert s.schedule.cadence is Cadence.SEMIMONTHLY and s.schedule.days == (1, 15)
    assert s.schedule.weekend is Weekend.BEFORE
    first = out[0]
    assert first.due_date == date(2028, 9, 29) and first.nominal_date == date(2028, 10, 1)
    assert first.id == 'pay:2028-10-01'


def test_yearly_and_once() -> None:
    s, _ = _one(_rows('Gifts', [date(2028, 12, 1), date(2029, 12, 1), date(2030, 12, 1)]))
    assert s.schedule.cadence is Cadence.YEARLY and (s.schedule.month, s.schedule.day) == (12, 1)
    s, out = _one(_rows('Trip', [date(2029, 6, 1)]))
    assert s.schedule.cadence is Cadence.ONCE and out[0].nominal_date == date(2029, 6, 1)


def test_irregular_and_mixed_kinds_stay_standalone() -> None:
    rows = _rows('Odd', [date(2029, 1, 1), date(2029, 1, 4), date(2029, 2, 20)])
    rows += _rows('Mixed', [date(2029, 1, 1)], start=10)
    rows += _rows('Mixed', [date(2029, 2, 1)], kind=Kind.INCOME, start=11)
    series, out = infer_series(rows)
    assert series == [] and [r.id for r in out] == [r.id for r in rows]


def test_ended_series_gets_end_and_watermark_is_the_sheet_end() -> None:
    rows = _rows('Old loan', [date(2029, 1, 10), date(2029, 2, 10)])
    rows += _rows('Rent', [date(2029, 1, 3), date(2029, 2, 3), date(2029, 3, 3), date(2029, 4, 3)], start=20)
    series, out = infer_series(rows, horizon_end=date(2029, 4, 30))
    by = {s.id: s for s in series}
    assert by['old-loan'].schedule.end == date(2029, 2, 10)
    assert by['rent'].schedule.end is None
    assert {s.generated_through for s in series} == {date(2029, 4, 30)}
    budget = Budget(start_balance=0, as_of=date(2029, 1, 1), horizon=Horizon('rolling', 15), series=tuple(series),
                    rows=tuple(out))
    later = generate(budget, date(2029, 8, 31))
    old = [r for r in later.rows if r.series_id == 'old-loan']
    assert len(old) == 2                                  # an ended bill is never refilled
    rent = sorted(r.id for r in later.rows if r.series_id == 'rent')
    assert rent[-1] == 'rent:2029-08-03' and len(rent) == len(set(rent)) == 8   # same id scheme, no duplicates


def test_imported_gap_is_not_refilled() -> None:
    rows = _rows('Water', [date(2029, 1, 8), date(2029, 2, 8), date(2029, 4, 8), date(2029, 5, 8)])
    series, out = infer_series(rows, horizon_end=date(2029, 5, 31))
    budget = Budget(start_balance=0, as_of=date(2029, 1, 1), horizon=Horizon('rolling', 15), series=tuple(series),
                    rows=tuple(out))
    later = generate(budget, date(2029, 7, 31))
    dates = sorted(r.due_date for r in later.rows)
    assert date(2029, 3, 8) not in dates and date(2029, 6, 8) in dates


def test_amount_overrides_status_and_card() -> None:
    rows = _rows('Fuel', [date(2029, 1, 1) + timedelta(weeks=k) for k in range(4)], status=Status.AUTOPAY)
    rows[3] = replace(rows[3], amount=5000)
    rows = [replace(r, card='card-a') for r in rows]
    s, out = _one(rows)
    assert s.amount == 10000 and s.status is Status.AUTOPAY and s.card == 'card-a'
    assert [r.overridden for r in out] == [False, False, False, True]
    assert all(r.status_override is None for r in out)
    mixed = _rows('Gas', [date(2029, 1, 2), date(2029, 2, 2)], status=Status.MANUAL)
    s, out = _one([mixed[0], replace(mixed[1], status_override=None)])
    assert s.status is Status.NONE and [r.status_override for r in out] == [Status.MANUAL, None]


def test_id_collisions() -> None:
    rows = _rows('Fee', [date(2029, 1, 5), date(2029, 2, 5)]) + _rows('fee!', [date(2029, 3, 9)], start=7)
    rows += _rows('Fee', [date(2029, 2, 5)], start=9)                 # two rows of one series on one date
    series, out = infer_series(rows)
    assert sorted(s.id for s in series) == ['fee', 'fee-2']
    assert sorted(r.id for r in out if r.series_id == 'fee') == ['fee:2029-01-05', 'fee:2029-02-05',
                                                                 'fee:2029-02-05:r9']


def test_empty_input() -> None:
    assert infer_series([]) == ([], [])
