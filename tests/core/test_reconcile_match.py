from dataclasses import replace

from recon_fixture import export, household

from factories import D, budget, chain, row, rule
from hbudget.core.models import Direction, RowState
from hbudget.core.reconcile import AutoMatch, Tracked, match, prepare


def run(b=None, rows=None):
    b = b or household()
    return match(b, prepare(rows or export(), b))


def test_full_scenario():
    r = run()
    assert r.auto == (AutoMatch('t2', 'm', 'mortgage'), AutoMatch('t7', 'pay', 'payroll'))
    got = [(p.id, p.txn_id, p.row_ids, p.kind, p.group) for p in r.proposals]
    assert got == [('P1', 't3', ('e1',), 'tolerance', 't3'), ('P2', 't4', ('ca',), 'ask', 't4'),
                   ('P3', 't4', ('cb',), 'ask', 't4'), ('P4', 't5', ('g1', 'f1', 'g2'), 'card_bundle', 't5'),
                   ('P5', 't6', (), 'expected', 't6')]
    assert r.proposals[0].note == 'bank amount differs from budget by $40.00'
    assert r.proposals[1].note.startswith('two cards from one bank; ')
    assert r.proposals[3].total == 37000 and '($370.00 of $350.00)' in r.proposals[3].note
    assert r.proposals[4].expected_id == 'refund' and r.proposals[4].total == -4000
    assert [(x.txn_id, x.rule_id) for x in r.tracked] == [('t8', 'shop'), ('t11', 'ignore')]
    assert isinstance(r.tracked[0], Tracked)
    assert [(u.txn_id, u.reason) for u in r.unknown] == [
        ('t6', 'no rule matches this payee'),
        ('t9', 'unexpected direction for electric (refund?)'),
        ('t10', 'card payment, but no open card-funded rows dated on or before it')]


def mini(rows, rules, items, start=100000):
    b = budget(start=start, as_of=D(2029, 2, 28), rows=rows, rules=rules)
    return match(b, prepare(chain(start, items), b)) if items else None


def test_oldest_bill_candidate_out_of_tolerance_becomes_a_proposal():
    rows = (row('w1', 'Water', 5000, D(2029, 3, 1), series_id='water'),
            row('w2', 'Water', 9000, D(2029, 3, 10), series_id='water'))
    r = mini(rows, (rule('water', 'bill', ('WATER',), targets=('water',)),), [('x', D(2029, 3, 5), -9000, 'WATER')])
    assert r.auto == () and [(p.row_ids, p.kind) for p in r.proposals] == [(('w1',), 'tolerance')]


def test_bill_tolerance_is_ten_percent_or_25_dollars():
    rows = (row('rent', 'Rent', 180000, D(2029, 3, 3)),)
    rules = (rule('rent', 'bill', ('RENT',), targets=('Rent',)),)       # targets may be row names
    assert mini(rows, rules, [('x', D(2029, 3, 3), -198000, 'RENT')]).auto     # +10%
    assert not mini(rows, rules, [('x', D(2029, 3, 3), -198100, 'RENT')]).auto
    small = (row('gym', 'Gym', 4500, D(2029, 3, 3)),)
    assert mini(small, (rule('g', 'bill', ('GYM',), targets=('Gym',)),), [('x', D(2029, 3, 3), -6500, 'GYM')]).auto


def test_date_window_12_before_21_after():
    rows = (row('gym', 'Gym', 4500, D(2029, 3, 20)),)
    rules = (rule('g', 'bill', ('GYM',), targets=('Gym',)),)
    assert mini(rows, rules, [('x', D(2029, 3, 8), -4500, 'GYM')]).auto
    early = mini(rows, rules, [('x', D(2029, 3, 7), -4500, 'GYM')])
    assert not early.auto and not early.unknown and early.proposals[0].kind == 'month'     # same month (#96)
    far = mini((row('gym', 'Gym', 4500, D(2029, 5, 20)),), rules, [('x', D(2029, 3, 7), -4500, 'GYM')])
    assert not far.auto and 'no open row in the date window' in far.unknown[0].reason
    late = (row('gym', 'Gym', 4500, D(2029, 3, 1)),)
    assert mini(late, rules, [('x', D(2029, 3, 22), -4500, 'GYM')]).auto
    assert not mini(late, rules, [('x', D(2029, 3, 23), -4500, 'GYM')]).auto


