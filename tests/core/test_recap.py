"""The monthly recap and the year in review (D-090)."""
from dataclasses import replace

from factories import D, budget, row, rule, txn
from hbudget.core.models import CardTxn, Milestone, RowState, Settings
from hbudget.core.recap import periods_with_history, recap


def test_month_recap():
    hist = [txn('p1', D(2029, 2, 1), 300000, 'EMPLOYER PAY', balance=400000),
            txn('r1', D(2029, 2, 3), -150000, 'LANDLORD'),
            txn('g1', D(2029, 2, 9), -4000, 'CORNER MARKET'),
            txn('k1', D(2029, 2, 20), -30000, 'CARD CO PAYMENT'),
            txn('x1', D(2029, 2, 21), -99999, 'TO SAVINGS'),                 # ignored
            txn('p0', D(2029, 1, 1), 280000, 'EMPLOYER PAY'), txn('r0', D(2029, 1, 3), -150000, 'LANDLORD'),
            txn('s1', D(2028, 12, 5), -1299, 'PROCESSOR *MUSIC'), txn('s2', D(2029, 1, 5), -1299, 'PROCESSOR *MUSIC'),
            txn('s3', D(2029, 2, 5), -1499, 'PROCESSOR *MUSIC')]
    cards = [CardTxn('c1', D(2029, 2, 4), D(2029, 2, 5), -6000, 'GROCER', 'Grocery'),
             CardTxn('c2', D(2029, 1, 30), D(2029, 2, 1), -1000, 'GROCER', 'Grocery')]   # bought in January
    rules = (rule('pay', 'pay', ('EMPLOYER',)), rule('rent', 'bill', ('LANDLORD',), name='Rent'),
             rule('card', 'card', ('CARD CO',)), rule('sav', 'ignore', ('TO SAVINGS',)))
    rows = (row('rent2', 'Rent', 150000, D(2029, 2, 1), state=RowState.CLEARED, cleared_on=D(2029, 2, 3), paid=150000),
            row('ph2', 'Phone', 5000, D(2029, 2, 15)),                            # due and still open
            row('ph3', 'Phone', 5000, D(2029, 3, 15)))
    win = Milestone('debt:x', 'debt_paid', 'Paid off Card B', D(2029, 2, 14))
    b = replace(budget(rows=rows, rules=rules), settings=replace(Settings(), milestones=(win,)))
    r = recap(hist, cards, b, span='month', start=D(2029, 2, 10), today=D(2029, 3, 1))
    assert (r.start, r.end, r.complete, r.has_history) == (D(2029, 2, 1), D(2029, 3, 1), True, True)
    assert (r.totals.money_in, r.totals.money_out, r.totals.pay, r.totals.card_spent) == (300000, 185499, 300000, 6000)
    assert r.totals.net == 300000 - 185499
    assert (r.before.money_in, r.before.money_out) == (280000, 151299)
    assert r.top_payees[0] == ('Rent', 150000) and all(n != 'Card' for n, _ in r.top_payees)   # card payments left out
    assert r.card == (('Grocery', 6000, None),)
    assert r.bills_paid == (('Rent', D(2029, 2, 1), 150000),) and r.bills_open == (('Phone', D(2029, 2, 15), 5000),)
    assert [(i.payee, d, o, n) for i, d, o, n in r.price_changes] == [('PROCESSOR *MUSIC', D(2029, 2, 5), 1299, 1499)]
    assert r.wins == (win,) and r.last_balance is None                     # the last posting has no balance
    assert periods_with_history(hist, 'month')[-1] == D(2029, 2, 1)
    assert periods_with_history(hist, 'year') == [D(2028, 1, 1), D(2029, 1, 1)]


def test_year_and_empty_periods():
    hist = [txn('a', D(2028, 6, 1), 1000, 'X'), txn('b', D(2029, 2, 1), -500, 'Y')]
    y = recap(hist, [], budget(), span='year', start=D(2029, 5, 5), today=D(2029, 3, 1))
    assert (y.start, y.end, y.complete) == (D(2029, 1, 1), D(2030, 1, 1), False)
    assert (y.totals.money_out, y.before.money_in) == (500, 1000)
    empty = recap(hist, [], budget(), span='month', start=D(2027, 1, 1), today=D(2029, 3, 1))
    assert not empty.has_history and empty.totals.money_in == 0 and empty.top_payees == ()
