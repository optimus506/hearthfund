from factories import D, budget, row
from hbudget.core.forecast import chart_points, compare, compute, series_for_chart
from hbudget.core.models import ExpectedItem, RowState, WhatIf


def test_running_balance_low_high_end():
    b = budget(start=100000, rows=(
        row('a', 'Rent', 80000, D(2029, 3, 3)),
        row('p', 'Pay', -50000, D(2029, 3, 15)),
        row('c', 'Car', 30000, D(2029, 3, 10), order=D(2029, 3, 15), seq=1),
        row('d', 'Gym', 20000, D(2029, 3, 20)),
    ))
    f = compute(b, today=D(2029, 3, 12))
    assert [p.balance for p in f.points] == [20000, 70000, 40000, 20000]
    assert (f.low, f.low_date, f.low_row_id) == (20000, D(2029, 3, 3), 'a')   # the first minimum wins
    assert (f.high, f.high_date) == (100000, D(2029, 3, 1))                  # the start is the maximum
    assert (f.end, f.end_date) == (20000, D(2029, 3, 20))
    assert f.now == 20000 and f.year_ends == ((2029, 20000),)


def test_low_excludes_start_and_high_point_after_start():
    b = budget(start=-5000, rows=(row('p', 'Pay', -100000, D(2029, 3, 5)), row('x', 'X', 10000, D(2029, 3, 6))))
    f = compute(b, today=D(2029, 3, 1))
    assert f.low == 85000 and f.low_date == D(2029, 3, 6)                   # start (-50.00) is not the low
    assert (f.high, f.high_date) == (95000, D(2029, 3, 5))
    assert f.now == -5000


def test_negative_start_and_low_below_zero():
    f = compute(budget(start=-10000, rows=(row('r', 'Rent', 5000, D(2029, 3, 2)),)), today=D(2029, 3, 1))
    assert f.low == -15000 and f.end == -15000


def test_zero_rows_count_and_no_rows():
    b = budget(start=1000, rows=(row('z', 'Placeholder', 0, D(2029, 3, 2)), row('x', 'X', 0, D(2029, 3, 3))))
    f = compute(b, today=D(2029, 3, 1))
    assert (f.low, f.low_row_id, f.low_date) == (1000, 'z', D(2029, 3, 2))
    empty = compute(budget(start=1234), today=D(2029, 3, 1))
    assert (empty.low, empty.low_date, empty.high, empty.end, empty.end_date) == (1234, D(2029, 3, 1), 1234, 1234,
                                                                                  D(2029, 3, 1))
    assert empty.points == () and empty.low_row_id is None and empty.year_ends == ((2029, 1234),)


def test_only_open_rows_count_and_row_amounts_override():
    b = budget(start=1000, rows=(row('a', 'A', 100, D(2029, 3, 2), state=RowState.CLEARED),
                                 row('b', 'B', 100, D(2029, 3, 3), state=RowState.REMOVED),
                                 row('c', 'C', 100, D(2029, 3, 4))))
    f = compute(b, today=D(2029, 3, 1), row_amounts={'c': 300})
    assert [p.row_id for p in f.points] == ['c'] and f.end == 700


def test_expected_items_and_whatifs_after_real_rows_of_the_date():
    b = budget(start=1000, rows=(row('r', 'Rent', 500, D(2029, 3, 10)),), expected=(
        ExpectedItem('e1', 'Refund', 200, 'in', D(2029, 3, 10)),
        ExpectedItem('e2', 'Old bill', 300, 'out', D(2029, 2, 20)),      # in the past: hits at as_of
        ExpectedItem('e3', 'Done', 999, 'out', D(2029, 3, 5), status='done'),
    ))
    f = compute(b, today=D(2029, 3, 1), whatifs=[WhatIf('Trip', 400, 'out', D(2029, 3, 10))])
    assert [(p.label, p.order_date, p.balance) for p in f.points] == [
        ('Old bill', D(2029, 3, 1), 700), ('Rent', D(2029, 3, 10), 200), ('Refund', D(2029, 3, 10), 400),
        ('Trip', D(2029, 3, 10), 0)]
    assert f.points[0].due_date == D(2029, 2, 20) and f.points[0].row_id is None
    assert compute(b, today=D(2029, 3, 1), include_expected=False).end == 500


def test_year_ends_over_several_years_carry_empty_years():
    b = budget(start=1000, as_of=D(2029, 11, 1), rows=(row('a', 'A', 100, D(2029, 12, 31)),
                                                       row('b', 'B', 200, D(2031, 1, 5))))
    f = compute(b, today=D(2029, 11, 1))
    assert f.year_ends == ((2029, 900), (2030, 900), (2031, 700))


