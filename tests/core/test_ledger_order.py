from dataclasses import replace

from factories import D, budget, row, schedule, series
from hbudget.core.ledger import generate, order_key, ordered
from hbudget.core.models import Kind, Weekend

PAY = series('pay-a', 'income', 240000, schedule('semimonthly', D(2029, 3, 1), days=(1, 15), weekend=Weekend.BEFORE),
             name='Pay A')


def test_order_is_not_due_date_order():
    late_bill = row('car', 'Car loan', 32000, D(2029, 3, 10), order=D(2029, 3, 15), seq=1)
    pay = row('pay', 'Pay A', -240000, D(2029, 3, 15), seq=0)
    early = row('el', 'Electric', 15000, D(2029, 3, 12))
    assert [r.id for r in ordered([late_bill, pay, early])] == ['el', 'pay', 'car']
    assert order_key(late_bill) == (D(2029, 3, 15), 1, 'car')
    # same order_date and seq: id decides, so the order is total
    a, b = row('a', 'A', 1, D(2029, 3, 1)), row('b', 'B', 1, D(2029, 3, 1))
    assert ordered([b, a]) == [a, b]


def test_funded_by_orders_bill_after_the_next_payday():
    car = series('car', 'bill', 32000, schedule('monthly', D(2029, 3, 1), day=10), funded_by='pay-a', name='Car loan')
    b = generate(budget(series=(PAY, car)), D(2029, 3, 31))
    rows = ordered(b.rows)
    assert [(r.name, r.due_date, r.order_date) for r in rows] == [
        ('Pay A', D(2029, 3, 1), D(2029, 3, 1)),
        ('Pay A', D(2029, 3, 15), D(2029, 3, 15)),
        ('Car loan', D(2029, 3, 10), D(2029, 3, 15)),
    ]
    # Apr 1 (a Sunday) is paid Friday Mar 30, but its nominal date is after `until`, so it is not generated yet
    assert D(2029, 3, 30) not in [r.due_date for r in rows]


def test_funded_by_with_payday_shifted_by_a_weekend():
    car = series('car', 'bill', 32000, schedule('monthly', D(2029, 4, 1), day=10), funded_by='pay-a', name='Car loan')
    b = generate(budget(series=(PAY, car), as_of=D(2029, 4, 1)), D(2029, 4, 20))
    bill = next(r for r in b.rows if r.series_id == 'car')
    assert bill.order_date == D(2029, 4, 13)     # Apr 15 is a Sunday; pay moves to Friday the 13th
    pay = next(r for r in b.rows if r.due_date == D(2029, 4, 13) and r.kind is Kind.INCOME)
    assert ordered(b.rows).index(pay) < ordered(b.rows).index(bill)


def test_funded_by_bill_due_on_a_payday_whose_pay_moved_earlier():
    # nominal pay date (the 15th) is on/after the bill's nominal date (the 15th): the bill follows that pay,
    # and order_date never goes before the due date
    bill = series('ins', 'bill', 10000, schedule('monthly', D(2029, 4, 1), day=15), funded_by='pay-a')
    b = generate(budget(series=(PAY, bill), as_of=D(2029, 4, 1)), D(2029, 4, 20))
    r = next(r for r in b.rows if r.series_id == 'ins')
    assert r.due_date == D(2029, 4, 15) and r.order_date == D(2029, 4, 15)


def test_funded_by_lookahead_miss_keeps_due_date():
    yearly = series('bonus', 'income', 100000, schedule('yearly', D(2029, 12, 1)))
    bill = series('gym', 'bill', 4500, schedule('monthly', D(2029, 3, 1), day=5), funded_by='bonus')
    b = generate(budget(series=(yearly, bill)), D(2029, 3, 31))
    assert [r.order_date for r in b.rows] == [D(2029, 3, 5)]


def test_seq_income_first_then_bills_by_due_then_name():
    pay_b = series('pay-b', 'income', 150000, schedule('monthly', D(2029, 3, 1), day=15), name='Pay B')
    rent = series('rent', 'bill', 180000, schedule('monthly', D(2029, 3, 1), day=15), name='Rent')
    gym = series('gym', 'bill', 4500, schedule('monthly', D(2029, 3, 1), day=15), name='Gym')
    existing = row('typed', 'Typed by hand', 1000, D(2029, 3, 15), seq=0)
    b = generate(budget(series=(rent, gym, PAY, pay_b), rows=(existing,)), D(2029, 3, 15))
    on15 = [r for r in ordered(b.rows) if r.order_date == D(2029, 3, 15)]
    assert [r.name for r in on15] == ['Pay A', 'Pay B', 'Typed by hand', 'Gym', 'Rent']
    assert next(r for r in on15 if r.id == 'typed').seq == 0       # existing rows never move
    assert len({r.seq for r in on15}) == len(on15)


def test_generated_rows_after_existing_rows_of_the_date():
    rent = series('rent', 'bill', 180000, schedule('monthly', D(2029, 3, 1), day=3))
    existing = row('x', 'X', 500, D(2029, 3, 3), seq=4)
    b = generate(budget(series=(rent,), rows=(existing,)), D(2029, 3, 3))
    new = next(r for r in b.rows if r.series_id == 'rent')
    assert new.seq == 5
    assert replace(new, seq=0).order_date == D(2029, 3, 3)
