"""Synthetic regression: a fake household whose numbers were worked out by hand (ARCHITECTURE 20.5).

The budgets below mirror tests/fixtures/synthetic/household.yaml and multiyear.yaml; test_yaml_mirrors_python
checks that the two agree, so the importer tests can load the YAML and expect the same numbers.

Household (start $1,000.00 on Thu 2029-03-01, fixed horizon to 2029-04-30), ledger order worked by hand.
Income comes first on its date; bills on one date by due date, then name. Car loan (due the 10th) is funded by
Pay A, so it is listed after the next Pay A. Apr 1 and Apr 15 are Sundays and Mar 31 a Saturday, so those paydays
move to the Friday before.

| order date | row (due date)               | amount   | balance  |
|------------|------------------------------|----------|----------|
| 03-01      | Pay A                        | +2000.00 | 3000.00  |
| 03-03      | Grocery                      |   160.00 | 2840.00  |
| 03-03      | Mortgage                     |  1800.00 | 1040.00  |
| 03-06      | Electric                     |   150.00 |  890.00  |
| 03-10      | Grocery                      |   160.00 |  730.00  |
| 03-14      | Lawn care (Mar: 40)          |    40.00 |  690.00  | <- low (first of two)
| 03-15      | Pay A                        | +2000.00 | 2690.00  |
| 03-15      | Car loan (due 03-10)         |   300.00 | 2390.00  |
| 03-17      | Grocery                      |   160.00 | 2230.00  |
| 03-24      | Grocery                      |   160.00 | 2070.00  |
| 03-25      | Spare (placeholder)          |     0.00 | 2070.00  |
| 03-30      | Pay A (nominal 04-01)        | +2000.00 | 4070.00  |
| 03-30      | Pay B (nominal 03-31)        | +1000.00 | 5070.00  | <- high
| 03-31      | Grocery                      |   160.00 | 4910.00  |
| 04-03      | Mortgage                     |  1800.00 | 3110.00  |
| 04-06      | Electric                     |   150.00 | 2960.00  |
| 04-07      | Grocery                      |   160.00 | 2800.00  |
| 04-13      | Pay A (nominal 04-15)        | +2000.00 | 4800.00  |
| 04-13      | Car loan (due 04-10)         |   300.00 | 4500.00  |
| 04-14      | Grocery                      |   160.00 | 4340.00  |
| 04-14      | Lawn care (Apr: 120)         |   120.00 | 4220.00  |
| 04-21      | Grocery                      |   160.00 | 4060.00  |
| 04-25      | Spare (placeholder)          |     0.00 | 4060.00  |
| 04-28      | Grocery                      |   160.00 | 3900.00  |
| 04-29      | Car insurance                |  3210.00 |  690.00  | <- the low again (the first one wins)
| 04-30      | Pay B                        | +1000.00 | 1690.00  | <- end

Bank export (tests/fixtures/synthetic/bank.csv), basis = the 02-28 posting that leaves $1,000.00:
| posted | description            | amount   | balance | result                                          |
|--------|------------------------|----------|---------|-------------------------------------------------|
| 03-01  | EMPLOYER A PAYROLL     | +2000.00 | 3000.00 | auto: Pay A 03-01                               |
| 03-02  | HOME LOAN SERVICER PMT | -1800.00 | 1200.00 | auto: Mortgage 03-03                            |
| 03-05  | ELECTRIC CO ACH        |  -190.00 | 1010.00 | P1 tolerance: Electric 03-06 (40.00 > 25.00)    |
| 03-06  | ONLINE MARKETPLACE     |   -30.00 |  980.00 | tracked                                         |
| 03-09  | MYSTERY SHOP           |   -25.00 |  955.00 | unknown                                         |
| 03-12  | TRANSFER TO CARD 1234  |  -320.00 |  635.00 | P2 card bundle: Grocery 03-03 + 03-10 = 320.00  |

After accepting P1 and P2: start $635.00 on 03-12. The open rows from 03-14 on run
595, 2595, 2295, 2135, 1975, 1975, 3975, 4975, 4815, 3015, 2865, 2705, 4705, 4405, 4245, 4125, 3965, 3965, 3805,
595, 1595: low $595.00 on 03-14 (again on 04-29), high $4,975.00 on 03-30, end $1,595.00 on 04-30.
The low moved by -95.00 = the electric overrun (40), the tracked purchase (30) and the unknown one (25).

Multi-year (start $100.00 on 2029-11-01 to 2031-02-28): Pay 2000 on the 1st, Rent 1900 on the 2nd, Holiday gifts
450 on Dec 10. After rent in month k (Nov 2029 = 0) the balance is 200 + 100 k, minus 450 per Dec 10 passed:
Nov-29 200, Dec-29 300 then -150 on Dec 10 (low), Jan-30 -50, ..., Nov-30 950, Dec-30 1050 then 600 on Dec 10,
Jan-31 700, Feb-31 800. High: Dec 1 2030 before rent, 1050 + 1900 = 2950.
Year ends: 2029 -150, 2030 600, 2031 800.
"""
import csv
from datetime import date
from pathlib import Path

