"""Labels on recurring items and rows, and items with nothing to schedule (D-104): the page's API, the agent
API, MCP and the backup."""
from __future__ import annotations

from test_api_v1 import anon, get, make_token, post, tool  # noqa: F401 - anon is a fixture


def _series(admin, name='Allowance', **kw):
    body = {'name': name, 'kind': 'bill', 'amount': 5000, 'schedule': {'cadence': 'monthly', 'start': '2029-03-03'},
            **kw}
    r = admin.api('POST', '/series', body)
    assert r.status_code == 200, r.text
    return r.json()['id'], body


def _rows(admin, name):
    return [r for r in admin.api('GET', '/ledger').json()['rows'] if r['name'] == name]


def test_item_labels_row_labels_and_nothing_to_schedule(admin):
    sid, body = _series(admin, labels=[' blue  card ', 'Blue Card'], no_schedule=True)
    s = next(x for x in admin.api('GET', '/series').json()['series'] if x['id'] == sid)
    assert (s['labels'], s['no_schedule']) == (['blue card'], True)
    first = _rows(admin, 'Allowance')[0]
    assert (first['labels'], first['own_labels'], first['no_schedule']) == (['blue card'], [], True)
    out = admin.api('POST', '/rows/%s/labels' % first['id'], {'labels': ['paid early', 'BLUE CARD']})
    assert out.json() == {'id': first['id'], 'labels': ['blue card', 'paid early'],
                          'own_labels': ['paid early', 'BLUE CARD']}
    led = admin.api('GET', '/ledger').json()
    assert led['labels'] == ['blue card', 'paid early']
    # the row keeps its own labels when the item changes; a body without labels keeps the item's
    plain = {k: v for k, v in body.items() if k not in ('labels', 'no_schedule')}
    assert admin.api('PUT', '/series/%s' % sid, {**plain, 'amount': 6000}).status_code == 200
    first = _rows(admin, 'Allowance')[0]
    assert (first['amount'], first['labels'], first['no_schedule']) == (6000, ['blue card', 'paid early'], True)
    # not in the check-in's "schedule what is due", nor in the summary's count
    assert [r['name'] for r in admin.api('GET', '/checkin').json()['due']] == []
    assert admin.api('GET', '/summary').json()['unscheduled']['count'] == 0
    assert admin.api('PUT', '/series/%s' % sid, {**body, 'labels': [], 'no_schedule': False}).status_code == 200
    assert [r['name'] for r in admin.api('GET', '/checkin').json()['due']] == ['Allowance']
    assert admin.api('GET', '/summary').json()['unscheduled']['count'] == 1
    assert _rows(admin, 'Allowance')[0]['labels'] == ['paid early', 'BLUE CARD']
    # refusals
    assert admin.api('POST', '/rows/%s/labels' % first['id'], {'labels': ['x' * 25]}).status_code == 400
    assert admin.api('POST', '/rows/%s/labels' % first['id'], {'labels': list('abcdef')}).status_code == 400
    assert admin.api('POST', '/rows/nope/labels', {'labels': []}).status_code == 404
    assert admin.api('POST', '/series', {**body, 'labels': ['y' * 30]}).status_code == 400


def test_labels_in_the_agent_api_mcp_and_the_backup(admin, anon):  # noqa: F811
    sid, _ = _series(admin)
    t = make_token(admin, scope='write')
    bills = get(anon, '/bills', t, params={'days': 10}).json()['bills']
    bill = next(b for b in bills if b['name'] == 'Allowance')
    assert (bill['labels'], bill['needs_scheduling']) == ([], True)
    assert tool(anon, t, 'set_series_details', series_id=sid, labels=['blue card'], no_schedule=True)['ok']
    assert tool(anon, t, 'set_bill_labels', bill_id=bill['id'], labels=['paid early'])['labels'] == [
        'blue card', 'paid early']
    assert post(anon, '/bills/%s/labels' % bill['id'], t, {'labels': ['once']}).json()['own_labels'] == ['once']
    got = next(b for b in tool(anon, t, 'list_upcoming_bills', days=10)['bills'] if b['id'] == bill['id'])
    assert (got['labels'], got['needs_scheduling']) == (['blue card', 'once'], False)
    listed = next(s for s in tool(anon, t, 'list_series')['series'] if s['id'] == sid)
    assert (listed['labels'], listed['no_schedule']) == (['blue card'], True)
    # export, wipe, restore: the labels and the switch come back
    backup = admin.get('/api/h/%d/export/backup.json' % admin.hid)
    assert admin.api('POST', '/wipe', {'confirm': 'Test household'}).status_code == 200
    r = admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('b.json', backup.content)},
                   data={'mode': 'replace', 'dry_run': '0', 'confirm': 'Test household'},
                   headers={'X-CSRF-Token': admin.csrf})
    assert r.status_code == 200, r.text
    s = next(x for x in admin.api('GET', '/series').json()['series'] if x['id'] == sid)
    assert (s['labels'], s['no_schedule']) == (['blue card'], True)
    assert next(x for x in _rows(admin, 'Allowance') if x['id'] == bill['id'])['labels'] == ['blue card', 'once']
