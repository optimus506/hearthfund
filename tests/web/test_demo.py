"""The demo household: built relative to today, every tab has data, and its waiting export reconciles."""
from __future__ import annotations

from datetime import date

import pytest

from hbudget import demo
from hbudget.web.app import create_app
from webhelpers import PASSWORD, Client, csrf_from, login, make_settings, page_csrf


def demo_client(app, tmp_path) -> Client:  # type: ignore[no-untyped-def]
    c = Client(app)
    r = c.get('/setup')
    token = (tmp_path / 'setup-token').read_text().strip()
    r = c.post('/setup', data={'csrf_token': csrf_from(r.text), 'token': token, 'username': 'admin',
                               'password': PASSWORD, 'household': 'Demo', 'start': 'demo'}, follow_redirects=False)
    assert r.status_code == 303
    c.hid = int(r.headers['location'].split('/')[2])
    c.csrf = page_csrf(c, '/h/%d/bills' % c.hid)
    return c


@pytest.mark.parametrize('today', [date(2028, 1, 5), date(2029, 7, 20), date(2030, 12, 31)])
def test_every_tab_has_data_and_the_export_reconciles(app, tmp_path, today):
    app.state.clock = lambda: today
    c = demo_client(app, tmp_path)
    s = c.api('GET', '/summary').json()
    assert s['balance'] == demo.START_BALANCE and s['alerts']
    rows = c.api('GET', '/ledger').json()['rows']
    assert {r['status'] for r in rows if r['kind'] == 'bill'} == {'autopay', 'manual', 'none'}
    assert any(r['spending'] for r in rows) and any(r['card'] for r in rows)
    series = {x['id']: x for x in c.api('GET', '/series').json()['series']}
    assert series['car-loan']['funded_by'] == 'pay-a' and series['lawn-care']['month_amounts']
    accounts = c.api('GET', '/accounts').json()['accounts']
    assert any(a['deferred_interest'] for a in accounts) and any(a['is_mortgage'] for a in accounts)
    assert c.api('POST', '/payoff', {}).json()['promos']
    flow = c.api('GET', '/flow').json()['flow']
    assert flow['sources'] and flow['groups'] and flow['totals']['money_in'] > 0
    assert c.api('GET', '/flow?view=planned').json()['flow']['groups']
    assert c.api('GET', '/checkup').json()['checkup']['bills']
    assert c.api('GET', '/card').json()['summary']['weeks']
    assert c.api('GET', '/rules').json()['rules']
    assert c.api('GET', '/expected').json()['items']
    assert 'made up' in c.get('/h/%d/notes' % c.hid).text
    ups = {u['id']: u for u in c.api('GET', '/uploads').json()['uploads']}
    assert ups['demo-waiting']['status'] == 'waiting'
    p = c.api('POST', '/reconcile/preview', {'upload_id': 'demo-waiting'}).json()
    assert [x['kind'] for x in p['proposals']] == ['tolerance', 'card_bundle']
    assert len(p['tracked']) == 1 and len(p['unknown']) == 1
    r = c.api('POST', '/reconcile/apply', {'upload_id': 'demo-waiting', 'accept': {'P1': 'clear', 'P2': 'clear'},
                                           'fingerprint': p['fingerprint']})
    assert r.status_code == 200, r.text
    assert r.json()['balance'] == p['latest_balance']
    assert c.api('GET', '/summary').json()['reconciled_through'] == p['through']


def test_demo_is_made_up_and_dated_from_today():
    b = demo.demo_budget(today=date(2029, 7, 20))
    assert b.as_of == date(2029, 7, 10) and b.start_balance == demo.START_BALANCE
    assert all(r.due_date >= b.as_of for r in b.rows)
    assert all(a.amount % 100 == 0 for a in b.series)          # whole-dollar amounts


def test_admin_can_create_a_demo_household(admin):
    r = admin.request('POST', '/api/admin/households', json={'name': 'Second', 'demo': True},
                      headers={'X-CSRF-Token': admin.csrf})
    hid = r.json()['id']
    c = Client(admin.app)
    c.cookies, c.csrf, c.hid = admin.cookies, admin.csrf, hid
    assert c.api('GET', '/uploads').json()['uploads']


def test_hbudget_demo_fills_the_first_household(tmp_path):
    a = create_app(make_settings(tmp_path, demo=True, admin_username='boss', admin_password=PASSWORD))
    a.state.stop.set()
    c = login(a, 'boss')
    loc = c.get('/', follow_redirects=False).headers['location']
    c.hid = int(loc.split('/')[2])
    c.csrf = page_csrf(c, loc)
    assert c.api('GET', '/uploads').json()['uploads']
    # and the setup page offers the demo first when HBUDGET_DEMO=1
    b = create_app(make_settings(tmp_path / 'other', demo=True))
    b.state.stop.set()
    assert 'value="demo" selected' in Client(b).get('/setup').text