import yaml

from factories import D, rule, schedule, series, txn
from hbudget.core import money
from hbudget.core.alerts import evaluate
from hbudget.core.forecast import compute
from hbudget.core.ledger import generate, horizon_end, ordered
from hbudget.core.models import Budget, Direction, Horizon, Kind, RowState, Status, Weekend
from hbudget.core.reconcile import apply, match, prepare

FIX = Path(__file__).resolve().parents[1] / 'fixtures' / 'synthetic'

SERIES = (
    series('pay-a', 'income', 200000, schedule('semimonthly', D(2029, 3, 1), days=(1, 15), weekend=Weekend.BEFORE),
           name='Pay A'),
    series('pay-b', 'income', 100000, schedule('monthly', D(2029, 3, 1), day=31, weekend=Weekend.BEFORE),
           name='Pay B'),
    series('mortgage', 'bill', 180000, schedule('monthly', D(2029, 3, 1), day=3), name='Mortgage',
           status=Status.AUTOPAY),
    series('electric', 'bill', 15000, schedule('monthly', D(2029, 3, 1), day=6), name='Electric'),
    series('car', 'bill', 30000, schedule('monthly', D(2029, 3, 1), day=10), name='Car loan', funded_by='pay-a'),
    series('lawn', 'bill', 4000, schedule('monthly', D(2029, 3, 1), day=14), name='Lawn care', card='rewards',
           month_amounts=tuple((m, 12000 if 4 <= m <= 8 else 4000) for m in range(1, 13))),
    series('placeholder', 'bill', 0, schedule('monthly', D(2029, 3, 1), day=25), name='Spare (placeholder)'),
    series('car-insurance', 'bill', 321000, schedule('yearly', D(2029, 1, 1), month=4, day=29), name='Car insurance'),
    series('grocery', 'spending', 16000, schedule('weekly', D(2029, 3, 3)), name='Grocery', card='rewards'),
)
RULES = (
    rule('payroll-a', 'pay', ('EMPLOYER A PAYROLL',), direction=Direction.IN, targets=('pay-a',), name='Pay A'),
    rule('mortgage', 'bill', ('HOME LOAN SERVICER',), targets=('mortgage',), name='Mortgage'),
    rule('electric', 'bill', ('ELECTRIC CO',), targets=('electric',), name='Electric'),
    rule('rewards', 'card', ('TRANSFER TO CARD 1234',), card='rewards', name='Rewards card payment'),
    rule('shopping', 'track', ('ONLINE MARKETPLACE',), direction=Direction.OUT, name='Online shopping'),
)

BALANCES = [3000, 2840, 1040, 890, 730, 690, 2690, 2390, 2230, 2070, 2070, 4070, 5070, 4910, 3110, 2960, 2800, 4800,
            4500, 4340, 4220, 4060, 4060, 3900, 690, 1690]


def household() -> Budget:
    b = Budget(start_balance=100000, as_of=D(2029, 3, 1), horizon=Horizon('fixed', end=D(2029, 4, 30)),
               series=SERIES, rows=(), rules=RULES)
    return generate(b, horizon_end(b))


def bank():
    with open(FIX / 'bank.csv', newline='') as f:
        raw = list(csv.DictReader(f))
    out = []
    for i, r in enumerate(raw):
        m, d, y = (int(x) for x in r['Date'].split('/'))
        out.append(txn('s%d' % (len(raw) - i), date(y, m, d), money.parse(r['Amount']), r['Description'],
                       money.parse(r['Balance']), i))
    return out


