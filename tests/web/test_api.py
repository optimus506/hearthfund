"""Each tab's API on a small fake household (round amounts, 2029)."""
from __future__ import annotations

import io

import openpyxl
import pytest

from webhelpers import Client, add_user, login, page_csrf


def bill(c: Client, name: str, amount: int, day: int, **kw: object) -> str:
    body = {'name': name, 'kind': 'bill', 'amount': amount,
            'schedule': {'cadence': 'monthly', 'start': '2029-03-01', 'day': day}, **kw}
    r = c.api('POST', '/series', body)
    assert r.status_code == 200, r.text
    return r.json()['id']


def pay(c: Client, amount: int = 200000, day: int = 15) -> str:
    r = c.api('POST', '/series', {'name': 'Pay A', 'kind': 'income', 'amount': amount,
                                  'schedule': {'cadence': 'monthly', 'start': '2029-03-01', 'day': day}})
    assert r.status_code == 200, r.text
    return r.json()['id']


def test_series_make_rows_and_the_forecast(admin):
    pid = pay(admin)
    bill(admin, 'Electric', 15000, 6)
    bill(admin, 'Car loan', 30000, 10, funded_by=pid)
    d = admin.api('GET', '/ledger').json()
    names = [(r['name'], r['due']) for r in d['rows'][:4]]
    # the car loan due on the 10th is listed after the payday on the 15th that funds it
    assert names == [('Electric', '2029-03-06'), ('Pay A', '2029-03-15'), ('Car loan', '2029-03-10'),
                     ('Electric', '2029-04-06')]
    assert d['rows'][0]['balance'] == 100000 - 15000
    s = admin.api('GET', '/summary').json()
    assert s['balance'] == 100000 and s['low'] == 85000 and s['low_date'] == '2029-03-06'
    assert s['year_end']['year'] == 2029
    assert d['chart'][0] == ['2029-03-01', 100000, None, 0]
    assert d['chart'][1] == ['2029-03-06', 85000, 'Electric', 1]     # the tooltip says "after Electric"


def test_row_edits_with_stale_check(admin):
    bill(admin, 'Electric', 15000, 6)
    rid = admin.api('GET', '/ledger').json()['rows'][0]['id']
    assert admin.api('POST', '/rows/%s/change' % rid, {'amount': 12000, 'expected_amount': 99}).status_code == 409
    assert admin.api('POST', '/rows/%s/change' % rid, {'amount': 12000, 'expected_amount': 15000}).status_code == 200
    row = admin.api('GET', '/ledger').json()['rows'][0]
    assert row['amount'] == 12000 and row['amount_was'] == 15000
    assert admin.api('POST', '/rows/%s/undo' % rid, {}).status_code == 200
    assert admin.api('POST', '/rows/%s/remove' % rid, {}).status_code == 200
    end_before = admin.api('GET', '/summary').json()['end']
    assert admin.api('POST', '/rows/%s/remove' % rid, {}).status_code == 400   # already removed
    row = admin.api('GET', '/ledger').json()['rows'][0]
    assert row['state'] == 'removed'
    assert admin.api('POST', '/rows/%s/undo' % rid, {}).status_code == 200
    assert admin.api('GET', '/summary').json()['end'] == end_before - 15000
    assert admin.api('POST', '/rows/%s/remove' % rid, {}).status_code == 200
    assert admin.api('POST', '/rows/%s/undo' % rid, {}).status_code == 200
    assert admin.api('POST', '/rows/%s/status' % rid, {'status': 'autopay'}).status_code == 200
    assert admin.api('GET', '/ledger').json()['rows'][0]['status'] == 'autopay'
    assert admin.api('POST', '/rows/nope/change', {'amount': 1}).status_code == 404


