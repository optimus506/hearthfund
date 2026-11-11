import pytest

from factories import D, budget, row
from hbudget.core.ledger import (
    add_row,
    change_amount,
    clear_row,
    mark_done,
    mark_status,
    move_row,
    ordered,
    reduce_row,
    remove_row,
    undo_change,
)
from hbudget.core.models import Kind, RowState, Status

INS = row('ins', 'Home insurance', 21000, D(2029, 3, 12))
PAY = row('pay', 'Pay', -200000, D(2029, 3, 15), seq=0)
GYM = row('gym', 'Gym', 4500, D(2029, 3, 15), seq=1)


def b0():
    return budget(rows=(INS, PAY, GYM))


def get(b, rid):
    return next(r for r in b.rows if r.id == rid)


def test_change_and_undo():
    b = change_amount(b0(), 'ins', 18000)
    r = get(b, 'ins')
    assert (r.amount, r.amount_was, r.overridden) == (18000, 21000, True)
    b = change_amount(b, 'ins', 17000)
    assert get(b, 'ins').amount_was == 21000            # the first amount is kept for Undo
    b = undo_change(b, 'ins')
    assert (get(b, 'ins').amount, get(b, 'ins').amount_was) == (21000, None)
    with pytest.raises(ValueError):
        undo_change(b, 'ins')


def test_change_income_keeps_the_sign():
    assert get(change_amount(b0(), 'pay', 210000), 'pay').amount == -210000
    with pytest.raises(ValueError):
        change_amount(b0(), 'pay', -5)


def test_reduce_then_undo_of_an_earlier_change_keeps_the_payment():
    b = change_amount(b0(), 'ins', 18000)
    b = reduce_row(b, 'ins', 4000, 't1')
    r = get(b, 'ins')
    assert (r.amount, r.paid, r.paid_txn_ids, r.amount_was) == (14000, 4000, ('t1',), 21000)
    b = undo_change(b, 'ins')
    assert get(b, 'ins').amount == 17000                 # 210 - 40 paid since the change


def test_reduce_rules():
    with pytest.raises(ValueError):
        reduce_row(b0(), 'ins', 21000, 't')
    with pytest.raises(ValueError):
        reduce_row(b0(), 'ins', 0, 't')
    with pytest.raises(ValueError):
        reduce_row(b0(), 'pay', 100, 't')


def test_remove_and_undo():
    b = remove_row(b0(), 'gym')
    assert get(b, 'gym').state is RowState.REMOVED and get(b, 'gym').amount == 4500
    with pytest.raises(ValueError):
        remove_row(b, 'gym')
    with pytest.raises(ValueError):
        change_amount(b, 'gym', 1)
    assert get(undo_change(b, 'gym'), 'gym').state is RowState.OPEN


def test_mark_done_and_undo():
    b = mark_done(b0(), 'pay', D(2029, 3, 10))
    r = get(b, 'pay')
    assert r.state is RowState.CLEARED and r.cleared_txn_id is None and r.cleared_on == D(2029, 3, 10)
    with pytest.raises(ValueError):
        mark_done(b, 'pay', D(2029, 3, 10))
    back = get(undo_change(b, 'pay'), 'pay')
    assert back.state is RowState.OPEN and back.cleared_on is None and back.amount == -200000


def test_undo_refuses_a_row_cleared_by_the_bank():
    b = clear_row(b0(), 'gym', 't1', D(2029, 3, 15))
    with pytest.raises(ValueError):
        undo_change(b, 'gym')


def test_mark_status():
    b = mark_status(b0(), 'ins', Status.AUTOPAY)
    assert get(b, 'ins').status_override is Status.AUTOPAY
    assert get(mark_status(b, 'ins', None), 'ins').status_override is None


def test_clear_row():
    b = clear_row(b0(), 'ins', 'txn9', D(2029, 3, 11))
    r = get(b, 'ins')
    assert (r.state, r.cleared_txn_id, r.cleared_on) == (RowState.CLEARED, 'txn9', D(2029, 3, 11))
    with pytest.raises(ValueError):
        clear_row(b, 'ins', 'x', D(2029, 3, 14))


def test_unknown_row():
    for fn, args in ((change_amount, (1,)), (remove_row, ()), (undo_change, ()), (mark_status, (None,)),
                     (clear_row, ('t', D(2029, 3, 1))), (reduce_row, (1, 't'))):
        with pytest.raises(KeyError):
            fn(b0(), 'nope', *args)


def test_add_row_at_due_date_and_after_a_row():
    b = add_row(b0(), 'x1', 'Car repair', Kind.BILL, 40000, D(2029, 3, 15))
    assert get(b, 'x1').seq == 2 and get(b, 'x1').series_id is None
    b = add_row(b, 'x2', 'Refund', Kind.INCOME, 5000, D(2029, 3, 15))
    assert get(b, 'x2').seq == -1 and get(b, 'x2').amount == -5000
    b = add_row(b, 'x3', 'Gift', Kind.BILL, 2000, D(2029, 3, 10), order_after='pay', card='card')
    assert [r.id for r in ordered(b.rows)] == ['ins', 'x2', 'pay', 'x3', 'gym', 'x1']
    assert get(b, 'x3').order_date == D(2029, 3, 15) and get(b, 'x3').card == 'card'
    with pytest.raises(ValueError):
        add_row(b, 'x1', 'dup', Kind.BILL, 1, D(2029, 3, 1))
    with pytest.raises(ValueError):
        add_row(b, 'x4', 'Late', Kind.BILL, 1, D(2029, 3, 20), order_after='pay')
    with pytest.raises(ValueError):
        add_row(b, 'x5', 'Neg', Kind.BILL, -1, D(2029, 3, 20))


