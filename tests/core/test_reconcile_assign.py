"""The person's own choice of rows for a bank line, and the look-ahead for card payments (D-107)."""
import pytest
from recon_fixture import export, household

from factories import D, budget, chain, row, rule
from hbudget.core import alerts
from hbudget.core.models import RowState
from hbudget.core.reconcile import Assign, apply, assignable, match, prepare

TODAY = D(2029, 3, 17)


def setup():
    b = household()
    return b, match(b, prepare(export(), b))


def early_payment(*extra):
    """A 600.00 card payment on the 15th; the rows it pays are dated the 16th and the 18th."""
    b = budget(start=100000, as_of=D(2029, 3, 10), rules=(rule('card', 'card', ('TRANSFER TO CARD',), card='card-a'),),
               rows=(row('a', 'Streaming', 3000, D(2029, 3, 16), card='card-a'),
                     row('b', 'Insurance', 20000, D(2029, 3, 16), seq=1, card='card-a'),
                     row('c', 'Grocery', 35000, D(2029, 3, 18), card='card-a', kind='spending'), *extra))
    txns = chain(100100, basis=False, items=[('t0', D(2029, 3, 10), -100, 'COFFEE'),
                                              ('t1', D(2029, 3, 15), -60000, 'TRANSFER TO CARD')])
    return b, match(b, prepare(txns, b))


def test_card_payment_dated_before_its_rows_is_proposed_not_automatic():
    b, res = early_payment()
    assert res.auto == () and res.unknown == ()
    (p,) = res.proposals
    assert (p.kind, p.txn_id, p.row_ids, p.total) == ('card_bundle', 't1', ('a', 'b', 'c'), 58000)
    assert p.note == 'card payment covers these card-funded rows ($580.00 of $600.00); dated after the payment'
    a = apply(b, res, {'P1': 'clear'}, today=TODAY)
    assert all(r.state is RowState.CLEARED for r in a.budget.rows)


def test_look_ahead_stops_after_a_week_and_rows_before_the_payment_win():
    b, res = early_payment()
    late = tuple(r for r in b.rows if r.id == 'c')        # only the row three days ahead
    far = budget(start=100000, as_of=D(2029, 3, 10), rules=b.rules,
                 rows=(row('z', 'Grocery', 35000, D(2029, 3, 23), card='card-a', kind='spending'),))
    txns = chain(100100, basis=False, items=[('t0', D(2029, 3, 10), -100, 'COFFEE'),
                                              ('t1', D(2029, 3, 15), -60000, 'TRANSFER TO CARD')])
    res = match(far, prepare(txns, far))
    assert res.proposals == ()
    assert res.unknown[0].reason == 'card payment, but no open card-funded rows dated up to 7 days after it'
    assert assignable(far, res) == {'t1': ('z',)}          # still offered when the person picks the rows
    # a row dated on or before the payment: the proposal is as before, later rows are not pulled in
    b2, res2 = early_payment(row('old', 'Fuel', 5000, D(2029, 3, 14), card='card-a', kind='spending'))
    assert [p.row_ids for p in res2.proposals] == [('old',)] and 'after' not in res2.proposals[0].note
    assert late and assignable(b2, res2)['t1'][0] == 'old'


def test_candidates_put_the_cards_rows_first_then_the_nearest():
    b, res = setup()
    c = assignable(b, res)
    # money-out lines only: unmatched (t10) and lines a proposal is about (t3, t4, t5); never tracked or money in
    assert set(c) == {'t3', 't4', 't5', 't10'}
    assert 'm' not in c['t10'] and 'pay' not in c['t10']           # auto-matched or income
    assert c['t5'][:3] == ('g2', 'g1', 'f1')                        # its own proposal's rows, nearest first
    assert c['t5'][3] == 'g3'                                       # then the card's other rows
    assert c['t4'][:2] == ('ca', 'cb') or c['t4'][:2] == ('cb', 'ca')