def test_row_already_paid_leaves_the_forecast_and_can_be_undone(admin):
    bill(admin, 'Electric', 15000, 6)
    rid = admin.api('GET', '/ledger').json()['rows'][0]['id']
    end_before = admin.api('GET', '/summary').json()['end']
    assert admin.api('POST', '/rows/%s/done' % rid, {'expected_amount': 99}).status_code == 409
    assert admin.api('POST', '/rows/%s/done' % rid, {}).status_code == 200
    assert admin.api('POST', '/rows/%s/done' % rid, {}).status_code == 400   # already done
    row = next(r for r in admin.api('GET', '/ledger').json()['rows'] if r['id'] == rid)
    assert row['state'] == 'cleared'                                           # still listed, so Undo is offered
    assert admin.api('GET', '/summary').json()['end'] == end_before + 15000
    assert admin.api('POST', '/rows/%s/undo' % rid, {}).status_code == 200
    assert admin.api('GET', '/summary').json()['end'] == end_before
    assert admin.api('POST', '/rows/%s/change' % rid, {'amount': -5}).status_code == 400
    r = admin.api('POST', '/rows', {'name': 'Gift', 'kind': 'bill', 'amount': 5000, 'due': '2029-03-20'})
    assert r.status_code == 200
    rows = admin.api('GET', '/ledger').json()['rows']
    anchor = next(x['id'] for x in rows if x['state'] == 'open' and x['id'] not in (rid, r.json()['id']))
    assert admin.api('POST', '/rows/%s/move' % r.json()['id'], {'after': anchor}).status_code == 200
    assert admin.api('POST', '/rows/%s/move' % r.json()['id'], {'after': rid}).status_code == 400   # removed anchor


def test_whatif_does_not_save(admin):
    admin.api('POST', '/rows', {'name': 'Electric', 'kind': 'bill', 'amount': 15000, 'due': '2029-03-06'})
    r = admin.api('POST', '/whatif', {'items': [{'label': 'Repair', 'amount': 95000, 'direction': 'out',
                                                 'date': '2029-03-07'}]})
    d = r.json()
    assert d['delta']['verdict'] == 'no' and d['whatif']['low'] == -10000 and d['base']['low'] == 85000
    assert admin.api('GET', '/summary').json()['low'] == 85000
    r = admin.api('POST', '/whatif', {'items': [{'amount': 1000, 'direction': 'in', 'date': '2029-03-07'}]})
    assert r.json()['delta']['verdict'] == 'yes'


def test_expected_items(admin):
    r = admin.api('POST', '/expected', {'label': 'Refund', 'amount': 2000, 'direction': 'in', 'date': '2029-03-08'})
    eid = r.json()['id']
    assert admin.api('GET', '/summary').json()['end'] == 102000
    assert admin.api('POST', '/expected/%s/status' % eid, {'status': 'done'}).status_code == 200
    assert admin.api('GET', '/summary').json()['end'] == 100000
    assert admin.api('PUT', '/expected/%s' % eid, {'label': 'Refund', 'amount': 3000, 'direction': 'in',
                                                   'date': '2029-03-08'}).status_code == 200
    assert admin.api('GET', '/expected').json()['items'][0]['amount'] == 3000
    assert admin.api('DELETE', '/expected/%s' % eid).status_code == 200
    assert admin.api('DELETE', '/expected/%s' % eid).status_code == 404
    assert admin.api('POST', '/expected', {'label': '', 'amount': 1, 'direction': 'in',
                                           'date': '2029-03-08'}).status_code == 400


def test_series_edit_seasonal_status_delete(admin):
    sid = bill(admin, 'Lawn', 10000, 20)
    r = admin.api('POST', '/series/%s/seasonal' % sid, {'rules': [{'months': '5-9', 'amount': 12000},
                                                                   {'months': '10-4', 'amount': 4000}]})
    d = r.json()
    assert d['dry_run'] and d['changes'] and all(c['now'] in (12000, 4000) for c in d['changes'])
    assert admin.api('GET', '/ledger').json()['rows'][0]['amount'] == 10000   # dry run saved nothing
    r = admin.api('POST', '/series/%s/seasonal' % sid,
                  {'rules': [{'months': '10-4', 'amount': 4000}], 'dry_run': False})
    assert r.status_code == 200
    assert admin.api('GET', '/ledger').json()['rows'][0]['amount'] == 4000
    assert admin.api('POST', '/series/%s/seasonal' % sid, {'rules': [{'months': '13', 'amount': 1}]}).status_code == 400
    assert admin.api('POST', '/series/%s/status' % sid, {'status': 'autopay'}).status_code == 200
    assert admin.api('GET', '/series').json()['series'][0]['status'] == 'autopay'
    body = {'name': 'Lawn care', 'kind': 'bill', 'amount': 9000,
            'schedule': {'cadence': 'monthly', 'start': '2029-03-01', 'day': 21}}
    assert admin.api('PUT', '/series/%s' % sid, body).status_code == 200
    rows = admin.api('GET', '/ledger').json()['rows']
    assert rows[0]['name'] == 'Lawn care'
    bad = dict(body, schedule={'cadence': 'semimonthly', 'start': '2029-03-01', 'days': [5, 5]})
    assert admin.api('PUT', '/series/%s' % sid, bad).status_code == 400
    assert admin.api('POST', '/generate', {}).status_code == 200
    assert admin.api('DELETE', '/series/%s' % sid).status_code == 200
    assert admin.api('GET', '/ledger').json()['rows'] == []


