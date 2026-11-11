from factories import D, budget, row, rule, txn
from hbudget.core.flow import actual, months_back, planned
from hbudget.core.models import CardTxn, Direction, FlowGroup, RowState

RULES = (
    rule('pay', 'pay', ('PAYROLL',), direction=Direction.IN, targets=('pay-a',)),
    rule('save', 'savings_draw', ('FROM SAVINGS',), direction=Direction.IN),
    rule('loan', 'loan_in', ('LOAN DISB',), direction=Direction.IN),
    rule('rent', 'bill', ('RENT',), targets=('rent',), name='Rent'),
    rule('card', 'card', ('TO CARD',), card='c'),
    rule('skip', 'ignore', ('INTERNAL',)),
)
GROUPS = (FlowGroup('Home', '--f-home', rule_ids=('rent',), series_ids=('rent',)),
          FlowGroup('Food & everyday', '--f-food', series_ids=('grocery',)),
          FlowGroup('Debt payments', '#aa0000', rule_ids=('rent',), row_names=('Car loan',)))


def test_months_back():
    assert months_back(D(2029, 3, 31)) == (D(2028, 4, 1), D(2029, 4, 1))      # month end: March included
    assert months_back(D(2029, 3, 15)) == (D(2028, 3, 1), D(2029, 3, 1))      # mid-month: ends before March
    assert months_back(D(2029, 1, 10)) == (D(2028, 1, 1), D(2029, 1, 1))      # January
    assert months_back(D(2029, 1, 31), 2) == (D(2028, 12, 1), D(2029, 2, 1))


def hist():
    from factories import Weekend  # noqa: F401  (keeps the import list honest)
    out = [txn('old', D(2028, 1, 15), 100, 'OTHER IN')]
    for m in range(2, 14):
        y, mm = (2028, m) if m <= 12 else (2029, m - 12)
        out += [txn('p%d' % m, D(y, mm, 1), 300000, 'PAYROLL'), txn('r%d' % m, D(y, mm, 3), -180000, 'RENT'),
                txn('c%d' % m, D(y, mm, 20), -30000, 'TO CARD')]
    out += [txn('s', D(2028, 6, 1), 50000, 'FROM SAVINGS'), txn('l', D(2028, 7, 1), 100000, 'LOAN DISB'),
            txn('g', D(2028, 8, 1), 2500, 'GIFT'), txn('i', D(2028, 8, 2), -99999, 'INTERNAL'),
            txn('u', D(2028, 8, 3), -1200, 'CORNER GROCERY'), txn('last', D(2029, 1, 31), -100, 'CORNER CAFE')]
    return out


def test_actual_sources_groups_and_card_split():
    cards = [CardTxn('k1', D(2028, 5, 1), D(2028, 5, 2), -30000, 'MARKET', 'Grocery'),
             CardTxn('k2', D(2028, 5, 3), D(2028, 5, 4), -10000, 'CAFE', 'Eating out'),
             CardTxn('k3', D(2027, 5, 3), D(2027, 5, 4), -99999, 'OLD', 'Grocery'),
             CardTxn('k4', D(2028, 5, 5), D(2028, 5, 6), 40000, 'PAYMENT', 'Payment')]
    b = budget(rules=RULES, flow_groups=GROUPS)
    f = actual(hist(), cards, b, today=D(2029, 2, 5))
    assert (f.start, f.end, f.months, f.complete) == (D(2028, 2, 1), D(2029, 2, 1), 12, True)
    assert [(s.name, s.amount) for s in f.sources] == [('pay-a', 3600000), ('Loans', 100000), ('From savings', 50000),
                                                      ('Other money in', 2500)]
    g = {x.name: x for x in f.groups}
    assert g['Home'].total == 12 * 180000 and g['Home'].colour == '--f-home'   # first group listing the rule
    assert 'Debt payments' not in g
    card = g['Card']
    assert card.total == 12 * 30000 and [(i.name, i.amount) for i in card.items] == [('Grocery', 270000),
                                                                                     ('Eating out', 90000)]
    assert g['Food & everyday'].items[0].name in ('Other', 'Grocery')
    assert g['Left over'].total == f.sources[0].amount + 152500 - sum(x.total for x in f.groups[:-1])
    assert 'split by what was bought' in f.note


