"""Dashboard polish: the summary strip, payoff dates, report totals and what the pages draw them with."""
from __future__ import annotations

from test_api import bill, pay
from test_static import js


def test_summary_not_scheduled_and_expected(admin):
    s = admin.api('GET', '/summary').json()
    assert s['unscheduled'] == {'count': 0, 'total': 0, 'days': 7} and s['expected_open'] == 0
    bill(admin, 'Electric', 15000, 6)                     # due 3/6: within 7 days of 3/1, not scheduled
    bill(admin, 'Water', 4000, 7, status='autopay')       # within 7 days but on autopay
    bill(admin, 'Phone', 6000, 20)                        # not due within 7 days
    eid = admin.api('POST', '/expected', {'label': 'Refund', 'amount': 2000, 'direction': 'in',
                                          'date': '2029-03-08'}).json()['id']
    s = admin.api('GET', '/summary').json()
    assert s['unscheduled'] == {'count': 1, 'total': 15000, 'days': 7}
    assert s['expected_open'] == 1
    # the alert list is still there for the page's alert boxes
    assert [a['code'] for a in s['alerts'] if a['code'] == 'unscheduled'] == ['unscheduled']
    rid = next(r['id'] for r in admin.api('GET', '/ledger').json()['rows'] if r['name'] == 'Electric')
    assert admin.api('POST', '/rows/%s/status' % rid, {'status': 'manual'}).status_code == 200
    admin.api('POST', '/expected/%s/status' % eid, {'status': 'done'})
    s = admin.api('GET', '/summary').json()
    assert s['unscheduled']['count'] == 0 and s['expected_open'] == 0


def test_payoff_has_the_other_methods_debt_free_date(admin):
    admin.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 300000, 'apr_bp': 2400,
                                    'payment': 20000})
    admin.api('POST', '/accounts', {'name': 'Loan B', 'kind': 'loan', 'balance': 50000, 'apr_bp': 500,
                                    'payment': 10000})
    p = admin.api('POST', '/payoff', {'method': 'avalanche'}).json()
    o = admin.api('POST', '/payoff', {'method': 'snowball'}).json()
    assert p['debt_free_on'] and p['other_debt_free_on'] == o['debt_free_on']
    assert o['other_debt_free_on'] == p['debt_free_on']
    # the promo verdict's payoff month comes from result.debts
    assert {d['name'] for d in p['result']['debts'] if d['paid_on']} == {'Card A', 'Loan B'}


def test_report_pay_shares_and_biggest_group(admin):
    pay(admin, 300000, 15)
    bill(admin, 'Rent', 150000, 1)
    bill(admin, 'Electric', 15000, 6)
    f = admin.api('GET', '/flow?view=planned').json()['flow']
    t = f['totals']
    assert t['pay'] == t['money_in'] > 0
    assert t['per_month']['pay'] == t['pay'] // f['months']
    assert t['pay_covers_pct'] == (200 * t['pay'] + t['money_out']) // (2 * t['money_out'])
    assert t['biggest']['name'] == f['groups'][0]['name'] and t['biggest']['total'] == f['groups'][0]['total']
    assert sum(g['total'] for g in f['groups']) == t['money_out']
    assert all(0 <= g['pct'] <= 100 for g in f['groups']) and all(s['pct'] is not None for s in f['sources'])
    assert t['left_over_pct'] == (200 * t['left_over'] + t['money_in']) // (2 * t['money_in'])
    empty = admin.api('GET', '/flow?view=actual').json()['flow']['totals']
    assert empty['pay'] == 0 and empty['pay_covers_pct'] is None and empty['biggest'] is None


def test_pages_draw_the_new_figures(admin):
    assert "stat('Not scheduled'" in js(admin, 'summary') and "' expected'" in js(admin, 'summary')
    c = js(admin, 'chart')
    assert "'fill-opacity'" in c and "' after '" in c and 'dots' in c
    plan = admin.get('/h/%d/plan' % admin.hid).text
    assert 'id="wiLegend"' in plan and 'id="wiTableBox"' in plan
    assert "classList.add('tall')" in js(admin, 'bills')
    d = js(admin, 'debts')
    assert 'Total without mortgage' in d and 'localStorage' in d and "'-12'" in d and ', 300)' in d
    assert 'other_debt_free_on' in d
    assert 'more than 10% over budget in' in js(admin, 'card')
    r = js(admin, 'report')
    assert 'Pay covers' in r and 'Biggest group' in r and "'Left over'" in r
    assert 'More than budget' in js(admin, 'checkup')


def test_checkup_caps_use_the_cap_rule_by_id():
    # Caps made in the UI have an id that differs from their name; the view used to look them up by name and
    # showed "cap $0.00 per week".
    from factories import D, budget, rule, txn
    from hbudget.core.checkup import compute
    from hbudget.core.models import AlertSettings, CapRule, Settings
    from hbudget.web import views
    cap = CapRule('c7f3', 'Eating out', ('shop',), 'month', 25000)
    b = budget(rules=(rule('shop', 'track', ('SHOP',)),), settings=Settings(alerts=AlertSettings(caps=(cap,))))
    out = views.checkup(compute([txn('s1', D(2029, 3, 5), -1000, 'SHOP')], b), b)['cap_weeks']
    assert [(c['id'], c['name'], c['period'], c['amount']) for c in out] == [('c7f3', 'Eating out', 'month', 25000)]