def test_debts_and_payoff(admin):
    r = admin.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 300000, 'apr_bp': 2000,
                                        'payment': 20000, 'limit': 500000})
    aid = r.json()['id']
    admin.api('POST', '/accounts', {'name': 'Store plan', 'kind': 'loan', 'balance': 100000, 'payment': 10000,
                                    'promo_ends': '2029-09-01', 'apr_after_bp': 2500, 'deferred_interest': True})
    admin.api('POST', '/accounts', {'name': 'Home loan', 'kind': 'mortgage', 'balance': 20000000, 'apr_bp': 600,
                                    'payment': 150000})
    acc = admin.api('GET', '/accounts').json()['accounts']
    assert acc[0]['utilization_bp'] == 6000 and acc[0]['monthly_interest'] == 5000
    assert acc[2]['is_mortgage']
    p = admin.api('POST', '/payoff', {'method': 'avalanche', 'extra': 5000}).json()
    assert 'Home loan' in p['excluded'] and p['result']['done']
    assert p['interest_saved'] >= 0 and p['promos'][0]['name'] == 'Store plan'
    p2 = admin.api('POST', '/payoff', {'include_mortgage': True, 'once': 100000, 'once_month': '2029-12'}).json()
    assert 'Home loan' not in p2['excluded']
    assert admin.api('POST', '/payoff', {'once_month': '2029-13'}).status_code == 400
    assert admin.api('POST', '/accounts', {'name': 'Card 1234567890', 'kind': 'credit_card'}).status_code == 400
    assert admin.api('PUT', '/accounts/%s' % aid, {'name': 'Card A', 'kind': 'credit_card',
                                                   'balance': 1}).status_code == 200
    assert admin.api('DELETE', '/accounts/%s' % aid).status_code == 200


def test_reports_on_an_empty_household(admin):
    assert admin.api('GET', '/flow').json()['flow']['sources'] == []
    assert admin.api('GET', '/flow?view=planned').status_code == 200
    assert admin.api('GET', '/flow?view=other').status_code == 400
    assert admin.api('GET', '/checkup').json()['checkup']['bills'] == []
    d = admin.api('GET', '/card').json()
    assert d['summary']['weeks'] == []
    r = admin.api('PUT', '/card/categories', {'categories': [{'name': 'Grocery', 'keywords': ['market']},
                                                             {'name': 'Fuel', 'keywords': ['fuel']}]})
    assert r.status_code == 200
    assert [c['keywords'] for c in admin.api('GET', '/card').json()['categories']] == [['MARKET'], ['FUEL']]
    assert admin.api('PUT', '/card/categories', {'categories': [{'name': 'A'}, {'name': 'A'}]}).status_code == 400


def test_rules_crud_order_test_and_safe_patterns(admin):
    sid = bill(admin, 'Electric', 15000, 6)
    r1 = admin.api('POST', '/rules', {'name': 'Electric', 'kind': 'bill', 'contains': ['ELECTRIC CO'],
                                      'targets': [sid]}).json()['id']
    r2 = admin.api('POST', '/rules', {'name': 'Coffee', 'kind': 'track', 'regex': r'CAFE \d+'}).json()['id']
    t = admin.api('POST', '/rules/test', {'description': 'ELECTRIC CO ACH 1234567890', 'amount': -15000}).json()
    assert t['first'] == 'Electric'
    assert admin.api('POST', '/rules/order', {'ids': [r2, r1]}).status_code == 200
    assert [x['id'] for x in admin.api('GET', '/rules').json()['rules']] == [r2, r1]
    assert admin.api('POST', '/rules/order', {'ids': [r1]}).status_code == 400
    slow = admin.api('POST', '/rules', {'name': 'Bad', 'kind': 'track', 'regex': r'(a|aa)+$'})
    assert slow.status_code == 400 and 'refused' in slow.json()['message']
    assert admin.api('POST', '/rules', {'name': 'Card', 'kind': 'card', 'contains': ['CARD']}).status_code == 400
    assert admin.api('POST', '/rules', {'name': 'X', 'kind': 'bill', 'contains': ['X'],
                                        'targets': ['nope']}).status_code == 400
    assert admin.api('POST', '/rules', {'name': 'Empty', 'kind': 'bill'}).status_code == 400
    body = {'name': 'Electric', 'kind': 'bill', 'contains': ['ELEC']}
    assert admin.api('PUT', '/rules/%s' % r1, body).status_code == 200
    assert admin.api('DELETE', '/rules/%s' % r2).status_code == 200


