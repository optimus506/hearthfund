"""#97: a recurring bill names the debt it pays down; Debts and get_debts show an estimated balance from the
reconciled payments since the typed balance."""
from __future__ import annotations

from datetime import date
from typing import Any

from test_api import bill
from test_api_v1 import anon, make_token, tool  # noqa: F401 - anon is a fixture
from test_reconcile_flow import upload

from webhelpers import Client


def debts(c: Client) -> dict[str, Any]:
    return {a['name']: a for a in c.api('GET', '/accounts').json()['accounts']}


def test_linked_bill_gives_an_estimate(admin: Client, anon, app) -> None:  # noqa: F811 - the anon fixture
    r = admin.api('POST', '/accounts', {'name': 'Lender', 'kind': 'loan', 'balance': 1_000_000, 'apr_bp': 1200,
                                        'as_of': '2029-02-20'})
    assert r.status_code == 200, r.text
    loan = r.json()['id']
    assert admin.api('POST', '/accounts', {'name': 'Savings', 'kind': 'savings'}).status_code == 200
    savings = debts(admin)['Savings']['id']
    sid = bill(admin, 'Lender', 30000, 5)
    assert admin.api('POST', '/rules', {'name': 'Lender', 'kind': 'bill', 'contains': ['LENDER'],
                                        'targets': [sid]}).status_code == 200
    s = next(x for x in admin.api('GET', '/series').json()['series'] if x['id'] == sid)
    assert s['debt'] is None
    body = {k: s[k] for k in ('name', 'kind', 'amount', 'schedule', 'status')}
    assert admin.api('PUT', '/series/' + sid, {**body, 'debt': savings}).status_code == 400   # not a debt
    assert admin.api('PUT', '/series/' + sid, {**body, 'debt': loan}).status_code == 200
    assert admin.api('PUT', '/series/' + sid, body).status_code == 200          # left out: the link stays
    assert next(x for x in admin.api('GET', '/series').json()['series'] if x['id'] == sid)['debt'] == loan
    assert debts(admin)['Lender']['estimate'] is None                              # nothing paid yet
    # reconcile the March payment
    app.state.clock = lambda: date(2029, 3, 6)
    csv = b'Date,Description,Amount,Balance\n2029-03-05,LENDER PAYMENT,-300.00,700.00\n2029-02-27,TEA,-5.00,1000.00\n'
    up = upload(admin, 'bank.csv', csv).json()
    assert admin.api('POST', '/reconcile/apply', {'upload_id': up['id'], 'accept': {}}).status_code == 200
    est = debts(admin)['Lender']['estimate']
    # 2/20 typed $10,000.00; no monthly anniversary before 3/5; one $300.00 payment
    assert est == {'balance': 970_000, 'since': '2029-02-20', 'paid': 30_000, 'payments': 1, 'interest': 0}
    assert debts(admin)['Lender']['balance'] == 1_000_000                          # the typed balance is untouched
    ro = make_token(admin, 'read')
    got = {d['name']: d for d in tool(anon, ro, 'get_debts')['debts']}
    assert got['Lender']['estimated_balance'] == 970_000
    backup = admin.get('/api/h/%d/export/backup.json' % admin.hid).json()
    assert next(x for x in backup['bills'] if x['id'] == sid)['debt'] == loan
    # over MCP, set_series_details can link or unlink
    t = make_token(admin, 'write')
    assert tool(anon, t, 'set_series_details', series_id=sid, debt=None)
    assert debts(admin)['Lender']['estimate'] is None
