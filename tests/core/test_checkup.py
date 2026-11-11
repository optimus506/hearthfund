from factories import D, budget, row, rule, txn
from hbudget.core.checkup import compute
from hbudget.core.models import AlertSettings, CapRule, Direction, RowState, Settings

RULES = (rule('elec', 'bill', ('ELECTRIC',), targets=('electric',)),
         rule('pay', 'pay', ('PAYROLL',), direction=Direction.IN, targets=('pay',)),
         rule('cards', 'ask', ('CARD SERVICES',), targets=('card-a', 'card-b')),
         rule('shop', 'track', ('SHOP',)))
ROWS = (row('e1', 'Electric', 15000, D(2029, 4, 6), series_id='electric'),
        row('e2', 'Electric', 15000, D(2029, 5, 6), series_id='electric'),
        row('e3', 'Electric', 20000, D(2029, 6, 6), series_id='electric'),
        row('ec', 'Electric', 99999, D(2029, 3, 6), series_id='electric', state=RowState.CLEARED),
        row('p', 'Pay', -50000, D(2029, 4, 5), series_id='pay'),
        row('a', 'Card A', 30000, D(2029, 4, 18), series_id='card-a'),
        row('b', 'Card B', 70000, D(2029, 4, 7), series_id='card-b'))


def hist():
    return [
        txn('h1', D(2029, 1, 3), -10000, 'ELECTRIC CO'),           # January is not a full month (starts on the 3rd)
        txn('h2', D(2029, 2, 6), -16000, 'ELECTRIC CO'),
        txn('h3', D(2029, 2, 20), -2000, 'ELECTRIC CO'),           # two parts in one month: 180.00
        txn('h4', D(2029, 3, 6), -18000, 'ELECTRIC CO'),
        txn('h5', D(2029, 4, 2), -50000, 'ELECTRIC CO'),           # April is not full (history ends 4/10)
        txn('p1', D(2029, 2, 1), 40000, 'PAYROLL'), txn('p2', D(2029, 3, 1), 50000, 'PAYROLL'),
        txn('c1', D(2029, 2, 8), -30000, 'CARD SERVICES'),
        txn('s1', D(2029, 3, 5), -1000, 'SHOP'), txn('s2', D(2029, 3, 7), -2000, 'SHOP'),
        txn('s3', D(2029, 3, 13), -500, 'SHOP'),
        txn('x', D(2029, 4, 10), -100, 'UNKNOWN'),
    ]


def test_checkup():
    b = budget(rows=ROWS, rules=RULES,
               settings=Settings(alerts=AlertSettings(caps=(CapRule('w', 'Shop', ('shop',), 'week', 1000),))))
    c = compute(hist(), b)
    assert (c.since, c.through, c.transactions) == (D(2029, 1, 3), D(2029, 4, 10), 12)
    by = {x.rule_id: x for x in c.bills}
    e = by['elec']
    assert e.budget == 15000 and e.per == 'month' and e.count == 2       # Feb and Mar only
    assert e.average == 18000 and e.diff_tenths_pct == 200 and e.verdict == 'over'
    assert e.actuals == ((D(2029, 1, 1), 10000), (D(2029, 2, 1), 18000), (D(2029, 3, 1), 18000),
                         (D(2029, 4, 1), 50000))
    p = by['pay']
    assert (p.per, p.count, p.average, p.budget, p.diff_tenths_pct, p.verdict) == ('deposit', 2, 45000, 50000, -100,
                                                                                   'ok')
    assert by['cards'].budget is None and by['cards'].verdict == 'ok'   # two targets with rows: no single budget
    assert 'shop' not in by
    assert c.cap_weeks == (('w', ((D(2029, 3, 5), 3000), (D(2029, 3, 12), 500))),)


def test_checkup_under_and_empty():
    b = budget(rows=ROWS, rules=RULES)
    low = [txn('a', D(2029, 1, 1), -10000, 'ELECTRIC'), txn('b', D(2029, 2, 1), -10000, 'ELECTRIC'),
           txn('c', D(2029, 3, 1), -1, 'X')]
    e = compute(low, b).bills[0]
    assert e.verdict == 'under' and e.count == 2 and e.diff_tenths_pct == -333
    only_partial = compute([txn('a', D(2029, 1, 5), -15000, 'ELECTRIC')], b).bills[0]
    assert only_partial.count == 1 and only_partial.verdict == 'ok'     # no full month: all months used
    assert compute([], b).bills == () and compute([], b).since is None


def test_tracked_payees_by_week():
    c = compute(hist(), budget(rows=ROWS, rules=RULES))
    [(rid, name, weeks)] = c.tracked
    assert (rid, name, len(weeks)) == ('shop', 'shop', 8)
    assert weeks[-1] == (D(2029, 4, 9), 0)                      # the week of the last posting, empty
    assert dict(weeks)[D(2029, 3, 5)] == 3000 and dict(weeks)[D(2029, 3, 12)] == 500
    assert sum(v for _, v in weeks) == 3500
