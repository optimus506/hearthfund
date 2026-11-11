"""Phase 2: the five-section navigation and the Home page."""
from __future__ import annotations

import re

from test_api import bill, pay
from test_static import js

from hbudget.db import connection, repo, users
from webhelpers import add_user, login


def sections(html: str) -> list[str]:
    nav = html[html.index('<nav class="sections"'):]
    return re.findall(r'data-s="(\w+)"', nav[:nav.index('</nav>')])


def sub(html: str) -> list[str]:
    if '<nav class="tabs"' not in html:
        return []
    nav = html[html.index('<nav class="tabs"'):]
    return re.findall(r'href="/h/\d+/(\w+)"', nav[:nav.index('</nav>')])


def test_household_root_is_home_and_sign_in_lands_there(admin, app):
    h = '/h/%d' % admin.hid
    r = admin.get(h)
    assert r.status_code == 200 and '<h1>Home</h1>' in r.text and '/js/home.js"' in r.text
    assert admin.get(h + '/home').status_code == 200
    assert admin.get('/switch?hid=%d' % admin.hid, follow_redirects=False).headers['location'] == h
    c = login(app, 'admin')
    assert c.get('/', follow_redirects=False).headers['location'] == h


def test_five_sections_and_each_sections_pages(admin):
    h = '/h/%d/' % admin.hid
    for page, section, pages in (('home', 'home', []), ('bills', 'bills', ['bills', 'series', 'debts', 'checkup']),
                                 ('series', 'bills', ['bills', 'series', 'debts', 'checkup']),
                                 ('debts', 'bills', ['bills', 'series', 'debts', 'checkup']),
                                 ('plan', 'plan', ['plan', 'goals', 'calendar']),
                                 ('goals', 'plan', ['plan', 'goals', 'calendar']),
                                 ('card', 'money', ['reconcile', 'report', 'card', 'subscriptions', 'recap']),
                                 ('notes', 'more', ['rules', 'settings', 'notes', 'import', 'export', 'members',
                                                    'access']),
                                 ('more', 'more', [])):
        html = admin.get(h + page).text
        assert sections(html) == ['home', 'bills', 'plan', 'money', 'more'], page
        current = re.search(r'data-s="(\w+)" aria-current="page"', html)
        assert current and current.group(1) == section, page
        assert sub(html) == pages, page
        assert ('id="strip"' in html) == (page not in ('notes', 'more')), page


def test_owner_only_pages_are_hidden_from_others(admin, app, tmp_path):
    add_user(tmp_path, admin.hid, 'v1', 'viewer')
    v = login(app, 'v1')
    assert sub(v.get('/h/%d/rules' % admin.hid).text) == ['rules', 'settings', 'notes', 'export']
    html = v.get('/h/%d/more' % admin.hid).text
    links = re.findall(r'<li><a href="/h/\d+/(\w+)">', html)
    assert links == ['rules', 'settings', 'notes', 'export'] and 'href="/admin"' not in html
    assert 'href="/admin"' in admin.get('/h/%d/more' % admin.hid).text


def menu(html: str) -> str:
    return html[html.index('<details class="menu">'):html.index('</details>')]


def test_account_menu_and_household_switcher(admin, tmp_path):
    m = menu(admin.get('/h/%d/bills' % admin.hid).text)
    assert 'href="/account"' in m and 'action="/logout"' in m and 'id="theme"' in m and 'href="/admin"' in m
    assert 'Households' not in m        # only one household
    conn = connection.connect(tmp_path / 'hbudget.db')
    try:
        hid2 = repo.create_household(conn, 'Second home')
        repo.add_member(conn, hid2, users.get_user_by_name(conn, 'admin').id, 'owner')  # type: ignore[union-attr]
        conn.commit()
    finally:
        conn.close()
    m = menu(admin.get('/h/%d/bills' % admin.hid).text)
    assert 'Households' in m and 'href="/h/%d">Second home</a>' % hid2 in m
    assert 'href="/h/%d" aria-current="true"' % admin.hid in m