def test_move_row():
    b = budget(rows=(row('car', 'Car', 32000, D(2029, 3, 10)), PAY, INS))
    b = move_row(b, 'car', 'pay')
    car = get(b, 'car')
    assert (car.order_date, car.overridden) == (D(2029, 3, 15), True)
    assert [r.id for r in ordered(b.rows)] == ['ins', 'pay', 'car']
    with pytest.raises(ValueError):
        move_row(b, 'ins', 'ins')
    early = budget(rows=(row('a', 'A', 1, D(2029, 3, 1)), row('late', 'Late', 1, D(2029, 3, 20))))
    with pytest.raises(ValueError):
        move_row(early, 'late', 'a')


def test_set_series_amount_skips_seasonal_months_and_hand_edits():
    from dataclasses import replace

    import pytest

    from factories import D, budget, row, schedule, series
    from hbudget.core.ledger import set_series_amount
    s = series('gas', 'bill', 5000, schedule('monthly', D(2029, 1, 5), day=5), month_amounts=((4, 9000),))
    rows = (row('m3', 'Gas', 5000, D(2029, 3, 5), series_id='gas'),
            row('m4', 'Gas', 9000, D(2029, 4, 5), series_id='gas'),
            row('m5', 'Gas', 4000, D(2029, 5, 5), series_id='gas', overridden=True),
            row('m6', 'Gas', 5000, D(2029, 6, 5), series_id='gas'))
    b, changes = set_series_amount(budget(series=(s,), rows=rows), 'gas', 6000, D(2029, 3, 6))
    assert [(c.row_id, c.was, c.now) for c in changes] == [('m6', 5000, 6000)]
    assert b.series[0].amount == 6000 and {r.id: r.amount for r in b.rows}['m3'] == 5000
    with pytest.raises(ValueError, match='list of amounts'):
        set_series_amount(budget(series=(replace(s, cycle_amounts=(1, 2)),), rows=rows), 'gas', 1, D(2029, 3, 1))
    with pytest.raises(KeyError):
        set_series_amount(budget(), 'nope', 1, D(2029, 3, 1))


def test_set_series_amount_until_a_date_marks_the_rows_and_keeps_the_series():
    import pytest

    from factories import D, budget, row, schedule, series
    from hbudget.core.ledger import set_series_amount
    s = series('gas', 'bill', 5000, schedule('monthly', D(2029, 1, 5), day=5))
    rows = tuple(row('m%d' % m, 'Gas', 5000, D(2029, m, 5), series_id='gas') for m in (3, 4, 5))
    b, changes = set_series_amount(budget(series=(s,), rows=rows), 'gas', 2500, D(2029, 3, 1), D(2029, 4, 5))
    assert [(c.row_id, c.now) for c in changes] == [('m3', 2500), ('m4', 2500)]
    by = {r.id: r for r in b.rows}
    assert by['m3'].overridden and by['m4'].overridden and by['m5'].amount == 5000 and not by['m5'].overridden
    assert b.series[0].amount == 5000
    with pytest.raises(ValueError, match='before the start'):
        set_series_amount(budget(series=(s,), rows=rows), 'gas', 1, D(2029, 4, 1), D(2029, 3, 1))


def test_relabel_series_renames_open_rows_from_a_date_and_keeps_amounts() -> None:
    from dataclasses import replace

    from factories import schedule, series
    from hbudget.core.ledger import relabel_series
    s = series('power', 'bill', 9000, schedule('monthly', D(2029, 3, 5), day=5), name='Power bill', card='Card A')
    rows = (row('r1', 'Old name', 8800, D(2029, 3, 5), series_id='power'),
            row('r2', 'Old name', 9100, D(2029, 4, 5), series_id='power'),
            replace(row('r3', 'Old name', 9000, D(2029, 4, 20), series_id='power'), state=RowState.CLEARED),
            row('r4', 'Someone else', 500, D(2029, 4, 6)))
    out = {r.id: r for r in relabel_series(budget(series=(s,), rows=rows), 'power', D(2029, 4, 1)).rows}
    assert (out['r1'].name, out['r1'].amount) == ('Old name', 8800)              # before the date
    assert (out['r2'].name, out['r2'].card, out['r2'].amount) == ('Power bill', 'Card A', 9100)
    assert out['r3'].name == 'Old name'                                          # cleared rows stay
    assert out['r4'].name == 'Someone else'
    with pytest.raises(KeyError):
        relabel_series(budget(), 'nope', D(2029, 4, 1))
