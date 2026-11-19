"""Debts (D-105): the reminder, all balances on one screen, "later", and the bill that pays a debt."""
from __future__ import annotations

from test_api import settings_body
from test_api_v1 import anon, get, make_token  # noqa: F401 - anon is a fixture


def debt(admin, name, balance, as_of, **kw):
    r = admin.api('POST', '/accounts', {'name': name, 'kind': 'credit_card', 'balance': balance, 'apr_bp': 1999,
                                        'payment': 5000, 'as_of': as_of, **kw})
    assert r.status_code == 200, r.text
    return r.json()['id']


def codes(admin):
    return [a['code'] for a in admin.api('GET', '/summary').json()['alerts']]


def test_the_reminder_the_quick_form_and_later(admin):
    old = debt(admin, 'Card A', 90000, '2028-12-01')
    new = debt(admin, 'Card B', 40000, '2029-02-20')
    d = admin.api('GET', '/accounts').json()
    assert d['reminder'] == {'days': 61, 'stale': [old], 'snoozed_until': None}
    assert {a['id']: a['source'] for a in d['accounts']} == {old: 'typed', new: 'typed'}
    [alert] = [a for a in admin.api('GET', '/summary').json()['alerts'] if a['code'] == 'debt_balances']
    assert alert['message'].startswith('1 debt balance was not updated for more than 2 months: Card A.')
    # put off for two weeks, and brought back
    assert admin.api('PUT', '/accounts/reminder/later', {}).json() == {'snoozed_until': '2029-03-15'}
    assert 'debt_balances' not in codes(admin)
    assert admin.api('GET', '/accounts').json()['reminder']['snoozed_until'] == '2029-03-15'
    assert admin.api('PUT', '/accounts/reminder/later', {'days': 0}).json() == {'snoozed_until': None}
    assert 'debt_balances' in codes(admin)
    # several balances in one change; a date after today, an unknown debt or a savings account refuse it all
    bad = admin.api('POST', '/accounts/balances', {'items': [{'id': old, 'balance': 1}, {'id': 'nope', 'balance': 1}]})
    assert bad.status_code == 404
    assert admin.api('POST', '/accounts/balances', {'items': [{'id': old, 'balance': 1, 'as_of': '2029-03-02'}]}
                     ).status_code == 400
    out = admin.api('POST', '/accounts/balances', {'items': [{'id': old, 'balance': 85000},
                                                             {'id': new, 'balance': 39000, 'as_of': '2029-02-25'}]})
    assert out.json() == {'ok': True, 'updated': 2}
    d = admin.api('GET', '/accounts').json()
    got = {a['id']: a for a in d['accounts']}
    assert (got[old]['balance'], got[old]['as_of']) == (85000, '2029-03-01')
    assert got[old]['history'] == [['2028-12-01', 90000], ['2029-03-01', 85000]]
    assert (got[new]['balance'], got[new]['as_of']) == (39000, '2029-02-25')
    assert d['reminder']['stale'] == [] and 'debt_balances' not in codes(admin)
    # the interval is a household setting; 0 turns the reminder off
    body = settings_body(admin)
    assert body['alerts']['debt_update_days'] == 61
    body['alerts'] = {**body['alerts'], 'debt_update_days': 3}
    assert admin.api('PUT', '/settings', body).status_code == 200
    assert admin.api('GET', '/accounts').json()['reminder']['stale'] == [new]
    body = settings_body(admin)
    body['alerts'] = {**body['alerts'], 'debt_update_days': 0}
    assert admin.api('PUT', '/settings', body).status_code == 200
    assert admin.api('GET', '/accounts').json()['reminder'] == {'days': 0, 'stale': [], 'snoozed_until': None}


def test_a_promo_ending_and_the_bill_that_pays_a_debt(admin, anon):  # noqa: F811
    did = debt(admin, 'Store card', 120000, '2029-03-01', promo_ends='2029-04-15', deferred_interest=True)
    [a] = [a for a in admin.api('GET', '/summary').json()['alerts'] if a['code'] == 'promo_ending']
    assert a['severity'] == 'warn' and a['data'] == {'account': did} and 'in 45 days' in a['message']
    sid = admin.api('POST', '/series', {'name': 'Store card payment', 'kind': 'bill', 'amount': 5000,
                                        'schedule': {'cadence': 'monthly', 'start': '2029-03-10'}}).json()['id']
    pay = admin.api('POST', '/series', {'name': 'Pay', 'kind': 'income', 'amount': 100000,
                                        'schedule': {'cadence': 'monthly', 'start': '2029-03-05'}}).json()['id']
    assert admin.api('PUT', '/accounts/%s/paid-by' % did, {'series_id': pay}).status_code == 400
    assert admin.api('PUT', '/accounts/nope/paid-by', {'series_id': sid}).status_code == 404
    assert admin.api('PUT', '/accounts/%s/paid-by' % did, {'series_id': sid}).status_code == 200
    assert next(a for a in admin.api('GET', '/accounts').json()['accounts'] if a['id'] == did)['paid_by'] == [
        'Store card payment']
    assert next(s for s in admin.api('GET', '/series').json()['series'] if s['id'] == sid)['debt'] == did
    # the agent sees the reminder among the alerts
    t = make_token(admin)
    assert 'promo_ending' in [x['code'] for x in get(anon, '/summary', t).json()['alerts']]
    assert admin.api('PUT', '/accounts/%s/paid-by' % did, {'series_id': None}).status_code == 200
    assert next(s for s in admin.api('GET', '/series').json()['series'] if s['id'] == sid)['debt'] is None


def test_editing_a_debts_details_keeps_the_date_of_its_balance(admin):
    did = debt(admin, 'Card A', 90000, '2028-12-01')
    body = {'name': 'Card A (blue)', 'kind': 'credit_card', 'balance': 90000, 'apr_bp': 2099, 'payment': 6000}

    def got():
        a = next(a for a in admin.api('GET', '/accounts').json()['accounts'] if a['id'] == did)
        return a['name'], a['balance'], a['as_of']
    assert admin.api('PUT', '/accounts/%s' % did, body).status_code == 200
    assert got() == ('Card A (blue)', 90000, '2028-12-01')                 # still due for a look at the statement
    assert admin.api('PUT', '/accounts/%s' % did, {**body, 'balance': 88000}).status_code == 200
    assert got() == ('Card A (blue)', 88000, '2029-03-01')                 # a new balance is dated today
    assert admin.api('PUT', '/accounts/%s' % did, {**body, 'balance': 88000, 'as_of': '2029-02-10'}).status_code == 200
    assert got() == ('Card A (blue)', 88000, '2029-02-10')