def test_pay_whatever_amount_and_no_candidate():
    rows = (row('p', 'Pay', -200000, D(2029, 3, 15), series_id='pay'),)
    rules = (rule('pay', 'pay', ('PAYROLL',), targets=('pay',)),)
    r = mini(rows, rules, [('x', D(2029, 3, 14), 150000, 'PAYROLL'), ('y', D(2029, 3, 15), 150000, 'PAYROLL')])
    assert r.auto == (AutoMatch('x', 'p', 'pay'),)
    assert r.unknown[0].reason == 'no open pay row near this date'


def test_card_greedy_stop_rule():
    rows = (row('a', 'Grocery', 16000, D(2029, 3, 1), card='c'), row('b', 'Fuel', 5000, D(2029, 3, 2), card='c'),
            row('x', 'Rent', 9000, D(2029, 3, 2)))
    rules = (rule('card', 'card', ('TO CARD',), card='c'),)
    r = mini(rows, rules, [('p', D(2029, 3, 5), -17000, 'TO CARD')])
    assert r.proposals[0].row_ids == ('a',)          # adding Fuel would move further from 170.00
    assert not r.proposals[0].can_reduce
    r = mini(rows, rules, [('p', D(2029, 3, 5), -20000, 'TO CARD')])
    assert r.proposals[0].row_ids == ('a', 'b')
    r = mini(rows, rules, [('p', D(2029, 3, 5), -10000, 'TO CARD')])
    assert r.proposals[0].row_ids == ('a',) and r.proposals[0].can_reduce   # at least one; 160 > 100


def test_ask_proposals_share_a_group_and_are_reserved():
    rows = (row('i', 'Home insurance', 21000, D(2029, 3, 12)), row('j', 'Home insurance', 21000, D(2029, 3, 20)))
    rules = (rule('ins', 'ask', ('HOME INSURER',), targets=('Home insurance',)),)
    r = mini(rows, rules, [('a', D(2029, 3, 11), -4000, 'HOME INSURER'), ('b', D(2029, 3, 16), -4000, 'HOME INSURER')])
    assert [(p.txn_id, p.row_ids, p.can_reduce) for p in r.proposals] == [('a', ('i',), True), ('b', ('j',), True)]


def test_two_genuine_identical_purchases_are_both_kept():
    r = mini((), (), [('a', D(2029, 3, 1), -500, 'COFFEE'), ('b', D(2029, 3, 1), -500, 'COFFEE')])
    assert [u.txn_id for u in r.unknown] == ['a', 'b']


def test_closed_rows_are_never_candidates():
    rows = (row('r', 'Rent', 180000, D(2029, 3, 3), state=RowState.CLEARED),)
    r = mini(rows, (rule('rent', 'bill', ('RENT',), targets=('Rent',)),), [('x', D(2029, 3, 3), -180000, 'RENT')])
    assert not r.auto and r.unknown


def test_expected_out_item_and_direction():
    b = replace(household(), rules=(), expected=(
        replace(household().expected[0], id='bill', label='Final bill', amount=5000, direction='out',
                date=D(2029, 3, 16)),))
    r = match(b, prepare(export(), b))
    assert [(p.txn_id, p.expected_id) for p in r.proposals] == [('t10', 'bill')]
    assert rule('x', 'track', ('A',), direction=Direction.IN).direction is Direction.IN


def test_zero_posting_is_an_unexpected_direction():
    rows = (row('r', 'Rent', 180000, D(2029, 3, 3)),)
    rules = (rule('rent', 'bill', ('RENT',), targets=('Rent',)), rule('pay', 'pay', ('PAYROLL',), targets=('x',)))
    r = mini(rows, rules, [('a', D(2029, 3, 2), -100, 'COFFEE'), ('z', D(2029, 3, 3), 0, 'RENT'),
                           ('y', D(2029, 3, 3), 0, 'PAYROLL')])
    assert [u.reason for u in r.unknown] == ['no rule matches this payee', 'unexpected direction for rent (refund?)',
                                             'unexpected direction for pay (refund?)']


def test_open_expected_item_without_a_fitting_posting_gives_no_proposal():
    from hbudget.core.models import ExpectedItem
    b = budget(start=100000, as_of=D(2029, 2, 28), expected=(
        ExpectedItem('big', 'Big refund', 90000, 'in', D(2029, 3, 5)),        # amount does not fit
        ExpectedItem('late', 'Late bill', 500, 'out', D(2029, 5, 1)),         # date window does not fit
        ExpectedItem('dir', 'Wrong way', 500, 'in', D(2029, 3, 5))))          # direction does not fit
    r = match(b, prepare(chain(100000, [('a', D(2029, 3, 5), -500, 'SHOP')]), b))
    assert r.proposals == () and [u.txn_id for u in r.unknown] == ['a']


