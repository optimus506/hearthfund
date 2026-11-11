from dataclasses import replace

import pytest

from factories import D, budget, row, schedule, series
from hbudget.core.ledger import generate, horizon_end, parse_months, regenerate_series, remove_row, set_month_amounts
from hbudget.core.models import Horizon, Kind, RowState, Status, Weekend


def dues(b, sid=None):
    return [r.due_date for r in sorted(b.rows, key=lambda r: r.due_date) if sid is None or r.series_id == sid]


def test_first_occurrence_when_start_is_after_as_of():
    # as_of is a Friday; a weekly Monday series starts the next Monday
    weekly = series('grocery', 'spending', 16000, schedule('weekly', D(2029, 3, 5)))
    b = generate(budget(as_of=D(2029, 3, 2), series=(weekly,)), D(2029, 3, 19))
    assert dues(b) == [D(2029, 3, 5), D(2029, 3, 12), D(2029, 3, 19)]


def test_row_due_on_as_of_is_included():
    rent = series('rent', 'bill', 180000, schedule('monthly', D(2029, 1, 1), day=1))
    b = generate(budget(as_of=D(2029, 3, 1), series=(rent,)), D(2029, 4, 30))
    assert dues(b) == [D(2029, 3, 1), D(2029, 4, 1)]


def test_ids_signs_card_and_watermark():
    pay = series('pay', 'income', 200000, schedule('monthly', D(2029, 3, 1), day=31, weekend=Weekend.BEFORE))
    lawn = series('lawn', 'bill', 12000, schedule('monthly', D(2029, 3, 1), day=14), card='card')
    b = generate(budget(series=(pay, lawn)), D(2029, 3, 31))
    p = next(r for r in b.rows if r.series_id == 'pay')
    assert p.id == 'pay:2029-03-31' and p.nominal_date == D(2029, 3, 31) and p.due_date == D(2029, 3, 30)
    assert p.amount == -200000 and p.kind is Kind.INCOME
    lawn_row = next(r for r in b.rows if r.series_id == 'lawn')
    assert lawn_row.card == 'card' and lawn_row.amount == 12000
    assert {s.id: s.generated_through for s in b.series} == {'pay': D(2029, 3, 31), 'lawn': D(2029, 3, 31)}


def test_generate_twice_adds_nothing_and_extends():
    rent = series('rent', 'bill', 180000, schedule('monthly', D(2029, 1, 1), day=3))
    b = generate(budget(series=(rent,)), D(2029, 3, 31))
    assert generate(b, D(2029, 3, 31)).rows == b.rows
    assert dues(generate(b, D(2029, 4, 30))) == [D(2029, 3, 3), D(2029, 4, 3)]


def test_watermark_keeps_removed_occurrence_and_imported_gap():
    rent = series('rent', 'bill', 180000, schedule('monthly', D(2029, 1, 1), day=3))
    b = generate(budget(series=(rent,)), D(2029, 4, 30))
    b = remove_row(b, 'rent:2029-04-03')
    # an imported gap: the March row is not there at all, and the watermark says rows exist up to April
    gap = replace(b, rows=tuple(r for r in b.rows if r.id != 'rent:2029-03-03'))
    again = generate(gap, D(2029, 5, 31))
    assert dues(again) == [D(2029, 4, 3), D(2029, 5, 3)]
    assert next(r for r in again.rows if r.id == 'rent:2029-04-03').state is RowState.REMOVED


def test_duplicate_guard_in_any_state_even_without_watermark():
    rent = series('rent', 'bill', 180000, schedule('monthly', D(2029, 1, 1), day=3))
    cleared = row('rent:2029-03-03', 'Rent', 180000, D(2029, 3, 3), series_id='rent', nominal_date=D(2029, 3, 3),
                  state=RowState.CLEARED)
    b = generate(budget(series=(rent,), rows=(cleared,)), D(2029, 3, 31))
    assert len(b.rows) == 1


def test_watermark_never_moves_back():
    rent = series('rent', 'bill', 180000, schedule('monthly', D(2029, 1, 1), day=3))
    b = generate(budget(series=(rent,)), D(2029, 5, 31))
    b = generate(b, D(2029, 3, 31))
    assert b.series[0].generated_through == D(2029, 5, 31)


