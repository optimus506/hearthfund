from datetime import timedelta

from factories import D, budget, row, rule, txn
from hbudget.core.milestones import detect, merge
from hbudget.core.models import Account, AccountKind, AlertSettings, Goal, RowState, Settings


def keys(ms):
    return [m.key for m in ms]


def test_debt_paid_off_and_goal_reached():
    paid = Account('a', 'Card A', AccountKind.CREDIT_CARD, balance=0, as_of=D(2029, 3, 1),
                   history=((D(2029, 1, 1), 50000), (D(2029, 3, 1), 0)))
    never = Account('n', 'Card N', AccountKind.CREDIT_CARD, balance=0)          # never owed anything: no win
    owing = Account('o', 'Loan', AccountKind.LOAN, balance=1000, history=((D(2029, 1, 1), 5000),))
    b = budget(accounts=(paid, never, owing),
               settings=Settings(goals=(Goal('g', 'Trip', 'savings', target=1000, saved=1000),
                                        Goal('h', 'House', 'savings', target=9000, saved=1))))
    found = detect(b, [], today=D(2029, 3, 10))
    assert keys(found) == ['debt:a', 'goal:g']
    assert found[0].text == 'Card A is paid off' and found[0].on == D(2029, 3, 1)


def test_weeks_above_the_cushion():
    s = Settings(alerts=AlertSettings(cushion=10000), week_starts=0)
    start = D(2029, 1, 1)                     # a Monday
    hist = [txn('t%d' % i, start + timedelta(days=7 * i + 2), -1, 'X', balance=20000) for i in range(9)]
    found = detect(budget(settings=s), hist, today=start + timedelta(weeks=9))
    assert keys(found) == ['cushion:4:2029-01-01', 'cushion:8:2029-01-01']
    # one week under breaks the run: counting starts again after it
    hist[5] = txn('t5', start + timedelta(days=37), -1, 'X', balance=5000)
    assert keys(detect(budget(settings=s), hist, today=start + timedelta(weeks=9))) == []


def test_month_under_budget():
    rows = (row('r1', 'Rent', 100000, D(2029, 2, 1), state=RowState.CLEARED),
            row('r2', 'Food', 40000, D(2029, 2, 15), state=RowState.CLEARED),
            row('x', 'Gone', 99999, D(2029, 2, 20), state=RowState.REMOVED),
            row('r3', 'Rent', 100000, D(2029, 3, 1)))
    hist = [txn('a', D(2029, 1, 20), -1, 'RENT'), txn('b', D(2029, 2, 1), -100000, 'RENT'),
            txn('c', D(2029, 2, 16), -30000, 'SHOP'), txn('d', D(2029, 2, 17), -999999, 'MOVE TO SAVINGS'),
            txn('e', D(2029, 3, 2), -5000, 'SHOP')]
    b = budget(rows=rows, rules=(rule('sav', 'ignore', ('SAVINGS',)),))
    found = detect(b, hist, today=D(2029, 3, 10))
    assert keys(found) == ['under:2029-02'] and found[0].text == 'February 2029: $100.00 under budget'
    # January is only partly covered (history starts on the 20th), March is not over: neither counts
    assert detect(budget(rows=rows), hist[1:3], today=D(2029, 3, 10))[0].key == 'under:2029-02'


def test_merge_keeps_each_win_once():
    b = budget(settings=Settings(goals=(Goal('g', 'Trip', 'savings', target=1, saved=1),)))
    found = detect(b, [], today=D(2029, 3, 1))
    kept, new = merge((), found)
    assert keys(new) == ['goal:g'] and keys(kept) == ['goal:g']
    again, new2 = merge(kept, detect(b, [], today=D(2029, 4, 1)))
    assert new2 == [] and again == kept