def settings_body(c: Client, **over: object) -> dict[str, object]:
    d = c.api('GET', '/rules').json()
    s = d['settings']
    body = {'alerts': {**s['alerts'], 'caps': d['caps']}, 'reconcile': s['reconcile'], 'horizon': d['horizon'],
            'status_colours': s['status_colours'], 'week_starts': s['week_starts'], 'flow_groups': d['flow_groups'],
            'expected_version': d['version']}
    body.update(over)
    return body


def test_settings_balance_caps_groups_and_stale(admin):
    rid = admin.api('POST', '/rules', {'name': 'Shops', 'kind': 'track', 'contains': ['SHOP']}).json()['id']
    body = settings_body(admin, start_balance=250000, as_of='2029-03-01')
    body['alerts'] = {**body['alerts'], 'cushion': 50000,  # type: ignore[dict-item]
                      'caps': [{'id': 'c1', 'name': 'Shops', 'rule_ids': [rid], 'period': 'week', 'amount': 5000}]}
    body['flow_groups'] = [{'name': 'Home', 'colour': '#3b6fb6', 'rule_ids': [rid]}]
    r = admin.api('PUT', '/settings', body)
    assert r.status_code == 200, r.text
    d = admin.api('GET', '/rules').json()
    assert d['start_balance'] == 250000 and d['settings']['alerts']['cushion'] == 50000
    assert d['caps'][0]['amount'] == 5000 and d['flow_groups'][0]['colour'] == '#3b6fb6'
    assert admin.api('GET', '/summary').json()['balance'] == 250000
    # the old updated_at is stale now
    assert admin.api('PUT', '/settings', body).status_code == 409
    bad = settings_body(admin)
    bad['status_colours'] = {'autopay': 'red; background:url(x)', 'manual': '#ec9ba4'}
    assert admin.api('PUT', '/settings', bad).status_code == 400
    bad = settings_body(admin, horizon={'mode': 'fixed', 'months': 15, 'end': None})
    assert admin.api('PUT', '/settings', bad).status_code == 400


def test_caps_and_groups_have_their_own_saves(admin):
    rid = admin.api('POST', '/rules', {'name': 'Shops', 'kind': 'track', 'contains': ['SHOP']}).json()['id']
    cap = {'id': 'c1', 'name': 'Shops', 'rule_ids': [rid], 'period': 'week', 'amount': 5000}
    assert admin.api('PUT', '/caps', {'caps': [cap]}).status_code == 200
    assert admin.api('PUT', '/caps', {'caps': [{**cap, 'rule_ids': ['nope']}]}).status_code == 400
    group = {'name': 'Home', 'colour': '#3b6fb6', 'rule_ids': [rid]}
    assert admin.api('PUT', '/flow-groups', {'groups': [group]}).status_code == 200
    assert admin.api('PUT', '/flow-groups', {'groups': [group, group]}).status_code == 400
    # the Settings page sends neither: saving it keeps them
    body = settings_body(admin, start_balance=1000)
    del body['flow_groups']
    del body['alerts']['caps']  # type: ignore[attr-defined]
    assert admin.api('PUT', '/settings', body).status_code == 200
    d = admin.api('GET', '/rules').json()
    assert [c['id'] for c in d['caps']] == ['c1'] and [g['name'] for g in d['flow_groups']] == ['Home']
    assert d['start_balance'] == 1000
    assert admin.api('PUT', '/caps', {'caps': []}).status_code == 200
    assert admin.api('GET', '/rules').json()['caps'] == []
    pages = {p: admin.get('/h/%d/%s' % (admin.hid, p)).text for p in ('rules', 'settings', 'checkup', 'report')}
    assert 'id="settingsForm"' in pages['settings'] and 'id="settingsForm"' not in pages['rules']
    assert 'id="capForm"' in pages['checkup'] and 'id="fgForm"' in pages['report']


def test_notes(admin):
    page = admin.get('/h/%d/notes' % admin.hid).text
    r = admin.api('PUT', '/notes', {'notes': '<script>alert(1)</script>\nline two'})
    assert r.status_code == 200
    page = admin.get('/h/%d/notes' % admin.hid).text
    assert '<script>alert(1)</script>' not in page and '\\u003cscript\\u003e' in page
    assert admin.api('PUT', '/notes', {'notes': 'x', 'expected_updated_at': 'old'}).status_code == 409


