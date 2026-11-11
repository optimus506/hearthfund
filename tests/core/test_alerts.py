from dataclasses import replace

from recon_fixture import export, household

from factories import D, budget, chain, row, rule, schedule, series, txn
from hbudget.core.alerts import cap_totals, evaluate, payee_key, period_start
from hbudget.core.forecast import compute
from hbudget.core.models import (
    AlertSettings,
    CapRule,
    Direction,
    ExpectedItem,
    IncomeAlert,
    RowState,
    Settings,
    Severity,
    Status,
)
from hbudget.core.reconcile import apply, match, prepare

TODAY = D(2029, 3, 17)


def codes(alerts):
    return [a.code for a in alerts]


def test_below_zero_and_cushion():
    b = budget(start=10000, rows=(row('r', 'Rent', 20000, D(2029, 3, 3)),))
    a = evaluate(b, compute(b, today=TODAY), today=TODAY)
    assert a[0].code == 'below_zero' and a[0].severity is Severity.BAD and '-$100.00 on 03/03/2029' in a[0].message
    paid = row('r', 'Rent', 20000, D(2029, 3, 3), status_override=Status.AUTOPAY)
    b = budget(start=40000, rows=(paid,))
    assert codes(evaluate(b, compute(b, today=TODAY), today=TODAY)) == ['cushion']
    b = budget(start=90000, rows=(paid,))
    assert evaluate(b, compute(b, today=TODAY), today=TODAY) == []


def recent_alerts(**settings):
    b = household(settings=Settings(alerts=AlertSettings(**settings)))
    a = apply(b, match(b, prepare(export(), b)), {'P1': 'clear', 'P2': 'clear', 'P4': 'clear'}, today=TODAY)
    return evaluate(a.budget, a.forecast_after, today=TODAY, recent=a)


def test_bill_variance_unmatched_and_income_check_short():
    al = recent_alerts(income=(IncomeAlert('pay-a', short_by=5000),), cushion=0)
    msgs = {a.code: a.message for a in al}
    # P1 cleared Electric (150.00) with a 190.00 posting: +27%; the card bundle (P4) is not checked
    variance = [a.message for a in al if a.code == 'bill_variance']
    assert variance == ['Electric posted at $190.00, budget $150.00 (+27%).']
    assert 'Pay A deposit of $2,300.00 is $100.00 short' in msgs['income_check_short']
    assert len([a for a in al if a.code == 'unmatched']) == 3


def test_unscheduled_and_expected_late():
    b = budget(rows=(row('a', 'Electric', 15000, D(2029, 3, 20)), row('b', 'Gym', 4500, D(2029, 3, 30)),
                     row('c', 'Internet', 8000, D(2029, 3, 21), status_override=Status.AUTOPAY),
                     row('d', 'Grocery', 16000, D(2029, 3, 20), kind='spending'),
                     row('z', 'Zero', 0, D(2029, 3, 20)),
                     row('k', 'Paid by card', 3000, D(2029, 3, 20), card='rewards')),
               expected=(ExpectedItem('e', 'Refund', 1000, 'in', D(2029, 3, 13)),
                         ExpectedItem('f', 'Recent', 1000, 'in', D(2029, 3, 15))))
    al = evaluate(b, compute(b, today=TODAY), today=TODAY)
    assert [(a.code, a.row_id) for a in al if a.code == 'unscheduled'] == [('unscheduled', 'a')]
    assert [a.data for a in al if a.code == 'expected_late'] == [(('expected', 'e'),)]


def test_unmatched_recurring_needs_two_months():
    hist = [txn('a', D(2029, 1, 5), -900, 'STREAMING #1234 SVC'), txn('b', D(2029, 2, 5), -900, 'STREAMING #5678 SVC'),
            txn('c', D(2029, 2, 6), -100, 'ONE OFF SHOP'), txn('d', D(2029, 2, 7), -100, 'ONE OFF SHOP')]
    b = budget(start=90000)
    al = evaluate(b, compute(b, today=TODAY), today=TODAY, history=hist)
    assert [a.data for a in al if a.code == 'unmatched_recurring'] == [(('payee', 'STREAMING SVC'),)]
    off = replace(b, settings=Settings(alerts=AlertSettings(unmatched_recurring=False)))
    assert evaluate(off, compute(off, today=TODAY), today=TODAY, history=hist) == []
    assert payee_key('ACH 123 Acme  Water  Co Ltd') == 'ACH ACME WATER'