def test_series_for_chart():
    b = budget(start=1000, rows=(row('a', 'A', 100, D(2029, 3, 1)), row('b', 'B', 100, D(2029, 3, 5)),
                                 row('c', 'C', 100, D(2029, 3, 5), seq=1)))
    assert series_for_chart(compute(b, today=D(2029, 3, 1))) == [(D(2029, 3, 1), 900), (D(2029, 3, 5), 700)]


def test_chart_counts_late_rows_on_the_balance_date():
    """Open rows ordered before the balance date never send the chart back in time (issue #13)."""
    b = budget(start=1000, as_of=D(2029, 3, 10), rows=(row('a', 'A', 100, D(2029, 3, 2)),
                                                       row('b', 'B', 100, D(2029, 3, 8), seq=1),
                                                       row('c', 'C', 100, D(2029, 3, 12), seq=2)))
    pts = chart_points(compute(b, today=D(2029, 3, 10)))
    assert [(d, c, n) for d, c, _, n in pts] == [(D(2029, 3, 10), 800, 2), (D(2029, 3, 12), 700, 1)]
    assert [p[0] for p in pts] == sorted(p[0] for p in pts)


def test_compare_judges_the_whatif():
    base = compute(budget(start=100000), today=D(2029, 3, 1))
    for whatif, verdict in ((5000, 'ok'), (80000, 'tight'), (120000, 'below_zero')):
        other = compute(budget(start=100000), today=D(2029, 3, 1),
                        whatifs=[WhatIf('x', whatif, 'out', D(2029, 3, 2))])
        d = compare(base, other, cushion=30000)
        assert d.verdict == verdict and d.end_change == -whatif and d.low_change == other.low - base.low


def test_year_ends_use_the_order_date_year():
    # a bill due Dec 30 but listed after the Jan 2 payday belongs to the next year's end
    b = budget(start=1000, as_of=D(2029, 12, 1), rows=(row('a', 'A', 100, D(2029, 12, 5)),
                                                       row('b', 'B', 300, D(2029, 12, 30), order=D(2030, 1, 2))))
    f = compute(b, today=D(2029, 12, 1))
    assert f.end_date == D(2029, 12, 30)
    assert f.year_ends == ((2029, 900), (2030, 600))


def test_card_transfers_on_payday():
    from dataclasses import replace

    from hbudget.core.models import Account, AccountKind, Settings
    from hbudget.core.paydays import periods
    rows = (row('s0', 'Streaming', 1500, D(2029, 3, 3), card='bluecard'),       # before the first payday: kept
            row('p1', 'Pay', -200000, D(2029, 3, 15)),
            row('r1', 'Rent', 50000, D(2029, 3, 15), seq=1),
            row('s1', 'Streaming', 1500, D(2029, 3, 20), card='bluecard'),
            row('g1', 'Groceries', 20000, D(2029, 3, 22), card='bluecard', kind='spending'),
            row('a1', 'Gas', 4000, D(2029, 3, 25), card='redcard'),
            row('p2', 'Pay', -200000, D(2029, 3, 29)),
            row('s2', 'Streaming', 1500, D(2029, 4, 20), card='bluecard'))
    off = budget(100000, rows=rows, accounts=(Account('bluecard', 'Bluecard', AccountKind('credit_card')),))
    on = replace(off, settings=replace(Settings(), card_transfers_on_payday=True))
    f_off, f_on = compute(off, today=D(2029, 3, 1)), compute(on, today=D(2029, 3, 1))
    assert f_on.end == f_off.end                                            # the same money, moved earlier
    pts = [(p.label, p.order_date, p.amount) for p in f_on.points]
    assert pts == [('Streaming', D(2029, 3, 3), 1500), ('Pay', D(2029, 3, 15), -200000),
                   ('Rent', D(2029, 3, 15), 50000), ('Move to Bluecard', D(2029, 3, 15), 21500),
                   ('Move to redcard', D(2029, 3, 15), 4000), ('Streaming', D(2029, 3, 20), 0),
                   ('Groceries', D(2029, 3, 22), 0), ('Gas', D(2029, 3, 25), 0), ('Pay', D(2029, 3, 29), -200000),
                   ('Move to Bluecard', D(2029, 3, 29), 1500), ('Streaming', D(2029, 4, 20), 0)]
    # Home's periods: the period's money out is the same, all of it on payday
    p_off, p_on = (periods(b, f, today=D(2029, 3, 1)) for b, f in ((off, f_off), (on, f_on)))
    assert [p.out for p in p_on] == [p.out for p in p_off]
    assert [p.balance_end for p in p_on] == [p.balance_end for p in p_off]
    after_pay = [p.balance for p in f_on.points if D(2029, 3, 15) <= p.order_date < D(2029, 3, 29)]
    assert min(after_pay) == p_on[1].balance_end == 98500 + 200000 - 50000 - 21500 - 4000   # all out on payday
