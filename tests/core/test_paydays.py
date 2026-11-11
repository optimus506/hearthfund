"""core.paydays: what is due before the next payday and the periods between later paydays."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from factories import D, budget, row
from hbudget.core import forecast as fc
from hbudget.core.models import ExpectedItem, RowState
from hbudget.core.paydays import periods

TODAY = D(2029, 3, 1)


def make(*rows, **kw):
    b = budget(100000, rows=tuple(rows), **kw)
    return b, periods(b, fc.compute(b, today=TODAY), today=TODAY)


def test_no_payday_means_no_periods():
    _, p = make(row('r1', 'Rent', 50000, D(2029, 3, 5)))
    assert p == []


def test_period_zero_and_the_paydays_after_it():
    _, p = make(row('late', 'Phone', 5000, D(2029, 2, 27)),            # open and late: still to come out
                row('r1', 'Rent', 50000, D(2029, 3, 5)),
                row('p1', 'Pay', -200000, D(2029, 3, 15)),
                row('r2', 'Electric', 10000, D(2029, 3, 15), seq=1),  # on the payday: the next period
                row('p2', 'Pay', -200000, D(2029, 3, 29)),
                row('r3', 'Card', 30000, D(2029, 4, 2)))
    assert [(x.start, x.end) for x in p] == [(None, D(2029, 3, 15)), (D(2029, 3, 15), D(2029, 3, 29)),
                                             (D(2029, 3, 29), None)]
    zero = p[0]
    assert [r.id for r in zero.rows] == ['late', 'r1']
    assert (zero.pay_in, zero.out, zero.balance_end, zero.low) == (0, 55000, 45000, 45000)
    assert [r.id for r in p[1].rows] == ['r2']
    assert (p[1].pay_in, p[1].out, p[1].balance_end, p[1].low) == (200000, 10000, 235000, 45000)
    assert (p[2].pay_in, p[2].out, p[2].balance_end) == (200000, 30000, 405000)


def test_count_limits_the_periods_and_the_last_ends_at_the_next_payday():
    rows = [row('p%d' % i, 'Pay', -1000, D(2029, 3, 1) + timedelta(days=7 * i)) for i in range(6)]
    _, p = make(*rows)
    assert len(p) == 4 and p[-1].end == D(2029, 3, 22)
    # a payday today is the first payday: period 0 is empty and opens with the checkbook balance
    assert p[0].end == TODAY and p[0].rows == () and p[0].balance_end == 100000


def test_cleared_rows_and_past_paydays_are_left_out():
    _, p = make(replace(row('old', 'Pay', -9000, D(2029, 2, 20)), state=RowState.CLEARED),
                row('old2', 'Pay', -9000, D(2029, 2, 25)),          # open but before today: not a payday
                replace(row('paid', 'Rent', 50000, D(2029, 3, 3)), state=RowState.CLEARED),
                row('p1', 'Pay', -1000, D(2029, 3, 10)))
    assert p[0].end == D(2029, 3, 10) and p[0].rows == () and p[0].pay_in == 9000


def test_expected_items_count_in_the_totals_but_are_not_rows():
    _, p = make(row('p1', 'Pay', -1000, D(2029, 3, 10)),
                expected=(ExpectedItem(id='e1', label='Refund', amount=2500, direction='in', date=D(2029, 3, 4)),))
    assert p[0].pay_in == 2500 and p[0].rows == () and p[0].balance_end == 102500


def test_card_transfers_per_period_and_plan():
    from hbudget.core.paydays import transfer_plan
    b, p = make(row('late', 'Gym', 3000, D(2029, 2, 27), card='bluecard'),
                row('r1', 'Rent', 50000, D(2029, 3, 5)),
                row('s1', 'Streaming', 1500, D(2029, 3, 6), card='bluecard'),
                row('g1', 'Groceries', 20000, D(2029, 3, 7), card='redcard', kind='spending'),
                row('x1', 'Done', 999, D(2029, 3, 8), card='bluecard', state=RowState.CLEARED),
                row('p1', 'Pay', -200000, D(2029, 3, 15)),
                row('s2', 'Streaming', 1500, D(2029, 4, 6), card='bluecard'))
    zero = p[0]
    assert [(t.card, t.total, [r.id for r in t.rows]) for t in zero.transfers] == [
        ('bluecard', 4500, ['late', 's1']), ('redcard', 20000, ['g1'])]
    assert [(t.card, t.total) for t in p[1].transfers] == [('bluecard', 1500)]
    plan = transfer_plan(b, 'bluecard', through=D(2029, 3, 31))
    assert plan.total == 4500 and [r.id for r in plan.rows] == ['late', 's1']
    assert transfer_plan(b, 'bluecard', through=D(2029, 4, 30)).total == 6000
    assert transfer_plan(b, 'none', through=D(2029, 4, 30)).rows == ()
