"""Plan > Calendar: the forecast by day for one month, with the balance at the end of each day."""
from __future__ import annotations

from test_api import bill, pay


def test_calendar_month(admin):
    pay(admin, 300000, 15)
    bill(admin, 'Rent', 150000, 1)
    bill(admin, 'Phone', 6000, 20)
    admin.api('POST', '/expected', {'label': 'Refund', 'amount': 2500, 'direction': 'in', 'date': '2029-03-10'})
    c = admin.api('GET', '/calendar').json()['calendar']          # today is 2029-03-01, the balance 1000.00
    assert c['month'] == '2029-03' and c['prev'] is None and c['next'] == '2029-04' and len(c['days']) == 31
    days = {d['date']: d for d in c['days']}
    assert [e['label'] for e in days['2029-03-01']['entries']] == ['Rent']
    assert days['2029-03-01']['balance'] == 100000 - 150000 and days['2029-03-01']['below_zero']
    assert days['2029-03-02']['balance'] == days['2029-03-01']['balance'] and not days['2029-03-02']['entries']
    refund = days['2029-03-10']['entries'][0]
    assert refund['label'] == 'Refund' and refund['kind'] == 'expected' and refund['amount'] == -2500
    assert [e['kind'] for e in days['2029-03-15']['entries']] == ['income']
    assert c['opening'] == 100000 and c['closing'] == days['2029-03-31']['balance']
    assert c['money_in'] == 302500 and c['money_out'] == 156000
    assert c['low'] == {'date': '2029-03-01', 'balance': -50000}
    april = admin.api('GET', '/calendar?month=2029-04').json()['calendar']
    assert april['opening'] == c['closing'] and april['prev'] == '2029-03'
    assert admin.api('GET', '/calendar?month=2029-13').status_code == 400
    assert admin.api('GET', '/calendar?month=March').status_code == 400
    page = admin.get('/h/%d/calendar' % admin.hid)
    assert page.status_code == 200 and 'calendar.js' in page.text
