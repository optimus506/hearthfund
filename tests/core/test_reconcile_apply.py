import re
from dataclasses import replace

import pytest
from recon_fixture import export, household

from factories import D, budget, chain, row, rule, schedule, series
from hbudget.core.forecast import compute
from hbudget.core.models import Horizon, RowState
from hbudget.core.reconcile import apply, match, prepare, report


def setup():
    b = household()
    return b, match(b, prepare(export(), b))


def test_apply_auto_and_accepted():
    b, res = setup()
    a = apply(b, res, {'P1': 'clear', 'P3': 'clear', 'P4': 'clear', 'P5': 'clear'}, today=D(2029, 3, 17))
    nb = a.budget
    assert (nb.start_balance, nb.as_of, nb.last_reconciled_through) == (263400, D(2029, 3, 16), D(2029, 3, 16))
    state = {r.id: r.state for r in nb.rows}
    assert {k for k, v in state.items() if v is RowState.CLEARED} == {'m', 'pay', 'e1', 'cb', 'g1', 'f1', 'g2'}
    assert nb.expected[0].status == 'done'
    assert [(c.row_id, c.how) for c in a.cleared] == [('m', 'auto'), ('pay', 'auto'), ('e1', 'accepted P1'),
                                                      ('cb', 'accepted P3'), ('g1', 'accepted P4'),
                                                      ('f1', 'accepted P4'), ('g2', 'accepted P4')]
    cleared = next(r for r in nb.rows if r.id == 'm')
    assert (cleared.cleared_txn_id, cleared.cleared_on) == ('t2', D(2029, 3, 2))
    assert a.still_open == ()                      # e2 3/20, ca 3/18, g3 3/17 are after 3/16
    assert a.forecast_before == compute(b, today=D(2029, 3, 17))
    assert a.forecast_after.start == 263400
    assert a.report_md.startswith('# Budget reconciliation\n')


def test_apply_errors():
    b, res = setup()
    with pytest.raises(ValueError, match='unknown proposal'):
        apply(b, res, {'P99': 'clear'}, today=D(2029, 3, 17))
    with pytest.raises(ValueError, match='same bank transaction'):
        apply(b, res, {'P2': 'clear', 'P3': 'clear'}, today=D(2029, 3, 17))
    with pytest.raises(ValueError, match='partial'):
        apply(b, res, {'P1': 'reduce'}, today=D(2029, 3, 17))
    with pytest.raises(ValueError, match='clear or reduce'):
        apply(b, res, {'P1': 'maybe'}, today=D(2029, 3, 17))  # type: ignore[dict-item]


def test_reduce_partial_payment_and_still_open():
    rows = (row('ins', 'Home insurance', 21000, D(2029, 3, 12)), row('old', 'Old bill', 999, D(2029, 3, 1)))
    b = budget(start=100000, as_of=D(2029, 2, 28), rows=rows,
               rules=(rule('ins', 'ask', ('HOME INSURER',), targets=('Home insurance',)),))
    res = match(b, prepare(chain(100000, [('x', D(2029, 3, 11), -4000, 'HOME INSURER')]), b))
    a = apply(b, res, {'P1': 'reduce'}, today=D(2029, 3, 12))
    ins = next(r for r in a.budget.rows if r.id == 'ins')
    assert (ins.state, ins.amount, ins.paid, ins.paid_txn_ids) == (RowState.OPEN, 17000, 4000, ('x',))
    assert a.cleared[0].how == 'reduced P1' and a.cleared[0].amount == 4000
    assert a.still_open == ('old',)              # 'ins' is due after the export ends


def test_rolling_horizon_generates_after_apply():
    rent = series('rent', 'bill', 180000, schedule('monthly', D(2029, 1, 1), day=3))
    b = budget(start=100000, as_of=D(2029, 2, 28), series=(rent,), horizon=Horizon('rolling', months=2))
    res = match(b, prepare(chain(100000, [('x', D(2029, 3, 14), -500, 'COFFEE')]), b))
    a = apply(b, res, {}, today=D(2029, 3, 15))
    assert sorted(r.due_date for r in a.budget.rows) == [D(2029, 4, 3), D(2029, 5, 3)]


