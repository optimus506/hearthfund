from factories import D
from hbudget.core.models import Account, AccountKind
from hbudget.core.payoff import monthly_interest, plan, simulate, utilization

START = D(2029, 4, 1)


def acct(id, balance, apr, payment, **kw):
    return Account(id=id, name=kw.pop('name', id), kind=kw.pop('kind', AccountKind.CREDIT_CARD), balance=balance,
                   apr_bp=apr, payment=payment, **kw)


def test_utilization_and_monthly_interest():
    a = acct('c', 300000, 2499, 40000, limit=800000)
    assert utilization(a) == 3750 and utilization(acct('x', 1, 0, 1)) is None
    assert monthly_interest(a) == 6248                  # 3000.00 * 24.99% / 12 = 62.475 -> 62.48
    assert monthly_interest(acct('z', 0, 2000, 1)) == 0


def test_single_debt_payoff_by_hand():
    # 1,000.00 at 12%: month 1 interest 10.00 -> 1010 - 500 = 510; month 2 interest 5.10 -> 515.10 - 500 = 15.10;
    # month 3 interest 0.15 -> 15.25 paid off
    r = simulate([acct('a', 100000, 1200, 50000)], method='avalanche', start=START)
    assert (r.months, r.done, r.total_interest) == (3, True, 1000 + 510 + 15)
    assert r.debts[0].paid_month == 3 and r.debts[0].paid_on == D(2029, 6, 1)


def test_payment_larger_than_the_balance():
    r = simulate([acct('a', 10000, 0, 50000)], method='snowball', start=START)
    assert (r.months, r.total_interest, r.debts[0].paid_month) == (1, 0, 1)


def test_payment_below_interest_never_finishes():
    r = simulate([acct('a', 100000, 2400, 1000)], method='avalanche', start=START, max_months=24)
    assert not r.done and r.months == 24 and r.debts[0].paid_month is None


def test_avalanche_vs_snowball_and_rollover():
    small = acct('small', 50000, 500, 10000)
    big = acct('big', 300000, 2400, 10000)
    av = simulate([small, big], method='avalanche', extra=20000, start=START)
    sb = simulate([small, big], method='snowball', extra=20000, start=START)
    assert av.total_interest < sb.total_interest
    by = lambda r: {d.id: d.paid_month for d in r.debts}  # noqa: E731
    assert by(sb)['small'] < by(av)['small']


def test_deferred_promo_gets_extra_money_first():
    # the D-013 case: a 0% deferred-interest promo must be cleared before a higher-APR card gets the extra
    promo = acct('promo', 60000, 0, 5000, promo_ends=D(2029, 10, 1), apr_after_bp=2699, deferred_interest=True)
    card = acct('card', 300000, 2499, 10000)
    r = simulate([promo, card], method='avalanche', extra=10000, start=START)
    by = {d.id: d for d in r.debts}
    assert by['promo'].paid_month == 4 and by['promo'].balance_at_promo == 0
    snow = simulate([promo, card], method='snowball', extra=10000, start=START)
    assert {d.id: d for d in snow.debts}['promo'].paid_month == 4


def test_promo_switches_apr_and_records_balance():
    promo = acct('promo', 100000, 0, 5000, promo_ends=D(2029, 6, 1), apr_after_bp=2400)
    r = simulate([promo], method='avalanche', start=START)
    d = r.debts[0]
    assert d.balance_at_promo == 90000 and d.interest > 0


def test_one_time_extra():
    a = acct('a', 100000, 0, 10000)
    r = simulate([a], method='avalanche', once=50000, once_month=D(2029, 5, 15), start=START)
    assert r.months == 5          # 100 + 600 in May, then 100 x 3


def test_plan_kpis_mortgage_and_promos():
    accts = [
        acct('chk', 50000, 0, 0, kind=AccountKind.CHECKING),
        acct('home', 24000000, 550, 180000, kind=AccountKind.MORTGAGE, is_mortgage=True, name='Mortgage'),
        acct('k401', 500000, 600, 20000, kind=AccountKind.RETIREMENT_LOAN, include_in_payoff=False, name='Plan loan'),
        acct('card', 300000, 2499, 40000),
        acct('promo', 150000, 0, 30000, promo_ends=D(2029, 9, 1), apr_after_bp=2699, deferred_interest=True,
             name='Appliance'),
        acct('stuck', 100000, 3000, 2000, name='Stuck'),
    ]
    p = plan(accts, method='avalanche', extra=10000, today=D(2029, 3, 17))
    assert p.start == START
    assert set(p.excluded) == {'Mortgage', 'Plan loan'}
    assert {d.id for d in p.result.debts} == {'card', 'promo', 'stuck'}
    assert p.not_covering == ('Stuck',)
    assert p.interest_saved == p.baseline.total_interest - p.result.total_interest > 0
    assert p.months_sooner is not None and p.months_sooner >= 0
    assert p.other_difference == p.other.total_interest - p.result.total_interest
    (v,) = p.promos
    assert v.debt_id == 'promo' and v.payments_left == 5 and v.monthly_needed == 30000 and v.deferred_interest
    assert v.paid_before and not v.apr_after_missing
    assert p.debt_free_on == max(d.paid_on for d in p.result.debts)
    with_home = plan(accts, method='snowball', today=D(2029, 3, 17), include_mortgage=True)
    assert 'Mortgage' not in with_home.excluded and with_home.method == 'snowball'