def test_exports_and_formula_injection(admin):
    bill(admin, '=HYPERLINK("http://evil.example","x")', 15000, 6)
    r = admin.get('/api/h/%d/export/forecast.csv' % admin.hid)
    assert r.status_code == 200 and 'attachment' in r.headers['content-disposition']
    assert 'filename="hearthfund-test-household-' in r.headers['content-disposition']
    assert '"\'=HYPERLINK' in r.text or "'=HYPERLINK" in r.text
    assert ',-150.00,' in r.text or ',150.00,' in r.text
    r = admin.get('/api/h/%d/export/forecast.xlsx' % admin.hid)
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    cells = [c for row in wb.active.iter_rows() for c in row]
    assert not any(c.data_type == 'f' for c in cells)          # stored as text, never as a formula
    assert any(isinstance(c.value, str) and 'HYPERLINK' in c.value for c in cells)
    r = admin.get('/api/h/%d/export/backup.yaml' % admin.hid)
    assert r.status_code == 200 and 'household' in r.text
    assert admin.get('/api/h/%d/export/backup.json' % admin.hid).json()['household']['name'] == 'Test household'


@pytest.mark.parametrize('fmt', ['yaml', 'json'])
def test_backup_round_trip_through_the_api(admin, fmt):
    pid = pay(admin)
    bill(admin, 'Electric', 15000, 6, funded_by=pid)
    admin.api('PUT', '/notes', {'notes': 'keep me'})
    before = admin.api('GET', '/summary').json()
    rows_before = admin.api('GET', '/ledger').json()['rows']
    text = admin.get('/api/h/%d/export/backup.%s' % (admin.hid, fmt)).content
    assert admin.api('POST', '/wipe', {'confirm': 'wrong'}).status_code == 400
    assert admin.api('POST', '/wipe', {'confirm': 'Test household'}).status_code == 200
    assert admin.api('GET', '/ledger').json()['rows'] == []
    def restore(**form: str):  # type: ignore[no-untyped-def]
        return admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('backup.' + fmt, text)},
                          data={'mode': 'replace', **form}, headers={'X-CSRF-Token': admin.csrf})
    dry = restore(dry_run='1').json()
    assert dry['is_backup'] and {'rows', 'bank_txns', 'card_txns', 'uploads', 'reconciliations',
                                 'profiles'} <= set(dry['replaces'])
    r = restore(dry_run='0')
    assert r.status_code == 400 and r.json()['error'] == 'confirm'
    assert restore(dry_run='0', confirm='wrong name').status_code == 400
    r = restore(dry_run='0', confirm='Test household')
    assert r.status_code == 200, r.text
    after = admin.api('GET', '/summary').json()
    assert {k: before[k] for k in ('balance', 'low', 'low_date', 'end', 'end_date')} == \
        {k: after[k] for k in ('balance', 'low', 'low_date', 'end', 'end_date')}
    assert admin.api('GET', '/ledger').json()['rows'] == rows_before
    assert 'keep me' in admin.get('/h/%d/notes' % admin.hid).text


def test_import_example_bill_system_dry_run_and_merge(admin):
    from pathlib import Path
    data = (Path(__file__).parents[2] / 'examples' / 'bill-system.example.yaml').read_bytes()
    r = admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('x.yaml', data)},
                   data={'mode': 'replace', 'dry_run': '1'}, headers={'X-CSRF-Token': admin.csrf})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['dry_run'] and d['counts']['series'] > 3 and not d['is_backup'] and 'replaces' not in d
    assert d['rows']['counts']['added'] >= len(d['rows']['added']) == 200 and d['rows']['counts']['removed'] == 0
    assert d['forecast_before']['end'] == 100000 and {'name', 'due', 'amount', 'check'} <= set(d['rows']['added'][0])
    assert admin.api('GET', '/series').json()['series'] == []
    r = admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('x.yaml', data)},
                   data={'mode': 'merge', 'dry_run': '0'}, headers={'X-CSRF-Token': admin.csrf})
    assert r.status_code == 200, r.text
    assert len(admin.api('GET', '/series').json()['series']) == d['counts']['series']