def test_report_sections_and_no_long_numbers():
    b, _ = setup()
    b = replace(b, rows=tuple(replace(r, name='Card ending 123456789') if r.id == 'ca' else r for r in b.rows))
    a = apply(b, match(b, prepare(export(), b)), {'P1': 'clear'}, today=D(2029, 3, 17))
    text = a.report_md
    for want in ('## Forecast after this update', '## Alerts', '## Cleared rows', '## Needs your decision',
                 '## Bank items with no budget row', '## Budget rows still open', '$2,634.00',
                 'matched the bank balance after 2/28', 'P4', 'not budgeted: shop', 'unmatched: no rule'):
        assert want in text, want
    dry = report(a, dry_run=True, today=D(2029, 3, 17))
    assert dry.startswith('# Budget reconciliation (dry run)')
    assert not re.search(r'\d{5,}', text.replace('123456789', '')), 'long number in the report'


def test_report_without_balance_column_and_nothing_cleared():
    from factories import txn
    b = replace(budget(start=1000), last_reconciled_through=D(2029, 3, 1))
    res = match(b, prepare([txn('a', D(2029, 3, 2), -100, 'COFFEE')], b))
    a = apply(b, res, {}, today=D(2029, 3, 3))
    assert 'no balance column' in a.report_md and 'None.' in a.report_md


def test_report_notes_ambiguous_basis_ignored_rows_and_caps():
    from hbudget.core.models import AlertSettings, CapRule, ExpectedItem, Settings
    shop = rule('shop', 'track', ('SHOP',))
    cap = CapRule('w', 'Shopping', ('shop',), 'week', 1000)
    b = budget(start=1000, as_of=D(2029, 2, 28), rules=(shop,),
               settings=Settings(alerts=AlertSettings(caps=(cap,))),
               expected=(ExpectedItem('done', 'Old', 500, 'out', D(2029, 3, 2), status='done'),))
    items = [('a', D(2029, 3, 1), 500, 'X'), ('b', D(2029, 3, 2), -500, 'Y'), ('c', D(2029, 3, 3), -2000, 'SHOP')]
    res = match(b, prepare(chain(1000, items), b, ignored=3))
    assert res.prepared.basis_ambiguous and res.proposals == ()
    text = apply(b, res, {}, today=D(2029, 3, 3)).report_md
    assert 'appears more than once' in text and '3 non-posted row(s)' in text
    assert '## Spending caps' in text and 'Shopping: $20.00 this week (cap $10.00' in text


def test_txn_label():
    from factories import txn
    from hbudget.core.reconcile import txn_label
    assert txn_label(txn('a', D(2029, 3, 5), -15000, 'ELECTRIC CO ' + 'X' * 50)) == \
        '3/5 -$150.00 ELECTRIC CO ' + 'X' * 28       # the description is cut at 40 characters
    assert txn_label(txn('b', D(2029, 12, 31), 240000, 'PAYROLL')) == '12/31 $2,400.00 PAYROLL'


def test_apply_allowance_and_release_the_leftover():
    from test_reconcile_match import allowance_budget

    from factories import chain
    b = allowance_budget()
    items = [('a', D(2029, 3, 2), -999, 'PROCESSOR'), ('b', D(2029, 3, 26), -15000, 'PROCESSOR'),
             ('x', D(2029, 4, 1), -10, 'OTHER')]
    res = match(b, prepare(chain(100000, items), b))
    assert [(m.row_id, m.amount) for m in res.auto] == [('s3', 999), ('s3', None), ('s4', 499)]
    done = apply(b, res, {}, today=D(2029, 4, 2))
    rows = {r.id: r for r in done.budget.rows}
    assert rows['s3'].state is RowState.CLEARED and rows['s3'].paid == 999
    assert rows['s4'].state is RowState.OPEN and rows['s4'].amount == 15500 - 499
    assert [(c.row_id, c.amount, c.how) for c in done.cleared] == [('s3', 999, 'allowance'), ('s3', 14501, 'auto'),
                                                                   ('s4', 499, 'allowance')]
    # a month with money left: released only when accepted
    res = match(b, prepare(chain(100000, [items[0], ('x', D(2029, 4, 1), -10, 'OTHER')]), b))
    [p] = res.proposals
    kept = apply(b, res, {}, today=D(2029, 4, 2))
    assert {r.id: r for r in kept.budget.rows}['s3'].amount == 15500 - 999
    freed = apply(b, res, {p.id: 'clear'}, today=D(2029, 4, 2))
    s3 = {r.id: r for r in freed.budget.rows}['s3']
    assert s3.state is RowState.CLEARED and s3.cleared_txn_id is None
    with pytest.raises(ValueError, match='partial'):
        apply(b, res, {p.id: 'reduce'}, today=D(2029, 4, 2))