def test_plan_promo_not_paid_in_time_and_no_debts():
    promo = acct('promo', 150000, 0, 1000, promo_ends=D(2029, 6, 1), name='Appliance')
    (v,) = plan([promo], method='avalanche', today=D(2029, 3, 17)).promos
    assert not v.paid_before and v.left_at_promo == 148000 and v.apr_after_missing and v.monthly_needed == 75000
    empty = plan([], method='avalanche', today=D(2029, 3, 17))
    assert empty.result.done and empty.debt_free_on == START and empty.promos == ()
    never = plan([acct('x', 100000, 3000, 100)], method='avalanche', today=D(2029, 3, 17))
    assert never.debt_free_on is None and never.months_sooner is None


def test_simulate_records_the_total_owed_each_month():
    r = simulate([acct('a', 100000, 1200, 50000)], method='avalanche', start=START)
    assert r.balances == (100000, 51000, 1510, 0)
    p = plan([acct('a', 100000, 1200, 50000), acct('b', 20000, 0, 5000)], method='snowball', extra=10000,
             today=D(2029, 3, 15))
    assert p.result.balances[0] == p.baseline.balances[0] == 120000 and p.result.balances[-1] == 0
    assert len(p.result.balances) == p.result.months + 1 and len(p.baseline.balances) > len(p.result.balances)


def test_record_keeps_the_balance_history():
    from hbudget.core.payoff import HISTORY_MAX, record
    a = acct('a', 100000, 1200, 50000, as_of=D(2029, 1, 5))
    first = record(None, a)
    assert first.history == ((D(2029, 1, 5), 100000),)
    second = record(first, acct('a', 80000, 1200, 50000, as_of=D(2029, 2, 5)))
    assert second.history == ((D(2029, 1, 5), 100000), (D(2029, 2, 5), 80000))
    # the same date again: the latest balance wins; an edit that leaves the balance alone adds nothing
    fixed = record(second, acct('a', 79000, 1200, 50000, as_of=D(2029, 2, 5)))
    assert fixed.history[-1] == (D(2029, 2, 5), 79000) and len(fixed.history) == 2
    same = record(fixed, acct('a', 79000, 1500, 50000, as_of=D(2029, 2, 5)))
    assert same.history == fixed.history
    # an account from before history existed: its old balance is kept as the first point
    old = acct('a', 50000, 0, 1, as_of=D(2028, 6, 1))
    assert record(old, acct('a', 40000, 0, 1, as_of=D(2028, 7, 1))).history == ((D(2028, 6, 1), 50000),
                                                                                 (D(2028, 7, 1), 40000))
    many = acct('a', 1, 0, 1, history=tuple((D(2000, 1, 1).replace(year=2000 + i // 12, month=i % 12 + 1), i)
                                             for i in range(HISTORY_MAX)), as_of=D(2010, 1, 1))
    assert len(record(many, acct('a', 2, 0, 1, as_of=D(2030, 1, 1))).history) == HISTORY_MAX


def test_estimate_from_reconciled_payments_since_the_typed_balance():
    """#97: typed balance + a month of interest on each monthly anniversary - payments of the linked item since."""
    from dataclasses import replace as rep

    from factories import D, budget, row, schedule, series, txn
    from hbudget.core.models import Account, AccountKind, RowState
    from hbudget.core.payoff import estimate
    loan = Account('lender', 'Lender', AccountKind.LOAN, balance=1_000_000, apr_bp=1200, as_of=D(2029, 1, 10))
    s = rep(series('lender-pay', 'bill', 30000, schedule('monthly', D(2029, 1, 15), day=15)), debt='lender')
    rows = (row('r1', 'Lender', 30000, D(2029, 1, 15), series_id='lender-pay', state=RowState.CLEARED,
                cleared_txn_id='t1'),
            row('r2', 'Lender', 30000, D(2029, 2, 15), series_id='lender-pay', state=RowState.CLEARED,
                cleared_txn_id='t2'),
            row('r0', 'Lender', 30000, D(2028, 12, 15), series_id='lender-pay', state=RowState.CLEARED,
                cleared_txn_id='t0'))
    hist = [txn('t0', D(2028, 12, 15), -30000, 'LENDER'), txn('t1', D(2029, 1, 15), -30000, 'LENDER'),
            txn('t2', D(2029, 2, 16), -30000, 'LENDER')]
    b = budget(series=(s,), rows=rows, accounts=(loan,))
    e = estimate(loan, b, hist, today=D(2029, 3, 1))
    # 1/15: 1,000,000 - 30,000 = 970,000; 2/10: +1% = 9,700 -> 979,700; 2/16: -30,000 = 949,700; 3/10 not reached
    assert e is not None and (e.balance, e.paid, e.payments, e.interest) == (949_700, 60_000, 2, 9_700)
    assert e.since == D(2029, 1, 10)                         # the December payment was before the typed balance
    assert estimate(loan, b, hist, today=D(2029, 1, 12)) is None                   # nothing paid since
    assert estimate(rep(loan, as_of=None), b, hist, today=D(2029, 3, 1)) is None
    assert estimate(loan, budget(series=(rep(s, debt=None),), rows=rows, accounts=(loan,)), hist,
                    today=D(2029, 3, 1)) is None                                   # no item linked