def test_inactive_series_reactivated_later_creates_no_past_due_rows():
    gym = series('gym', 'bill', 4500, schedule('monthly', D(2029, 1, 1), day=5), active=False)
    b = generate(budget(series=(gym,)), D(2029, 4, 30))
    assert b.rows == () and b.series[0].generated_through == D(2029, 2, 28)
    # months later (a reconciliation moved as_of), the user switches it back on
    later = replace(b, as_of=D(2029, 6, 1), series=(replace(b.series[0], active=False),))
    later = generate(later, D(2029, 6, 30))
    later = replace(later, series=(replace(later.series[0], active=True),))
    assert dues(generate(later, D(2029, 7, 31))) == [D(2029, 6, 5), D(2029, 7, 5)]


def test_seasonal_amount_by_nominal_month():
    lawn = series('lawn', 'bill', 4000, schedule('monthly', D(2029, 1, 1), day=30, weekend=Weekend.AFTER),
                  month_amounts=((5, 12000), (6, 12000), (7, 12000), (8, 12000), (9, 12000)))
    b = generate(budget(as_of=D(2029, 9, 1), series=(lawn,)), D(2029, 10, 31))
    # Sep 30 2029 is a Sunday: due Monday Oct 1, but the nominal month is September
    sep = next(r for r in b.rows if r.nominal_date == D(2029, 9, 30))
    assert sep.due_date == D(2029, 10, 1) and sep.amount == 12000
    assert next(r for r in b.rows if r.nominal_date == D(2029, 10, 30)).amount == 4000


def test_cycle_amounts_index_stable_across_windows():
    fuel = series('fuel', 'spending', 5000, schedule('weekly', D(2029, 3, 3)), cycle_amounts=(5000, 3500))
    one = generate(budget(series=(fuel,)), D(2029, 4, 30))
    two = generate(generate(budget(series=(fuel,)), D(2029, 3, 20)), D(2029, 4, 30))
    amounts = lambda b: [r.amount for r in sorted(b.rows, key=lambda r: r.due_date)]  # noqa: E731
    assert amounts(one) == amounts(two) == [5000, 3500] * 4 + [5000]


def test_imported_ended_bill_is_not_refilled():
    # an inferred series that stopped in March, in a sheet that runs to June: end set, watermark = sheet end
    old = series('old-loan', 'bill', 30000, schedule('monthly', D(2029, 1, 10), day=10, end=D(2029, 3, 10)),
                 generated_through=D(2029, 6, 30))
    rows = tuple(row('old-loan:2029-%02d-10' % m, 'Old loan', 30000, D(2029, m, 10), series_id='old-loan',
                     nominal_date=D(2029, m, 10)) for m in (3,))
    b = budget(series=(old,), rows=rows, horizon=Horizon('fixed', end=D(2029, 6, 30)))
    assert generate(b, D(2029, 9, 30)).rows == rows                      # Extend
    assert regenerate_series(b, 'old-loan', D(2029, 3, 1)).rows == rows   # a series edit
    rolling = replace(b, horizon=Horizon('rolling', months=15))
    assert generate(rolling, horizon_end(rolling)).rows == rows           # switch to rolling


def test_regenerate_series_keeps_ids_seq_and_marks():
    elec = series('electric', 'bill', 15000, schedule('monthly', D(2029, 1, 1), day=6))
    b = generate(budget(series=(elec,)), D(2029, 5, 31))
    b = replace(b, rows=tuple(replace(r, seq=7, status_override=Status.MANUAL) if r.due_date == D(2029, 4, 6) else
                              replace(r, amount=99, overridden=True) if r.due_date == D(2029, 5, 6) else r
                              for r in b.rows))
    b = replace(b, series=(replace(b.series[0], amount=16000),))
    after = regenerate_series(b, 'electric', D(2029, 4, 1))
    by = {r.due_date: r for r in after.rows}
    assert by[D(2029, 3, 6)].amount == 15000                 # before from_date: untouched
    assert by[D(2029, 4, 6)].amount == 16000 and by[D(2029, 4, 6)].seq == 7
    assert by[D(2029, 4, 6)].status_override is Status.MANUAL and by[D(2029, 4, 6)].id == 'electric:2029-04-06'
    assert by[D(2029, 5, 6)].amount == 99                    # overridden: never touched


def test_regenerate_series_moves_day_and_respects_watermark():
    elec = series('electric', 'bill', 15000, schedule('monthly', D(2029, 1, 1), day=6))
    b = generate(budget(series=(elec,)), D(2029, 4, 30))
    b = replace(b, series=(replace(b.series[0], schedule=schedule('monthly', D(2029, 1, 1), day=20)),))
    after = regenerate_series(b, 'electric', D(2029, 3, 1))
    assert dues(after) == [D(2029, 3, 20), D(2029, 4, 20)]
    with pytest.raises(KeyError):
        regenerate_series(b, 'nope', D(2029, 3, 1))