def test_members(admin, app, tmp_path):
    r = admin.api('POST', '/members', {'username': 'partner', 'role': 'contributor', 'password': 'a long password'})
    uid = r.json()['user_id']
    again = {'username': 'partner', 'role': 'viewer', 'password': 'another one!'}
    assert admin.api('POST', '/members', again).status_code == 409
    assert admin.api('POST', '/members', {'username': 'ghost', 'role': 'viewer'}).status_code == 404
    assert admin.api('PUT', '/members/%d' % uid, {'role': 'editor'}).status_code == 200
    me = next(m for m in admin.api('GET', '/members').json()['members'] if m['username'] == 'admin')
    r = admin.api('PUT', '/members/%d' % me['user_id'], {'role': 'viewer'})
    assert r.status_code == 400 and r.json()['error'] == 'last_owner'
    assert admin.api('DELETE', '/members/%d' % me['user_id']).status_code == 400
    assert admin.api('DELETE', '/members/%d' % uid).status_code == 200
    log = admin.api('GET', '/audit').json()['entries']
    assert {'member.add', 'member.role', 'member.remove'} <= {e['action'] for e in log}
    existing = {'username': 'partner', 'role': 'viewer'}
    assert admin.api('POST', '/members', existing).status_code == 200      # the admin can add an existing user


def test_an_owner_cannot_pull_an_existing_user_into_their_household(admin, app, tmp_path):
    """D-098: only the admin adds someone who already has an account, and the answer does not say who exists."""
    add_user(tmp_path, admin.hid, 'boss2', 'owner')
    add_user(tmp_path, admin.hid, 'neighbour', None)            # an account on the server, in no household
    owner = login(app, 'boss2')
    owner.hid = admin.hid
    owner.csrf = page_csrf(owner, '/h/%d/members' % admin.hid)
    for name in ('neighbour', 'nobody-at-all'):
        r = owner.api('POST', '/members', {'username': name, 'role': 'owner'})
        assert r.status_code == 403 and r.json()['error'] == 'admin_only', r.text
    r = owner.api('POST', '/members', {'username': 'neighbour', 'role': 'viewer', 'password': 'a long password'})
    assert r.status_code == 409 and 'leave the password empty' not in r.json()['message'].lower()
    r = owner.api('POST', '/members', {'username': 'newbie', 'role': 'viewer', 'password': 'a long password'})
    assert r.status_code == 200                                     # creating a new user is still open to owners
    names = {m['username'] for m in owner.api('GET', '/members').json()['members']}
    assert 'neighbour' not in names and 'newbie' in names
    page = owner.get('/h/%d/members' % admin.hid).text
    assert 'already has an account' not in page
    assert 'already has an account' in admin.get('/h/%d/members' % admin.hid).text


def test_admin_api(admin):
    A = lambda m, p, b=None: admin.request(m, '/api/admin' + p, json=b, headers={'X-CSRF-Token': admin.csrf})  # noqa: E731
    r = A('POST', '/users', {'username': 'second', 'password': 'a long password'})
    uid = r.json()['id']
    assert A('POST', '/users', {'username': 'second', 'password': 'a long password'}).status_code == 400
    assert A('POST', '/users/%d/disabled' % uid, {'disabled': True}).status_code == 200
    assert A('POST', '/users/%d/password' % uid, {'password': 'short'}).status_code == 400
    assert A('POST', '/users/%d/password' % uid, {'password': 'reset password!'}).status_code == 200
    assert A('POST', '/users/%d/admin' % uid, {'is_admin': True}).status_code == 200
    me = next(u for u in A('GET', '/overview').json()['users'] if u['username'] == 'admin')
    assert A('POST', '/users/%d/disabled' % me['id'], {'disabled': True}).status_code == 400
    hid = A('POST', '/households', {'name': 'Second'}).json()['id']
    ov = A('GET', '/overview').json()
    assert any(h['id'] == hid and h['is_member'] for h in ov['households'])
    assert {'user.create', 'user.disable', 'password.reset', 'user.admin'} <= {a['action'] for a in ov['audit']}
    assert A('POST', '/households/9999/owner', {}).status_code == 404
    assert A('POST', '/users/9999/disabled', {'disabled': True}).status_code == 404