def test_repeating_payees_age_out_and_can_be_set_aside():
    """#104: only the last 12 months count, a payee quiet for 60 days drops out, and "ignore until it posts again"
    hides it until a posting in a later month."""
    def rep(b, hist, today=TODAY):
        return [a.data for a in evaluate(b, compute(b, today=today), today=today, history=hist)
                if a.code == 'unmatched_recurring']
    b = budget(start=90000)
    old = [txn('o%d' % m, D(2028, m, 3), -2500, 'BIG BOX STORE TYPE: CARD') for m in range(1, 10)]
    assert rep(b, old) == []                                     # paid off: last posting 2028-09, long quiet
    recent = [txn('x', D(2028, 2, 3), -500, 'GYM'), txn('y', D(2029, 3, 1), -500, 'GYM')]
    assert rep(b, recent) == []                                  # 2028-02 is outside the last 12 months
    hist = [txn('a', D(2029, 2, 5), -900, 'STREAMING SVC'), txn('b', D(2029, 3, 5), -900, 'STREAMING SVC')]
    assert rep(b, hist) == [(('payee', 'STREAMING SVC'),)]
    aside = replace(b, settings=Settings(alert_snoozes=(('unmatched_recurring', 'STREAMING SVC', D(2029, 3, 10)),)))
    assert rep(aside, hist) == []
    later = [*hist, txn('c', D(2029, 4, 5), -900, 'STREAMING SVC')]
    assert rep(aside, later, D(2029, 4, 6)) == [(('payee', 'STREAMING SVC'),)]  # posted again in a later month
    other = replace(b, settings=Settings(alert_snoozes=(('unbudgeted', 'STREAMING SVC', D(2029, 3, 10)),)))
    assert rep(other, hist) == [(('payee', 'STREAMING SVC'),)]  # set aside for another alert only


def test_a_steady_payment_only_tracked_is_flagged():
    """#95: a monthly payment caught by a track rule at a similar amount is pointed out."""
    def unb(rules, hist):
        b = budget(start=90000, rules=rules)
        return [(a.data, a.message) for a in evaluate(b, compute(b, today=TODAY), today=TODAY, history=hist)
                if a.code == 'unbudgeted']
    track = (rule('pp', 'track', ('PAYPAL',)),)
    three = [txn('p1', D(2029, 1, 6), -7500, 'PAYPAL CREDIT'), txn('p2', D(2029, 2, 5), -6000, 'PAYPAL CREDIT'),
             txn('p3', D(2029, 3, 6), -6000, 'PAYPAL CREDIT')]
    got = unb(track, three)
    assert got == [((('payee', 'PAYPAL CREDIT'), ('amount', '6000')),
                    '"PAYPAL CREDIT" was paid 3 months in a row (about $60.00) but is only tracked, not budgeted.')]
    assert unb(track, three[1:]) == []                          # two months is not enough...
    bill_pay = [txn('q%d' % i, t.posted, t.amount, 'PAYPAL TYPE: BILL PAYMT CO: PAYPAL') for i, t in enumerate(three)]
    assert len(unb(track, bill_pay[1:])) == 1                    # ...unless the bank calls it a bill payment
    uneven = [*three[:2], txn('p3', D(2029, 3, 6), -30000, 'PAYPAL CREDIT')]
    assert unb(track, uneven) == []                              # spending, not a steady payment
    gap = [three[0], three[2], txn('p0', D(2028, 12, 6), -6000, 'PAYPAL CREDIT')]
    assert unb(track, gap) == []                                 # February is missing: not in a row
    assert unb((rule('pp', 'bill', ('PAYPAL',)),), three) == []  # a bill rule: budgeted


