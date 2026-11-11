from factories import D, budget, row
from hbudget.core.dates import add_months
from hbudget.core.goals import monthly_bills, progress, total_debt
from hbudget.core.models import Account, AccountKind, Goal, Settings


def debt(id, balance, **kw):
    return Account(id=id, name=id, kind=kw.pop('kind', AccountKind.CREDIT_CARD), balance=balance, apr_bp=2000,
                   payment=kw.pop('payment', 20000), **kw)


def test_monthly_bills_and_total_debt():
    rents = [row('r%d' % i, 'Rent', 100000, add_months(D(2029, 3, 1), i)) for i in range(10)]
    rows = (*rents, row('p', 'Pay', -300000, D(2029, 3, 15)))
    assert monthly_bills(budget(rows=rows), today=D(2029, 3, 1)) == 83334      # 10 x 1000.00 / 12, rounded up
    accts = (debt('a', 50000), debt('m', 900000, is_mortgage=True), debt('c', 1, kind=AccountKind.CHECKING),
             debt('x', 7000, include_in_payoff=False))
    assert total_debt(accts) == 50000


def test_progress_for_each_kind():
    rows = tuple(row('r%d' % i, 'Rent', 120000, add_months(D(2029, 3, 1), i)) for i in range(12))
    goals = (Goal('e', 'Rainy day', 'emergency', months=3, saved=180000),
             Goal('s', 'Trip', 'savings', target=200000, saved=50000, by=D(2029, 9, 1)),
             Goal('d', 'Debt-free', 'debt_free', start_debt=100000),
             Goal('done', 'Done', 'savings', target=1000, saved=1500))
    b = budget(rows=rows, accounts=(debt('a', 40000),), settings=Settings(goals=goals))
    p = {x.goal.id: x for x in progress(b, today=D(2029, 3, 1))}
    assert (p['e'].target, p['e'].current, p['e'].pct, p['e'].reached) == (360000, 180000, 50, False)
    assert (p['s'].pct, p['s'].monthly_needed) == (25, 25000)        # 1500.00 over 6 months
    assert (p['d'].target, p['d'].current, p['d'].pct) == (100000, 60000, 60) and p['d'].done_on is not None
    assert p['d'].months_left == 3 and p['done'].reached and p['done'].pct == 100
    # more debt now than when the goal was set: progress starts from today's debt, never negative
    worse = budget(accounts=(debt('a', 150000),), settings=Settings(goals=(goals[2],)))
    [w] = progress(worse, today=D(2029, 3, 1))
    assert (w.target, w.current, w.pct) == (150000, 0, 0)
    [free] = progress(budget(settings=Settings(goals=(goals[2],))), today=D(2029, 3, 1))
    assert free.reached and free.pct == 100
