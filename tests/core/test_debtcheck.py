"""The reminder to update debt balances and the promo nudge (D-105)."""
from dataclasses import replace

from factories import D, budget
from hbudget.core import debtcheck
from hbudget.core.alerts import evaluate
from hbudget.core.forecast import compute
from hbudget.core.models import Account, AccountKind, AlertSettings, Settings, Severity

TODAY = D(2029, 3, 1)


def acct(id, balance=50000, as_of=None, kind=AccountKind.CREDIT_CARD, **kw):
    return Account(id, id.capitalize(), kind, balance, as_of=as_of, **kw)


ACCOUNTS = (acct('old', as_of=D(2028, 12, 1)), acct('fresh', as_of=D(2029, 2, 1)), acct('undated'),
            acct('paid', 0, D(2028, 1, 1)), acct('savings', as_of=D(2028, 1, 1), kind=AccountKind.SAVINGS),
            acct('fund', as_of=D(2028, 1, 1), kind=AccountKind.INVESTMENT))


def test_stale_lists_owed_debts_older_than_the_interval_oldest_first():
    assert [a.id for a in debtcheck.stale(ACCOUNTS, TODAY, 61)] == ['undated', 'old']
    assert [a.id for a in debtcheck.stale(ACCOUNTS, TODAY, 20)] == ['undated', 'old', 'fresh']
    assert debtcheck.stale(ACCOUNTS, TODAY, 0) == ()
    assert [a.id for a in debtcheck.stale(ACCOUNTS, D(2029, 1, 31), 61)] == ['undated']      # day 61: not yet


def test_the_alert_and_putting_it_off():
    b = budget(accounts=ACCOUNTS)
    assert b.settings.alerts.debt_update_days == 61
    [a] = [x for x in evaluate(b, compute(b, today=TODAY), today=TODAY) if x.code == 'debt_balances']
    assert a.severity is Severity.WARN and dict(a.data) == {'count': '2'}
    assert a.message.startswith('2 debt balances were not updated for more than 2 months: Undated, Old.')
    later = replace(b, settings=replace(b.settings, alert_snoozes=((*debtcheck.SNOOZE, D(2029, 3, 15)),)))
    assert debtcheck.due(later, D(2029, 3, 14)) == () and debtcheck.snoozed_until(later) == D(2029, 3, 15)
    assert [x.id for x in debtcheck.due(later, D(2029, 3, 15))] == ['undated', 'old']
    off = replace(b, settings=Settings(alerts=AlertSettings(debt_update_days=0)))
    assert not [x for x in evaluate(off, compute(off, today=TODAY), today=TODAY) if x.code == 'debt_balances']


def test_a_promo_that_ends_soon_is_pointed_out():
    accounts = (acct('store', 120000, TODAY, promo_ends=D(2029, 4, 15), deferred_interest=True),
                acct('plain', 30000, TODAY, promo_ends=D(2029, 3, 2)),
                acct('far', 30000, TODAY, promo_ends=D(2029, 6, 1)),
                acct('over', 30000, TODAY, promo_ends=D(2029, 2, 1)),
                acct('done', 0, TODAY, promo_ends=D(2029, 3, 20)))
    assert [a.id for a in debtcheck.promos_ending(accounts, TODAY)] == ['plain', 'store']
    b = budget(accounts=accounts)
    found = [x for x in evaluate(b, compute(b, today=TODAY), today=TODAY) if x.code == 'promo_ending']
    assert [(x.severity, dict(x.data)['account']) for x in found] == [(Severity.INFO, 'plain'),
                                                                     (Severity.WARN, 'store')]
    assert 'in 1 day' in found[0].message and 'in 45 days' in found[1].message
    assert found[1].message.endswith('Interest for the whole promo is charged if it is not paid off by then.')