def test_income_short_uses_posted_amounts():
    pay = series('pay-b', 'income', 50000, schedule('weekly', D(2029, 1, 5)))
    rows = tuple(row('p%d' % i, 'Pay B', -50000, D(2029, 3, 1 + i), series_id='pay-b', state=RowState.CLEARED,
                     cleared_txn_id='t%d' % i) for i in range(5))
    hist = [txn('t%d' % i, D(2029, 3, 1 + i), 40000, 'PAY B') for i in range(5)]
    b = budget(start=900000, series=(pay,), rows=rows,
               settings=Settings(alerts=AlertSettings(income=(IncomeAlert('pay-b'),), unmatched_recurring=False)))
    al = evaluate(b, compute(b, today=TODAY), today=TODAY, history=hist)
    assert codes(al) == ['income_short'] and 'averaged $400.00 over the last 4' in al[0].message
    ok_hist = [replace(t, amount=45000) for t in hist]                     # 90%: fine
    assert evaluate(b, compute(b, today=TODAY), today=TODAY, history=ok_hist) == []
    assert evaluate(b, compute(b, today=TODAY), today=TODAY) == []          # no history: skipped


def test_caps():
    shop = rule('shop', 'track', ('MARKETPLACE',), direction=Direction.OUT)
    cap = CapRule('weekly', 'Online shopping', ('shop',), 'week', 5000)
    b = budget(start=900000, rules=(shop,),
               settings=Settings(alerts=AlertSettings(caps=(cap,), unmatched_recurring=False)))
    hist = [txn('a', D(2029, 3, 6), -3000, 'MARKETPLACE'), txn('b', D(2029, 3, 13), -4000, 'MARKETPLACE'),
            txn('c', D(2029, 3, 14), -2000, 'MARKETPLACE'), txn('d', D(2029, 3, 14), 9000, 'MARKETPLACE')]
    (got,) = cap_totals(b, hist, today=TODAY)
    assert got == (cap, 6000, 4500)          # this week 60.00; 90.00 over two weeks
    al = evaluate(b, compute(b, today=TODAY), today=TODAY, history=hist)
    assert codes(al) == ['cap'] and 'Online shopping: $60.00 this week' in al[0].message
    monthly = CapRule('m', 'Monthly', ('shop',), 'month', 100000)
    assert cap_totals(replace(b, settings=Settings(alerts=AlertSettings(caps=(monthly,)))), hist,
                      today=D(2029, 4, 2)) == [(monthly, 0, 4500)]
    assert cap_totals(b, [], today=TODAY) == [(cap, 0, 0)]


def test_period_start():
    assert period_start(D(2029, 3, 17), 'week', 0) == D(2029, 3, 12)
    assert period_start(D(2029, 3, 17), 'month', 0) == D(2029, 3, 1)


def test_without_recent_result():
    b = household()
    from hbudget.core.reconcile import Applied
    f = compute(b, today=TODAY)
    fake = Applied(b, (), (), f, f, '')
    assert 'unmatched' not in codes(evaluate(b, f, today=TODAY, recent=fake))
    assert chain(0, []) == []


def test_cap_average_counts_quiet_periods_from_the_start_of_the_history():
    shop = rule('shop', 'track', ('MARKETPLACE',), direction=Direction.OUT)
    cap = CapRule('weekly', 'Online shopping', ('shop',), 'week', 5000)
    b = budget(rules=(shop,), settings=Settings(alerts=AlertSettings(caps=(cap,))))
    # history starts two weeks before the first matching posting: four weeks in all, 60.00 spent
    hist = [txn('x', D(2029, 2, 26), -100, 'COFFEE'), txn('a', D(2029, 3, 13), -6000, 'MARKETPLACE')]
    assert cap_totals(b, hist, today=TODAY) == [(cap, 6000, 2000)]


def test_snoozed_until_the_payee_posts_in_a_later_month() -> None:
    from hbudget.core.alerts import snoozed
    b = budget()
    b = replace(b, settings=replace(b.settings, alert_snoozes=(('not_budgeted', 'SHOP', D(2029, 3, 10)),)))
    march, april = 2029 * 12 + 2, 2029 * 12 + 3
    assert snoozed(b, 'not_budgeted', 'SHOP', [march - 1, march])          # nothing after the month set aside
    assert not snoozed(b, 'not_budgeted', 'SHOP', [march, april])          # it posted again in April
    assert not snoozed(b, 'not_budgeted', 'OTHER', [march])                # another payee
    assert not snoozed(b, 'repeating', 'SHOP', [march])                    # another kind of alert