def allowance_budget():
    rows = (row('s3', 'Subscriptions', 15500, D(2029, 3, 18), series_id='subs'),
            row('s4', 'Subscriptions', 15500, D(2029, 4, 18), series_id='subs'))
    rules = (rule('subs', 'allowance', ('PROCESSOR',), targets=('subs',), direction=Direction.OUT),)
    return budget(start=100000, as_of=D(2029, 2, 28), rows=rows, rules=rules)


def test_allowance_payments_use_up_the_months_row():
    b = allowance_budget()
    r = match(b, prepare(chain(100000, [('a', D(2029, 3, 2), -999, 'PROCESSOR'),
                                        ('b', D(2029, 3, 2), -1099, 'PROCESSOR'),
                                        ('c', D(2029, 3, 5), -2699, 'PROCESSOR')]), b))
    assert set(r.auto) == {AutoMatch('a', 's3', 'subs', 999), AutoMatch('b', 's3', 'subs', 1099),
                           AutoMatch('c', 's3', 'subs', 2699)}
    assert r.proposals == () and r.unknown == ()       # the month is not over: nothing is offered yet


def test_allowance_spills_into_the_next_row_and_offers_the_leftover_after_the_month():
    b = allowance_budget()
    r = match(b, prepare(chain(100000, [('a', D(2029, 3, 2), -15000, 'PROCESSOR'),
                                        ('b', D(2029, 3, 26), -1000, 'PROCESSOR'),
                                        ('x', D(2029, 4, 1), -10, 'OTHER')]), b))
    assert r.auto == (AutoMatch('a', 's3', 'subs', 15000), AutoMatch('b', 's3', 'subs'),
                      AutoMatch('b', 's4', 'subs', 500))
    assert r.proposals == ()          # March was used up exactly; April is not over
    # nothing charged in a finished month: the whole row is offered, never released on its own
    r = match(b, prepare(chain(100000, [('a', D(2029, 3, 2), -999, 'PROCESSOR'),
                                        ('x', D(2029, 4, 2), -10, 'OTHER')]), b))
    [p] = r.proposals
    assert (p.kind, p.row_ids, p.total, p.txn_id, p.can_reduce) == ('leftover', ('s3',), 14501, '', False)
    assert 'release the $145.01 left of $155.00' in p.note


def test_allowance_without_a_row_is_unknown():
    b = replace(allowance_budget(), rows=())
    r = match(b, prepare(chain(100000, [('a', D(2029, 3, 2), -999, 'PROCESSOR')]), b))
    assert r.auto == () and 'no open subs row this month or later' in r.unknown[0].reason


def test_a_bill_paid_in_pieces_is_offered_the_months_row():
    """#96: two $50 Zelle payments for a $75 monthly lawn row due later in the month: each is offered the row; the
    first takes $50 off, the second pays the rest."""
    from hbudget.core.reconcile import apply
    lawn = row('l11', 'Lawn care', 7500, D(2029, 3, 23), series_id='lawn')
    rules = (rule('lawn', 'bill', ('ZELLE LAWN',), targets=('lawn',)),)
    b = budget(start=100000, rows=(lawn,), rules=rules)
    txns = chain(100000, [('z1', D(2029, 3, 1), -5000, 'ZELLE LAWN'), ('z2', D(2029, 3, 2), -5000, 'ZELLE LAWN')])
    r = match(b, prepare(txns, b))
    assert [(p.txn_id, p.row_ids, p.kind, p.can_reduce) for p in r.proposals] == [
        ('z1', ('l11',), 'month', True), ('z2', ('l11',), 'month', True)]
    assert 'Lawn care is due 3/23 ($75.00)' in r.proposals[0].note
    one = apply(b, r, {'P1': 'reduce'}, today=D(2029, 3, 3))
    assert {x.id: (x.amount, x.state) for x in one.budget.rows}['l11'] == (2500, RowState.OPEN)
    both = apply(b, r, {'P1': 'reduce', 'P2': 'reduce'}, today=D(2029, 3, 3))
    assert {x.id: x.state for x in both.budget.rows}['l11'] is RowState.CLEARED
    # the next month's row when this month has none
    nxt = budget(start=100000, rows=(replace(lawn, due_date=D(2029, 4, 23)),), rules=rules)
    assert match(nxt, prepare(txns, nxt)).proposals[0].row_ids == ('l11',)