def test_regenerate_inactive_series_removes_open_generated_rows():
    elec = series('electric', 'bill', 15000, schedule('monthly', D(2029, 1, 1), day=6))
    b = generate(budget(series=(elec,)), D(2029, 4, 30))
    b = replace(b, series=(replace(b.series[0], active=False),))
    assert regenerate_series(b, 'electric', D(2029, 4, 1)).rows == tuple(r for r in b.rows if r.due_date.month == 3)


def test_parse_months():
    assert parse_months('10-4') == frozenset({10, 11, 12, 1, 2, 3, 4})
    assert parse_months('5-9') == frozenset(range(5, 10))
    assert parse_months('1, 3') == frozenset({1, 3})
    assert parse_months('7') == frozenset({7})
    for bad in ('0', '13', '3-14', '', 'x', '1,'):
        with pytest.raises(ValueError):
            parse_months(bad)


def test_set_month_amounts():
    lawn = series('lawn', 'bill', 8000, schedule('monthly', D(2029, 1, 1), day=14))
    b = generate(budget(series=(lawn,), horizon=Horizon('fixed', end=D(2029, 12, 31))), D(2029, 12, 31))
    b = replace(b, rows=tuple(replace(r, state=RowState.CLEARED) if r.due_date.month == 3 else r for r in b.rows))
    out, changes = set_month_amounts(b, 'lawn', [(parse_months('5-9'), 12000), (parse_months('10-4'), 4000)],
                                     D(2029, 3, 1))
    by = {r.due_date.month: r.amount for r in out.rows}
    assert by[3] == 8000                                     # cleared rows never change
    assert [by[m] for m in (4, 5, 9, 10, 12)] == [4000, 12000, 12000, 4000, 4000]
    assert len(changes) == 9 and changes[0].was == 8000 and changes[0].now == 4000
    assert dict(out.series[0].month_amounts)[7] == 12000
    with pytest.raises(ValueError):
        set_month_amounts(b, 'lawn', [(frozenset({1}), -5)], D(2029, 3, 1))
    with pytest.raises(KeyError):
        set_month_amounts(b, 'nope', [], D(2029, 3, 1))


def test_horizon_end():
    assert horizon_end(budget(horizon=Horizon('rolling', months=15), as_of=D(2029, 1, 31))) == D(2030, 4, 30)
    assert horizon_end(budget(horizon=Horizon('fixed', end=D(2029, 12, 31)))) == D(2029, 12, 31)
    with pytest.raises(ValueError):
        horizon_end(budget(horizon=Horizon('fixed')))


def test_regenerate_weekend_edit_keeps_the_row_on_the_from_date_sunday():
    # a monthly pay on the 1st; Apr 1 2029 is a Sunday. Editing it to "weekend: before" from that Sunday on must
    # move the April row to Friday Mar 30, not drop it (rows are matched by their nominal date).
    pay = series('pay', 'income', 200000, schedule('monthly', D(2029, 1, 1), day=1))
    b = generate(budget(series=(pay,)), D(2029, 4, 30))
    edited = replace(b, series=(replace(b.series[0], schedule=schedule('monthly', D(2029, 1, 1), day=1,
                                                                       weekend=Weekend.BEFORE)),))
    after = regenerate_series(edited, 'pay', D(2029, 4, 1))
    april = next(r for r in after.rows if r.nominal_date == D(2029, 4, 1))
    assert (april.id, april.due_date, april.order_date) == ('pay:2029-04-01', D(2029, 3, 30), D(2029, 3, 30))
    assert len(after.rows) == len(b.rows) == 2


def test_regenerate_series_gives_hand_edited_rows_the_new_card_and_name():
    """#60: a new amount and card in one edit; rows changed by hand keep their amounts, not the old card."""
    s = series('grocery', 'spending', 20000, schedule('weekly', D(2029, 3, 1)), card='bluecard')
    b = generate(budget(series=(s,)), D(2029, 3, 31))
    first = b.rows[0]
    b = replace(b, rows=tuple(replace(r, amount=25000, overridden=True) if r.id == first.id else r for r in b.rows))
    b = replace(b, series=(replace(s, amount=21000, card=None, name='Groceries'),))
    out = {r.id: r for r in regenerate_series(b, 'grocery', D(2029, 3, 1)).rows}
    assert (out[first.id].amount, out[first.id].card, out[first.id].name) == (25000, None, 'Groceries')
    assert all(r.card is None and r.amount == 21000 for r in out.values() if r.id != first.id)