def test_ledger_order_and_balances_match_the_hand_table():
    b = household()
    f = compute(b, today=D(2029, 3, 1))
    assert [p.balance for p in f.points] == [x * 100 for x in BALANCES]
    assert [p.label for p in f.points][:8] == ['Pay A', 'Grocery', 'Mortgage', 'Electric', 'Grocery', 'Lawn care',
                                               'Pay A', 'Car loan']
    assert (f.start, f.low, f.low_date) == (100000, 69000, D(2029, 3, 14))      # first of the two minimums
    assert (f.high, f.high_date) == (507000, D(2029, 3, 30))
    assert (f.end, f.end_date, f.year_ends) == (169000, D(2029, 4, 30), ((2029, 169000),))
    car = next(r for r in b.rows if r.id == 'car:2029-03-10')
    assert (car.due_date, car.order_date) == (D(2029, 3, 10), D(2029, 3, 15))
    assert sum(1 for r in b.rows if r.amount == 0) == 2


def test_reconciliation_matches_the_hand_table():
    b = household()
    res = match(b, prepare(bank(), b))
    assert res.prepared.basis.description == 'CORNER CAFE' and res.prepared.latest_balance == 63500
    assert [(m.row_id, m.rule_id) for m in res.auto] == [('pay-a:2029-03-01', 'payroll-a'),
                                                         ('mortgage:2029-03-03', 'mortgage')]
    assert [(p.id, p.kind, p.row_ids) for p in res.proposals] == [
        ('P1', 'tolerance', ('electric:2029-03-06',)),
        ('P2', 'card_bundle', ('grocery:2029-03-03', 'grocery:2029-03-10'))]
    assert [t.txn_id for t in res.tracked] == ['s5'] and [u.txn_id for u in res.unknown] == ['s6']
    a = apply(b, res, {'P1': 'clear', 'P2': 'clear'}, today=D(2029, 3, 12))
    assert sum(r.state is RowState.CLEARED for r in a.budget.rows) == 5
    f = a.forecast_after
    assert (f.start, f.low, f.low_date, f.high, f.high_date, f.end) == (63500, 59500, D(2029, 3, 14), 497500,
                                                                         D(2029, 3, 30), 159500)
    assert f.low - a.forecast_before.low == -9500
    assert 'Lowest balance: **$595.00 on 03/14/2029**' in a.report_md


def multiyear() -> Budget:
    b = Budget(start_balance=10000, as_of=D(2029, 11, 1), horizon=Horizon('fixed', end=D(2031, 2, 28)), rows=(),
               series=(series('pay', 'income', 200000, schedule('monthly', D(2029, 11, 1), day=1), name='Pay'),
                       series('rent', 'bill', 190000, schedule('monthly', D(2029, 11, 1), day=2), name='Rent'),
                       series('holidays', 'bill', 45000, schedule('yearly', D(2029, 11, 1), month=12, day=10),
                              name='Holiday gifts')))
    return generate(b, horizon_end(b))


def test_multiyear_year_ends_and_below_zero():
    b = multiyear()
    f = compute(b, today=D(2029, 11, 1))
    assert (f.low, f.low_date) == (-15000, D(2029, 12, 10))
    assert (f.high, f.high_date) == (295000, D(2030, 12, 1))
    assert f.year_ends == ((2029, -15000), (2030, 60000), (2031, 80000))
    assert (f.end, f.end_date) == (80000, D(2031, 2, 2))
    assert evaluate(b, f, today=D(2029, 11, 1))[0].code == 'below_zero'


def _yaml(name):
    class Loader(yaml.SafeLoader):
        pass
    Loader.yaml_implicit_resolvers = {k: [r for r in v if r[0] != 'tag:yaml.org,2002:timestamp']
                                      for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    return yaml.load((FIX / name).read_text(), Loader=Loader)


def test_yaml_mirrors_python():
    doc = _yaml('household.yaml')
    assert money.parse(doc['household']['start_balance']) == household().start_balance
    file_series = {s['id']: (kind, money.parse(s['amount'])) for kind in ('income', 'bills', 'spending')
                   for s in doc.get(kind, [])}
    kinds = {'income': Kind.INCOME, 'bills': Kind.BILL, 'spending': Kind.SPENDING}
    assert {k: (kinds[v[0]], v[1]) for k, v in file_series.items()} == {s.id: (s.kind, s.amount) for s in SERIES}
    assert [r['id'] for r in doc['payee_rules']] == [r.id for r in RULES]
    multi = _yaml('multiyear.yaml')
    assert {s['id'] for k in ('income', 'bills') for s in multi[k]} == {s.id for s in multiyear().series}
    assert len(ordered(multiyear().rows)) == 16 + 16 + 2