def test_home_api(admin):
    empty = admin.api('GET', '/home').json()
    assert empty['periods'] == [] and empty['today'] == '2029-03-01' and 'alerts' in empty['summary']
    pid = pay(admin, 300000, 15)
    bill(admin, 'Electric', 15000, 6)
    bill(admin, 'Car loan', 30000, 10, funded_by=pid)    # funded by the 15th: after payday
    bill(admin, 'Rent', 100000, 1)
    d = admin.api('GET', '/home').json()
    zero = d['periods'][0]
    assert zero['start'] is None and zero['end'] == '2029-03-15'
    assert [r['name'] for r in zero['rows']] == ['Rent', 'Electric']
    assert zero['out'] == 115000 and zero['balance_end'] == d['summary']['balance'] - 115000
    assert [r['status'] for r in zero['rows']] == ['none', 'none']
    first = d['periods'][1]
    assert first['start'] == '2029-03-15' and first['pay_in'] == 300000
    assert 'Car loan' in [r['name'] for r in first['rows']]
    assert len(d['periods']) <= 4
    # the sparkline covers the next 60 days and ends at the horizon
    assert d['spark'][0][0] == '2029-03-01' and d['spark'][-1][0] == '2029-04-30'
    assert all(p[0] <= '2029-04-30' for p in d['spark'])


def test_alerts_only_on_home(admin):
    bill(admin, 'Electric', 15000, 6)       # due within 7 days, not scheduled: an alert
    s = js(admin, 'summary')
    assert "$('alertlink')" in s and "if (box)" in s and "'/home#alerts'" in s
    assert 'id="alerts"' in admin.get('/h/%d' % admin.hid).text
    bills = admin.get('/h/%d/bills' % admin.hid).text
    assert 'id="alerts"' not in bills and 'id="alertlink"' in bills


def test_pay_and_bills_page_lists_pay_separately(admin):
    html = admin.get('/h/%d/series' % admin.hid).text
    assert '>Bill setup</a>' in html and '<h2 id="h-s">Pay, bills and spending</h2>' in html and 'href="debts"' in html
    src = admin.get('/static/js/series.js').text
    assert "['income', 'Pay'" in src and "['bill', 'Bills'" in src and "['spending', 'Spending budgets'" in src


def test_weekly_check_in(admin, app, tmp_path):
    from test_api import bill

    from webhelpers import page_csrf
    rent = bill(admin, 'Rent', 100000, 5)
    assert rent
    c = admin.api('GET', '/checkin').json()
    assert c['last'] is None and c['your_last'] is None and c['done_this_week'] is False
    assert [r['name'] for r in c['due']] == ['Rent'] and c['bank']['waiting'] == []
    # someone else changes a row: it shows, named, until I check in; my own changes never show
    add_user(tmp_path, admin.hid, 'partner', 'editor')
    p = login(app, 'partner')
    p.hid, p.csrf = admin.hid, page_csrf(p, '/h/%d/bills' % admin.hid)
    row = c['due'][0]
    assert p.api('POST', '/rows/%s/status' % row['id'], {'status': 'manual'}).status_code == 200
    c = admin.api('GET', '/checkin').json()
    assert {'by': 'partner', 'what': 'marked', 'item': 'Rent'}.items() <= c['changes'][0].items()
    assert all(x['by'] != 'admin' for x in c['changes']) and c['due'] == []
    assert admin.api('POST', '/checkin', {}).status_code == 200
    c = admin.api('GET', '/checkin').json()
    assert c['done_this_week'] is True and c['last']['by'] == 'admin' and c['changes'] == []
    # the partner still sees the admin's changes and check-in is not one of them
    pc = p.api('GET', '/checkin').json()
    assert pc['your_last'] is None and all(x['what'] != 'checked in' for x in pc['changes'])
    assert 'id="checkSteps"' in admin.get('/h/%d/home' % admin.hid).text