def test_bill_finder_bulk_change_all_or_nothing(admin, app, tmp_path):
    bill(admin, 'Electric', 15000, 6)
    bill(admin, 'Water', 4000, 9)
    rows = [r for r in admin.api('GET', '/ledger').json()['rows'] if r['name'] == 'Electric']
    assert len(rows) >= 3
    pick = [{'id': r['id'], 'expected_amount': r['amount']} for r in rows]
    r = admin.api('POST', '/rows/bulk', {'rows': pick, 'action': 'change', 'amount': 16500})
    assert r.status_code == 200 and r.json()['rows'] == len(rows)
    now = {x['id']: x for x in admin.api('GET', '/ledger').json()['rows']}
    assert all(now[r['id']]['amount'] == 16500 and now[r['id']]['amount_was'] == 15000 for r in rows)
    # one stale row: nothing is saved
    stale = [{**pick[0], 'expected_amount': 16500}, {**pick[1], 'expected_amount': 15000}]
    r = admin.api('POST', '/rows/bulk', {'rows': stale, 'action': 'remove'})
    assert r.status_code == 409 and 'Electric' in r.json()['message']
    assert {x['id']: x for x in admin.api('GET', '/ledger').json()['rows']}[rows[0]['id']]['state'] == 'open'
    fresh = [{'id': r['id'], 'expected_amount': 16500} for r in rows[:2]]
    assert admin.api('POST', '/rows/bulk', {'rows': fresh, 'action': 'status', 'status': 'autopay'}).status_code == 200
    now = {x['id']: x for x in admin.api('GET', '/ledger').json()['rows']}
    assert [now[r['id']]['status'] for r in rows[:3]] == ['autopay', 'autopay', 'none']
    assert admin.api('POST', '/rows/bulk', {'rows': fresh, 'action': 'remove'}).status_code == 200
    assert {x['id']: x for x in admin.api('GET', '/ledger').json()['rows']}[rows[0]['id']]['state'] == 'removed'
    for r in fresh:
        assert admin.api('POST', '/rows/%s/undo' % r['id'], {}).status_code == 200
    assert admin.api('POST', '/rows/bulk', {'rows': fresh, 'action': 'done'}).status_code == 200
    assert {x['id']: x for x in admin.api('GET', '/ledger').json()['rows']}[rows[0]['id']]['state'] == 'cleared'
    for r in fresh:
        assert admin.api('POST', '/rows/%s/undo' % r['id'], {}).status_code == 200
    assert admin.api('POST', '/rows/bulk', {'rows': fresh[:1] * 2, 'action': 'status'}).status_code == 400
    assert admin.api('POST', '/rows/bulk', {'rows': fresh, 'action': 'change'}).status_code == 400
    assert admin.api('POST', '/rows/bulk', {'rows': [{'id': 'nope', 'expected_amount': 1}],
                                            'action': 'status'}).status_code == 404
    # a contributor may mark statuses but not change amounts
    from webhelpers import add_user, login, page_csrf
    add_user(tmp_path, admin.hid, 'helper', 'contributor')
    h = login(app, 'helper')
    h.hid = admin.hid
    h.csrf = page_csrf(h, '/h/%d/bills' % admin.hid)
    one = [{'id': rows[2]['id'], 'expected_amount': 16500}]
    assert h.api('POST', '/rows/bulk', {'rows': one, 'action': 'status', 'status': 'manual'}).status_code == 200
    assert h.api('POST', '/rows/bulk', {'rows': one, 'action': 'change', 'amount': 1}).status_code == 403
    page = admin.get('/h/%d/bills' % admin.hid).text
    assert 'id="pageSearch"' in page and 'id="findCard"' in page


def test_recent_views(admin):
    assert admin.api('GET', '/flow?view=recent').json()['flow']['note']
    assert admin.api('GET', '/flow?view=nope').status_code == 400
    assert admin.api('GET', '/card?window=recent').status_code == 200
    assert admin.api('GET', '/checkup').json()['checkup']['tracked'] == []
    for page, marker in (('report', 'data-flow="recent"'), ('card', 'data-win="recent"'),
                         ('checkup', 'id="cuTracked"')):
        assert marker in admin.get('/h/%d/%s' % (admin.hid, page)).text


def test_debt_balance_history_and_burn_down_data(admin, app):
    body = {'name': 'Card A', 'kind': 'credit_card', 'balance': 300000, 'apr_bp': 2000, 'payment': 20000,
            'as_of': '2029-02-01'}
    aid = admin.api('POST', '/accounts', body).json()['id']
    assert admin.api('PUT', '/accounts/' + aid, {**body, 'balance': 285000, 'as_of': '2029-03-01'}).status_code == 200
    [a] = admin.api('GET', '/accounts').json()['accounts']
    assert a['history'] == [['2029-02-01', 300000], ['2029-03-01', 285000]]
    p = admin.api('POST', '/payoff', {'method': 'avalanche'}).json()
    assert p['result']['balances'][0] == 285000 and p['result']['balances'][-1] == 0
    assert len(p['baseline']['balances']) == p['baseline']['months'] + 1
    assert 'lows: false' in admin.get('/static/js/debts.js').text


