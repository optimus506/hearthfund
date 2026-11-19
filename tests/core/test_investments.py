"""Investments (D-102): balances by date, thinning, the total by date and the change over a period."""
from datetime import timedelta

from factories import D
from hbudget.core import investments as inv
from hbudget.core.goals import total_debt
from hbudget.core.models import Account, AccountKind
from hbudget.core.payoff import plan

TODAY = D(2029, 3, 1)


def acct(id, *points):
    a = Account(id=id, name=id, kind=AccountKind.INVESTMENT, include_in_payoff=False)
    for day, v in points:
        a = inv.record(a, day, v, TODAY)
    return a


def test_record_keeps_the_newest_as_the_balance():
    a = acct('k', (D(2029, 2, 1), 100000))
    assert (a.balance, a.as_of, a.history) == (100000, D(2029, 2, 1), ((D(2029, 2, 1), 100000),))
    a = inv.record(a, D(2029, 3, 1), 120000, TODAY)
    older = inv.record(a, D(2028, 12, 31), 90000, TODAY)           # an old statement fills the history only
    assert (older.balance, older.as_of) == (120000, D(2029, 3, 1))
    assert [d for d, _ in older.history] == [D(2028, 12, 31), D(2029, 2, 1), D(2029, 3, 1)]
    again = inv.record(older, D(2029, 3, 1), 125000, TODAY)        # the same date: the later entry wins
    assert again.balance == 125000 and len(again.history) == 3


def test_forget_takes_one_out_but_never_the_last():
    a = acct('k', (D(2029, 2, 1), 100000), (D(2029, 3, 1), 120000))
    b = inv.forget(a, D(2029, 3, 1))
    assert (b.balance, b.as_of, len(b.history)) == (100000, D(2029, 2, 1), 1)
    assert inv.forget(b, D(2029, 2, 1)) is b and inv.forget(a, D(2020, 1, 1)) is a


def test_thin_daily_then_weekly_then_monthly_and_the_first_stays():
    start = TODAY - timedelta(days=1500)
    pts = [(start + timedelta(days=i), 1000 + i) for i in range(1501)]
    out = inv.thin(pts, TODAY)
    assert out[0] == pts[0] and out[-1] == pts[-1] and len(out) <= inv.HISTORY_MAX
    days = [d for d, _ in out]
    recent = [d for d in days if (TODAY - d).days <= inv.DAILY_DAYS]
    assert len(recent) == inv.DAILY_DAYS + 1                                  # every day of the last weeks
    weekly = [d for d in days if inv.DAILY_DAYS < (TODAY - d).days <= inv.WEEKLY_DAYS]
    assert 95 <= len(weekly) <= 100 and len({d.isocalendar()[:2] for d in weekly}) == len(weekly)
    monthly = [d for d in days[1:] if (TODAY - d).days > inv.WEEKLY_DAYS]
    assert len({(d.year, d.month) for d in monthly}) == len(monthly) and 24 <= len(monthly) <= 27
    assert inv.thin(out, TODAY) == out                                        # thinning again changes nothing
    assert inv.thin([], TODAY) == ()
    many = [(TODAY - timedelta(days=31 * i), i) for i in range(600)]          # 50 years of months: the newest stay
    capped = inv.thin(many, TODAY)
    assert len(capped) == inv.HISTORY_MAX and capped[0] == many[-1] and capped[-1] == many[0]


def test_total_series_counts_an_account_from_its_first_balance():
    a = acct('a', (D(2028, 3, 1), 100000), (D(2029, 3, 1), 130000))
    b = acct('b', (D(2028, 9, 1), 50000), (D(2029, 2, 1), 55000))
    assert inv.total_series([a, b]) == ((D(2028, 3, 1), 100000), (D(2028, 9, 1), 150000), (D(2029, 2, 1), 155000),
                                        (D(2029, 3, 1), 185000))
    assert inv.value_on(a.history, D(2028, 2, 1)) is None and inv.value_on(a.history, D(2028, 6, 1)) == 100000


def test_change_over_a_period():
    pts = ((D(2028, 3, 1), 100000), (D(2029, 1, 15), 110000), (D(2029, 3, 1), 121000))
    c = inv.change(pts, TODAY, 30)
    assert (c.since, c.was, c.amount, c.pct_bp) == (D(2029, 1, 15), 110000, 11000, 1000)
    assert inv.change(pts, TODAY, 365).since == D(2028, 3, 1) and inv.change(pts, TODAY, 365).pct_bp == 2100
    assert inv.change(pts, TODAY, None).amount == 21000
    assert inv.change(pts[1:], TODAY, 365) is None            # nothing a year back
    assert inv.change(pts[:1], TODAY, None) is None           # one balance: nothing to compare
    assert inv.change(((D(2028, 1, 1), 0), (D(2029, 3, 1), 500)), TODAY, None).pct_bp is None
    down = inv.change(((D(2029, 1, 1), 200000), (D(2029, 3, 1), 150000)), TODAY, None)
    assert (down.amount, down.pct_bp) == (-50000, -2500)


def test_summary_and_investments_are_not_debts():
    a = acct('a', (D(2028, 3, 1), 100000), (D(2029, 3, 1), 130000))
    b = acct('b', (D(2029, 2, 1), 55000))
    card = Account(id='c', name='Card', kind=AccountKind.CREDIT_CARD, balance=40000, apr_bp=2000, payment=5000)
    s = inv.summary([card, a, b], TODAY)
    assert [x.id for x in s.accounts] == ['a', 'b'] and s.total == 185000 and s.as_of == D(2029, 2, 1)
    assert dict(s.changes)['all'].amount == 85000 and dict(s.by_account[1])['all'] is None
    assert total_debt([card, a, b]) == 40000
    assert [d.name for d in plan([card, a, b], method='avalanche', today=TODAY).result.debts] == ['Card']
    empty = inv.summary([card], TODAY)
    assert (empty.total, empty.as_of, empty.series) == (0, None, ())