def test_chosen_rows_are_cleared_by_the_line():
    b, res = setup()
    a = apply(b, res, {}, today=TODAY, assign={'t10': Assign(('f1',))})
    f1 = next(r for r in a.budget.rows if r.id == 'f1')
    assert (f1.state, f1.cleared_txn_id, f1.cleared_on) == (RowState.CLEARED, 't10', D(2029, 3, 16))
    assert ('f1', 't10', 'chosen') in [(c.row_id, c.txn_id, c.how) for c in a.cleared]
    assert a.assigned == (('t10', 5000),)
    assert 'TRANSFER TO OTHER CARD | unmatched' not in a.report_md and '| chosen |' in a.report_md
    found = alerts.evaluate(a.budget, a.forecast_after, today=TODAY, recent=a)
    assert not [x for x in found if 'OTHER CARD' in x.message]


def test_less_than_the_payment_leaves_the_rest_unbudgeted():
    b, res = setup()
    # t3 is 190.00 against Electric 150.00 (P1): the person picks the row themselves, 40.00 is left over
    a = apply(b, res, {}, today=TODAY, assign={'t3': Assign(('e1',))})
    assert a.assigned == (('t3', 15000),)
    assert '| P1 |' not in a.report_md                     # no longer waiting for a decision
    assert 'ELECTRIC CO ACH | not budgeted: $40.00 of it (the rest paid the rows you chose)' in a.report_md


def test_more_than_the_payment_needs_a_row_that_keeps_the_rest():
    b, res = setup()
    with pytest.raises(ValueError, match=r'total \$160.00, more than the payment'):
        apply(b, res, {}, today=TODAY, assign={'t10': Assign(('g1',))})
    a = apply(b, res, {}, today=TODAY, assign={'t10': Assign((), 'g1')})
    g1 = next(r for r in a.budget.rows if r.id == 'g1')
    assert (g1.state, g1.amount, g1.paid, g1.paid_txn_ids) == (RowState.OPEN, 11000, 5000, ('t10',))
    assert a.assigned == (('t10', 5000),)
    # rows in full plus one larger row for the rest: t5 is 350.00 = Grocery 160.00 + Fuel 50.00 + 140.00 of Grocery
    a = apply(b, res, {}, today=TODAY, assign={'t5': Assign(('g1', 'f1'), 'g3')})
    left = {r.id: (r.state, r.amount) for r in a.budget.rows}
    assert left['g1'][0] is left['f1'][0] is RowState.CLEARED and left['g3'] == (RowState.OPEN, 2000)
    assert [(c.row_id, c.amount, c.how) for c in a.cleared if c.txn_id == 't5'] == [
        ('g1', 16000, 'chosen'), ('f1', 5000, 'chosen'), ('g3', 14000, 'chosen, part paid')]


@pytest.mark.parametrize(('accept', 'assign', 'message'), [
    ({'P1': 'clear'}, {'t3': Assign(('e2',))}, 'P1 is about a bank line you chose rows for'),
    ({}, {'t10': Assign(('f1',)), 't5': Assign(('f1', 'g1'))}, 'Fuel 3/3/2029 is already chosen for'),
    ({'P4': 'clear'}, {'t10': Assign(('f1',))}, 'already cleared by P4'),
    ({}, {'t10': Assign(('m',))}, 'already matched automatically'),
    ({}, {'t10': Assign(('pay',))}, 'not an open bill or spending row|already matched'),
    ({}, {'t10': Assign(('nope',))}, 'not an open bill or spending row'),
    ({}, {'t8': Assign(('f1',))}, 'not a money-out bank line waiting'),
    ({}, {'t9': Assign(('f1',))}, 'not a money-out bank line waiting'),
    ({}, {'t10': Assign()}, 'choose at least one row'),
    ({}, {'t10': Assign(('f1',), 'f1')}, 'chosen twice'),
    ({}, {'t10': Assign(('f1',), 'g1')}, 'can only keep the rest'),      # nothing is left for it
    ({}, {'t5': Assign((), 'f1')}, 'can only keep the rest'),            # 350.00 covers the 50.00 row whole
])
def test_refused_choices(accept, assign, message):
    b, res = setup()
    with pytest.raises(ValueError, match=message):
        apply(b, res, accept, today=TODAY, assign=assign)