def test_actual_without_card_data_or_food_group_and_incomplete():
    b = budget(rules=RULES)
    f = actual(hist()[-5:], [], b, today=D(2029, 2, 5))
    assert not f.complete and 'covers 7 full months' in f.note and f.months == 7
    g = {x.name: x for x in f.groups}
    assert g['Other'].items[0].name in ('Other', 'Rent')
    f2 = actual(hist(), [], b, today=D(2029, 2, 5))
    assert {x.name: x for x in f2.groups}['Card'].items[0].name == 'Card payments'
    empty = actual([], [], b, today=D(2029, 2, 5))
    assert empty.sources == () and empty.groups == () and not empty.complete


def test_planned_window_and_groups():
    rows = (row('p', 'Pay A', -300000, D(2029, 3, 1), series_id='pay-a'),
            row('r', 'Rent', 180000, D(2029, 3, 3), series_id='rent'),
            row('c', 'Car loan', 32000, D(2029, 3, 10)),
            row('x', 'Misc', 1000, D(2029, 3, 11)),
            row('z', 'Zero', 0, D(2029, 3, 12)),
            row('d', 'Done', 5000, D(2029, 3, 12), state=RowState.CLEARED),
            row('t', 'Today', 7000, D(2029, 2, 28)),
            row('n', 'Next year', 7000, D(2030, 3, 1)))
    f = planned(budget(rows=rows, flow_groups=GROUPS), today=D(2029, 2, 28))
    assert (f.start, f.end) == (D(2029, 3, 1), D(2030, 3, 1))
    assert [(s.name, s.amount) for s in f.sources] == [('Pay A', 300000)]
    assert {g.name: g.total for g in f.groups} == {'Home': 180000, 'Debt payments': 32000, 'Other': 1000,
                                                   'Left over': 87000}


def test_planned_from_feb_29():
    f = planned(budget(), today=D(2028, 2, 28))
    assert (f.start, f.end) == (D(2028, 2, 29), D(2029, 3, 1))


def test_card_split_shares_add_up_exactly():
    from hbudget.core.flow import _split
    shares = _split(10000, {'a': 1, 'b': 1, 'c': 1})
    assert sum(shares.values()) == 10000 and sorted(shares.values()) == [3333, 3333, 3334]
    odd = _split(101, {'x': 2, 'y': 1})
    assert odd == {'x': 67, 'y': 34} and sum(odd.values()) == 101


def test_partial_first_month_is_left_out_and_months_are_counted():
    from factories import txn
    b = budget(rules=RULES)
    # history from mid-March to the end of May: March is partial, so April and May count
    h = [txn('a', D(2029, 3, 20), -9000, 'RENT'), txn('b', D(2029, 4, 10), -1000, 'RENT'),
         txn('c', D(2029, 5, 10), -3000, 'RENT'), txn('d', D(2029, 5, 31), 100, 'X')]
    f = actual(h, [], b, today=D(2029, 6, 2))
    assert (f.start, f.end, f.months, f.complete) == (D(2029, 4, 1), D(2029, 6, 1), 2, False)
    assert sum(g.total for g in f.groups if g.name != 'Left over') == 4000
    # the last 2 months, with a longer history, are complete
    r = actual([txn('z', D(2029, 1, 1), -1, 'RENT'), *h], [], b, today=D(2029, 6, 2), months=2)
    assert (r.start, r.months, r.complete) == (D(2029, 4, 1), 2, True)
    # less than a full month: what there is, as one month
    one = actual([txn('a', D(2029, 5, 20), -500, 'RENT')], [], b, today=D(2029, 5, 25))
    assert one.groups == () and not one.complete and 'less than one full month' in one.note