def test_goals(admin):
    admin.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 300000, 'apr_bp': 2000,
                                    'payment': 20000})
    bill(admin, 'Rent', 100000, 5)
    r = admin.api('PUT', '/goals', {'goals': [{'name': 'Rainy day', 'kind': 'emergency', 'months': 3, 'saved': 150000},
                                              {'name': 'Debt-free', 'kind': 'debt_free'}]})
    assert r.status_code == 200, r.text
    g = {x['kind']: x for x in r.json()['goals']}
    assert g['emergency']['target'] == 3 * r.json()['monthly_bills'] and g['emergency']['pct'] > 0
    assert (g['debt_free']['target'], g['debt_free']['current']) == (300000, 0) and g['debt_free']['done_on']
    # the debt goal keeps what was owed when it was set; paying down shows as progress
    aid = admin.api('GET', '/accounts').json()['accounts'][0]['id']
    admin.api('PUT', '/accounts/' + aid, {'name': 'Card A', 'kind': 'credit_card', 'balance': 240000, 'apr_bp': 2000,
                                          'payment': 20000})
    keep = [{**x, 'target': None} for x in admin.api('GET', '/goals').json()['goals']]
    g = {x['kind']: x for x in admin.api('PUT', '/goals', {'goals': [
        {k: x[k] for k in ('id', 'name', 'kind', 'months', 'saved', 'by')} for x in keep]}).json()['goals']}
    assert (g['debt_free']['current'], g['debt_free']['pct']) == (60000, 20)
    assert admin.api('PUT', '/goals', {'goals': [{'name': 'Trip', 'kind': 'savings'}]}).status_code == 400
    assert 'id="goalList"' in admin.get('/h/%d/goals' % admin.hid).text
    assert 'id="goalsCard"' in admin.get('/h/%d/home' % admin.hid).text


def test_milestones_celebrated_once_and_can_be_hidden(admin):
    admin.api('PUT', '/goals', {'goals': [{'name': 'Trip', 'kind': 'savings', 'target': 1000, 'saved': 1000}]})
    w = admin.api('GET', '/milestones').json()
    assert w['celebrate'] is True and [x['text'] for x in w['new']] == ['Goal reached: Trip'] and w['history'] == []
    assert admin.api('POST', '/milestones/seen', {'keys': [x['key'] for x in w['new']]}).status_code == 200
    w = admin.api('GET', '/milestones').json()
    assert w['new'] == [] and [x['text'] for x in w['history']] == ['Goal reached: Trip']
    assert admin.api('PUT', '/celebrate', {'on': False}).status_code == 200
    assert admin.api('GET', '/milestones').json()['celebrate'] is False
    assert admin.api('GET', '/rules').json()['settings']['celebrate'] is False
    assert 'id="winsCard"' in admin.get('/h/%d/home' % admin.hid).text
    assert 'id="stWins"' in admin.get('/h/%d/settings' % admin.hid).text


def test_bill_and_row_notes(admin):
    """Notes on a recurring item survive a save from the page that leaves them out; a note on one row."""
    sid = bill(admin, 'Electric', 9000, 12, notes='account ending 1234')
    body = {'name': 'Electric', 'kind': 'bill', 'amount': 9500,
            'schedule': {'cadence': 'monthly', 'start': '2029-03-01', 'day': 12}}
    assert admin.api('PUT', '/series/%s' % sid, body).status_code == 200          # no notes sent: kept
    s = next(x for x in admin.api('GET', '/series').json()['series'] if x['id'] == sid)
    assert s['notes'] == 'account ending 1234'
    assert admin.api('PUT', '/series/%s' % sid, {**body, 'notes': ''}).status_code == 200
    assert next(x for x in admin.api('GET', '/series').json()['series'] if x['id'] == sid)['notes'] == ''
    led = admin.api('GET', '/ledger').json()
    rid = next(r['id'] for r in led['rows'] if r['series_id'] == sid)
    r = admin.api('POST', '/rows/%s/note' % rid, {'note': '  paid   by phone '})
    assert r.status_code == 200 and r.json() == {'id': rid, 'note': 'paid by phone'}
    assert next(x for x in admin.api('GET', '/ledger').json()['rows'] if x['id'] == rid)['note'] == 'paid by phone'
    assert admin.api('POST', '/rows/nope/note', {'note': 'x'}).status_code == 404
    assert admin.api('POST', '/rows/%s/note' % rid, {'note': 'x' * 301}).status_code == 400
    assert 'row.note' in [a['action'] for a in admin.api('GET', '/audit').json()['entries']]
