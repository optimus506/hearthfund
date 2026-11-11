"""The Subscriptions page, /recurring and list_recurring_charges (#34, D-084)."""
from __future__ import annotations

from test_api_v1 import anon, get, make_token, tool  # noqa: F401 - anon is a fixture
from test_reconcile_flow import upload

MONTHS = ['2028-%02d' % m for m in range(5, 13)] + ['2029-01', '2029-02']     # the web tests' today is 2029-03-01


def line(ym: str, day: int, desc: str, amount: str) -> str:
    y, m = ym.split('-')
    return '%s/%02d/%s,%s,-%s\n' % (m, day, y, desc, amount)


BANK = ('Date,Description,Amount\n'
        + ''.join(line(ym, 5, 'PROCESSOR *MUSIC', '22.00' if ym < '2028-10' else '26.00') for ym in MONTHS)
        + ''.join(line(ym, 12, 'PROCESSOR *CLOUD', '9.99') for ym in MONTHS[1:5])
        + '02/03/2029,CORNER MARKET,-41.20\n01/03/2029,CORNER MARKET,-38.75\n').encode()


def test_recurring_page_api_and_tool(admin, anon):  # noqa: F811 - the anon fixture
    assert admin.api('GET', '/recurring').json()['recurring']['items'] == []
    assert upload(admin, 'bank.csv', BANK).status_code == 200
    r = admin.api('GET', '/recurring').json()['recurring']
    items = {i['payee']: i for i in r['items']}
    assert set(items) == {'PROCESSOR *MUSIC', 'PROCESSOR *CLOUD'}
    music = items['PROCESSOR *MUSIC']
    assert music['amount'] == 2600 and music['status'] in ('active', 'new') and music['price_changes'] == [
        {'from': '2028-10-05', 'old': 2200, 'new': 2600}] and music['rule'] is None and music['budget_lines'] == []
    assert items['PROCESSOR *CLOUD']['status'] == 'stopped' and items['PROCESSOR *CLOUD']['next'] == '2028-10-12'
    assert r['monthly_total'] == sum(i['monthly'] for i in r['items'] if i['status'] != 'stopped')
    assert r['bills_monthly'] == 0 and r['subscriptions_monthly'] == r['monthly_total']
    assert (music['group'], music['generic'], music['latest']) == ('subscription', False, None)
    page = admin.get('/h/%d/subscriptions' % admin.hid)
    assert page.status_code == 200 and 'subscriptions.js' in page.text
    ro = make_token(admin, 'read')
    assert tool(anon, ro, 'list_recurring_charges')['recurring'] == r
    assert get(anon, '/recurring', ro).json()['recurring'] == r


def test_not_a_subscription(admin, anon):  # noqa: F811 - the anon fixture
    assert upload(admin, 'bank.csv', BANK).status_code == 200
    r = admin.api('PUT', '/recurring/hidden', {'source': 'bank', 'payee': 'PROCESSOR *CLOUD'})
    assert r.status_code == 200 and r.json() == {'source': 'bank', 'payee': 'PROCESSOR *CLOUD', 'hidden': True}
    rec = admin.api('GET', '/recurring').json()['recurring']
    assert [i['payee'] for i in rec['items']] == ['PROCESSOR *MUSIC']
    assert [i['payee'] for i in rec['hidden']] == ['PROCESSOR *CLOUD']
    assert 'recurring.hide' in [a['action'] for a in admin.api('GET', '/audit').json()['entries']]
    # in the backup; a restore keeps it; the wipe clears it
    backup = admin.get('/api/h/%d/export/backup.json' % admin.hid)
    assert backup.json()['not_recurring'] == [{'source': 'bank', 'payee': 'PROCESSOR *CLOUD'}]
    assert admin.api('POST', '/wipe', {'confirm': 'Test household'}).status_code == 200
    assert upload(admin, 'bank.csv', BANK).status_code == 200
    assert admin.api('GET', '/recurring').json()['recurring']['hidden'] == []
    r = admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('b.json', backup.content)},
                   data={'mode': 'replace', 'dry_run': '0', 'confirm': 'Test household'},
                   headers={'X-CSRF-Token': admin.csrf})
    assert r.status_code == 200, r.text
    assert [i['payee'] for i in admin.api('GET', '/recurring').json()['recurring']['hidden']] == ['PROCESSOR *CLOUD']
    # show it again, through the tool; a read token cannot
    ro, rw = make_token(admin, 'read'), make_token(admin, 'write', name='Agent')
    assert anon.put('/api/v1/recurring/hidden', headers={'Authorization': 'Bearer ' + ro},
                    json={'source': 'bank', 'payee': 'X'}).status_code == 403
    shown = tool(anon, rw, 'hide_recurring_charge', source='bank', payee='PROCESSOR *CLOUD', hidden=False)
    assert shown['hidden'] is False
    assert admin.api('GET', '/recurring').json()['recurring']['hidden'] == []
    assert admin.api('PUT', '/recurring/hidden', {'source': 'atm', 'payee': 'X'}).status_code == 400


def test_repeating_payee_alert_can_be_set_aside_until_it_posts_again(admin):
    """#104: Home's note about a repeating payee has an "ignore until it posts again" action that the backup keeps;
    every page's alert line counts it."""
    assert upload(admin, 'bank.csv', BANK).status_code == 200

    def notes() -> list[dict[str, object]]:
        return [a for a in admin.api('GET', '/summary').json()['alerts'] if a['code'] == 'unmatched_recurring'
                and a['data']['payee'] != 'CORNER MARKET']
    got = notes()
    # MUSIC posts every month up to February; CLOUD stopped in September, so it has aged out
    assert [a['data'] for a in got] == [{'payee': 'PROCESSOR *MUSIC'}] and got[0]['severity'] == 'info'
    r = admin.api('PUT', '/alerts/snooze', {'alert': 'unmatched_recurring', 'payee': 'PROCESSOR *MUSIC'})
    assert r.status_code == 200 and r.json()['snoozed'] is True
    assert notes() == []
    assert 'alert.snooze' in [a['action'] for a in admin.api('GET', '/audit').json()['entries']]
    backup = admin.get('/api/h/%d/export/backup.json' % admin.hid).json()
    assert backup['alert_snoozes'] == [{'alert': 'unmatched_recurring', 'payee': 'PROCESSOR *MUSIC',
                                        'since': '2029-03-01'}]
    assert admin.api('PUT', '/alerts/snooze', {'alert': 'unmatched_recurring', 'payee': 'PROCESSOR *MUSIC',
                                               'snoozed': False}).status_code == 200
    assert len(notes()) == 1
    assert admin.api('PUT', '/alerts/snooze', {'alert': 'cushion', 'payee': 'x'}).status_code == 400
