"""Money > Recap: GET /recap for a month and a year, the default period and the page."""
from __future__ import annotations

from test_api_v1 import anon, get, make_token, tool  # noqa: F401 - anon is a fixture
from test_reconcile_flow import upload

BANK = (b'Date,Description,Amount\n'
        b'01/02/2029,EMPLOYER PAY,3000.00\n01/05/2029,LANDLORD,-1500.00\n'
        b'02/01/2029,EMPLOYER PAY,3100.00\n02/05/2029,LANDLORD,-1500.00\n02/09/2029,CORNER MARKET,-40.00\n')


def test_recap(admin, anon):  # noqa: F811 - the anon fixture
    r = admin.api('GET', '/recap').json()['recap']                    # no history: this month, empty
    assert r['period'] == '2029-03' and not r['has_history'] and r['periods'] == []
    assert upload(admin, 'bank.csv', BANK).status_code == 200
    r = admin.api('GET', '/recap').json()['recap']                    # today 2029-03-01: the last full month
    assert r['period'] == '2029-02' and r['complete'] and r['prev'] == '2029-01' and r['next'] is None
    assert r['totals']['money_in'] == 310000 and r['totals']['money_out'] == 154000 and r['totals']['net'] == 156000
    assert r['before']['money_in'] == 300000 and r['periods'] == ['2029-01', '2029-02']
    assert [p['name'] for p in r['top_payees']] == ['LANDLORD', 'CORNER MARKET']
    jan = admin.api('GET', '/recap?period=2029-01').json()['recap']
    assert jan['next'] == '2029-02' and jan['totals']['money_out'] == 150000
    y = admin.api('GET', '/recap?span=year').json()['recap']          # no full year yet: this year so far
    assert y['span'] == 'year' and y['period'] == '2029' and not y['complete'] and y['totals']['money_in'] == 610000
    assert admin.api('GET', '/recap?period=2029-13').status_code == 400
    assert admin.api('GET', '/recap?span=week').status_code == 400
    page = admin.get('/h/%d/recap' % admin.hid)
    assert page.status_code == 200 and 'recap.js' in page.text
    ro = make_token(admin, 'read')
    assert tool(anon, ro, 'get_recap', period='2029-01')['recap'] == jan
    assert get(anon, '/recap?span=year', ro).json()['recap'] == y
    assert get(anon, '/recap?period=bad', ro).status_code == 400
