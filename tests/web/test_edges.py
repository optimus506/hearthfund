"""Error paths, review fixes and the server-side report totals."""
from __future__ import annotations

import logging
from datetime import date

from test_reconcile_flow import FIX, import_synthetic, upload

from hbudget.web.app import create_app
from webhelpers import PASSWORD, Client, csrf_from, login, make_settings, page_csrf


def test_chunked_body_over_the_limit_is_413(admin):
    def chunks():  # no Content-Length: sent chunked
        yield b'{"notes": "'
        for _ in range(140):
            yield b'a' * 8192
        yield b'"}'
    r = admin.request('PUT', '/api/h/%d/notes' % admin.hid, content=chunks(),
                      headers={'X-CSRF-Token': admin.csrf, 'Content-Type': 'application/json'})
    assert r.status_code == 413


def test_bad_content_length_and_form_limit(admin, app):
    r = admin.request('PUT', '/api/h/%d/notes' % admin.hid, content=b'{}',
                      headers={'X-CSRF-Token': admin.csrf, 'Content-Type': 'application/json', 'Content-Length': 'x'})
    assert r.status_code == 400
    c = Client(app)
    page = c.get('/login')
    big = 'csrf_token=%s&username=%s' % (csrf_from(page.text), 'a' * (70 * 1024))
    r = c.post('/login', content=big.encode(), headers={'Content-Type': 'application/x-www-form-urlencoded'})
    assert r.status_code == 413


def test_referer_fallback_and_bad_cookie(admin, app):
    url = '/api/h/%d/notes' % admin.hid
    r = admin.request('PUT', url, json={'notes': 'x'}, headers={'X-CSRF-Token': admin.csrf,
                                                               'Referer': 'http://evil.example/page'})
    assert r.status_code == 403
    r = admin.request('PUT', url, json={'notes': 'x'}, headers={'X-CSRF-Token': admin.csrf,
                                                               'Referer': 'http://testserver/h/1/notes'})
    assert r.status_code == 200
    c = Client(app)
    r = c.get('/api/h/%d/summary' % admin.hid, headers={'Cookie': 'hb_session="unterminated; ;;=='})
    assert r.status_code == 401


def test_500_is_generic_and_logs_no_details(tmp_path, caplog):
    a = create_app(make_settings(tmp_path))
    a.state.stop.set()

    @a.get('/boom')
    def boom() -> None:
        raise RuntimeError('ELECTRIC CO 4000 1234 5678 9010')
    with caplog.at_level(logging.ERROR, logger='hbudget'):
        r = Client(a, raise_server_exceptions=False).get('/boom', headers={'Accept': 'application/json'})
    assert r.status_code == 500 and r.json() == {'error': 'server', 'message': 'Something went wrong on the server.'}
    assert 'ELECTRIC' not in caplog.text and 'RuntimeError' in caplog.text


def test_serve_builds_uvicorn_arguments(monkeypatch, tmp_path):
    from hbudget import cli
    seen: dict[str, object] = {}
    monkeypatch.setattr('uvicorn.run', lambda app, **kw: seen.update(kw))
    monkeypatch.setenv('HBUDGET_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HBUDGET_TRUST_PROXY', '1')
    monkeypatch.setenv('HBUDGET_FORWARDED_ALLOW_IPS', '10.0.0.5')
    assert cli.main(['serve', '--port', '9123']) == 0
    assert seen['port'] == 9123 and seen['host'] == '127.0.0.1' and seen['proxy_headers'] is True
    assert seen['forwarded_allow_ips'] == '10.0.0.5' and seen['server_header'] is False
    monkeypatch.setenv('HBUDGET_TRUST_PROXY', '0')
    cli.main(['serve'])
    assert seen['proxy_headers'] is False and seen['forwarded_allow_ips'] == '127.0.0.1'
    assert seen['ssl_certfile'] is None and seen['ssl_keyfile'] is None
    monkeypatch.setattr('hbudget.web.config.Settings.check_tls_files', lambda self: None)
    monkeypatch.setenv('HBUDGET_TLS_CERT', '/certs/fullchain.pem')
    monkeypatch.setenv('HBUDGET_TLS_KEY', '/certs/privkey.pem')
    assert cli.main(['serve']) == 0
    assert (seen['ssl_certfile'], seen['ssl_keyfile']) == ('/certs/fullchain.pem', '/certs/privkey.pem')
    monkeypatch.setenv('HBUDGET_DEV', '1')
    assert cli.main(['serve', '--host', '0.0.0.0']) == 2   # refused, with a one-line error


def test_untrusted_forwarded_for_is_ignored(admin, app):
    # without a trusted proxy, X-Forwarded-For does not change the client address the throttle counts
    c = Client(app)
    page = c.get('/login')
    tok = csrf_from(page.text)
    for i in range(5):
        c.post('/login', data={'csrf_token': tok, 'username': 'admin', 'password': 'bad password!'},
               headers={'X-Forwarded-For': '203.0.113.%d' % i})
    r = c.post('/login', data={'csrf_token': tok, 'username': 'admin', 'password': 'bad password!'},
               headers={'X-Forwarded-For': '198.51.100.1'})
    assert r.status_code == 429


def test_preview_tokens_belong_to_one_session_and_household(admin, app, tmp_path):
    data = (FIX / 'workbook' / 'checkbook.xlsx').read_bytes()
    p = admin.post('/api/h/%d/import/workbook/preview' % admin.hid, files={'file': ('c.xlsx', data)},
                   headers={'X-CSRF-Token': admin.csrf}).json()
    other = admin.api('POST', '/members', {'username': 'owner2', 'role': 'owner', 'password': 'a long password'})
    assert other.status_code == 200
    o2 = login(app, 'owner2', 'a long password')
    o2.hid, o2.csrf = admin.hid, page_csrf(o2, '/h/%d/bills' % admin.hid)
    r = o2.api('POST', '/import/workbook/commit', {'token': p['token'], 'mapping': p['guess']})
    assert r.status_code == 400 and r.json()['error'] == 'expired'
    src = (FIX / 'synthetic' / 'bank.csv').read_text().replace('Date,Description,Amount,Balance', 'W,X,Y,Z')
    up = upload(admin, 'odd.csv', src.encode()).json()
    r = o2.api('POST', '/uploads/%s/mapping' % up['id'], {'mapping': {'date_col': 'W', 'amount_col': 'Y'}})
    assert r.status_code == 400 and r.json()['error'] == 'expired'


def preview(c: Client):  # type: ignore[no-untyped-def]
    import_synthetic(c)
    c.app.state.clock = lambda: date(2029, 3, 12)
    up = upload(c, 'export.csv', (FIX / 'synthetic' / 'bank.csv').read_bytes()).json()
    return up, c.api('POST', '/reconcile/preview', {'upload_id': up['id']}).json()


def test_apply_refuses_a_stale_fingerprint_and_bad_choices(admin):
    up, p = preview(admin)
    assert p['fingerprint']
    r = admin.api('POST', '/reconcile/apply', {'upload_id': up['id'], 'accept': {'P2': 'reduce'},
                                               'fingerprint': p['fingerprint']})
    assert r.status_code == 400                       # a card bundle cannot be a partial payment
    admin.api('PUT', '/notes', {'notes': 'unrelated'})  # notes do not change the budget version
    rid = admin.api('GET', '/ledger').json()['rows'][-1]['id']
    admin.api('POST', '/rows/%s/status' % rid, {'status': 'manual'})   # but this edit does
    r = admin.api('POST', '/reconcile/apply', {'upload_id': up['id'], 'accept': {'P1': 'clear'},
                                               'fingerprint': p['fingerprint']})
    assert r.status_code == 409
    p2 = admin.api('POST', '/reconcile/preview', {'upload_id': up['id']}).json()
    r = admin.api('POST', '/reconcile/apply', {'upload_id': up['id'], 'accept': {'P1': 'clear'},
                                               'fingerprint': p2['fingerprint']})
    assert r.status_code == 200


def test_report_totals_come_from_the_server(admin):
    up, _ = preview(admin)
    admin.api('POST', '/reconcile/apply', {'upload_id': up['id'], 'accept': {'P1': 'clear', 'P2': 'clear'}})
    for view in ('actual', 'planned'):
        f = admin.api('GET', '/flow?view=' + view).json()['flow']
        t = f['totals']
        assert t['money_in'] == sum(s['amount'] for s in f['sources'])
        assert t['money_out'] == sum(g['total'] for g in f['groups'])
        assert not any(g['name'] == 'Left over' and not g['items'] for g in f['groups'])
        assert t['left_over'] - t['short'] == t['money_in'] - t['money_out']
        assert t['per_month']['money_in'] == t['money_in'] // f['months']
    planned = admin.api('GET', '/flow?view=planned').json()['flow']
    assert planned['totals']['money_in'] > 0 and planned['groups']


def test_budget_dates_in_the_future_start_the_window_there(admin):
    # the household starts 2029-03-01 but the clock says 2028-01-01: "today" becomes the as-of date
    admin.app.state.clock = lambda: date(2028, 1, 1)
    d = admin.api('GET', '/ledger').json()
    assert d['today'] == '2029-03-01'
    admin.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 10000, 'payment': 5000})
    assert admin.api('POST', '/payoff', {}).json()['start'] == '2029-04-01'


def test_series_kind_cannot_change_and_rule_targets_can_be_row_names(admin):
    body = {'name': 'Electric', 'kind': 'bill', 'amount': 15000,
            'schedule': {'cadence': 'monthly', 'start': '2029-03-01', 'day': 6}}
    sid = admin.api('POST', '/series', body).json()['id']
    r = admin.api('PUT', '/series/%s' % sid, dict(body, kind='income'))
    assert r.status_code == 400 and 'cannot change' in r.json()['message']
    admin.api('POST', '/rows', {'name': 'Dentist', 'kind': 'bill', 'amount': 9000, 'due': '2029-03-20'})
    assert 'Dentist' in admin.api('GET', '/rules').json()['row_names']
    r = admin.api('POST', '/rules', {'name': 'Dentist', 'kind': 'bill', 'contains': ['DENTAL'], 'targets': ['Dentist']})
    assert r.status_code == 200


def test_profiles_by_kind(admin):
    src = (FIX / 'synthetic' / 'bank.csv').read_text().replace('Date,Description,Amount,Balance', 'W,X,Y,Z')
    up = upload(admin, 'odd.csv', src.encode()).json()
    admin.api('POST', '/uploads/%s/mapping' % up['id'], {'mapping': {'date_col': 'W', 'description_cols': ['X'],
                                                                     'amount_col': 'Y'}, 'save_as': 'Odd'})
    bank = admin.api('GET', '/profiles?kind=bank').json()['profiles']
    card = admin.api('GET', '/profiles?kind=card').json()['profiles']
    assert any(p['name'] == 'Odd' for p in bank) and not any(p['name'] == 'Odd' for p in card)
    assert admin.api('GET', '/profiles?kind=other').status_code == 400
    pid = next(p['id'] for p in bank if p['name'] == 'Odd')
    r = upload(admin, 'card.csv', src.encode(), kind='card', profile_id=pid)
    assert r.status_code == 400


def test_card_resort_keeps_a_bank_category_match(admin):
    admin.api('PUT', '/card/categories', {'categories': [{'name': 'Dining', 'keywords': ['BISTRO'],
                                                          'bank_categories': ['Restaurants']}]})
    data = (b'Posted,Text,Value,Category\n03/02/2029,CORNER PLACE,-40.00,Restaurants\n'
            b'03/03/2029,BISTRO TWO,-20.00,\n')
    r = admin.post('/api/h/%d/uploads' % admin.hid, files={'file': ('card.csv', data)}, data={'kind': 'card'},
                   headers={'X-CSRF-Token': admin.csrf})
    assert r.json()['needs_mapping']
    r = admin.api('POST', '/uploads/%s/mapping' % r.json()['id'], {'mapping': {
        'date_col': 'Posted', 'description_cols': ['Text'], 'amount_col': 'Value', 'category_col': 'Category'}})
    assert r.status_code == 200, r.text
    before = dict(admin.api('GET', '/card').json()['summary']['by_category'])
    assert before.get('Dining') == 6000
    admin.api('PUT', '/card/categories', {'categories': [{'name': 'Dining', 'keywords': ['BISTRO'],
                                                          'bank_categories': ['Restaurants']}]})
    assert dict(admin.api('GET', '/card').json()['summary']['by_category']).get('Dining') == 6000


def test_password_change_is_throttled(admin):
    for _ in range(5):
        admin.post('/account/password', data={'csrf_token': admin.csrf, 'current': 'wrong password',
                                              'new': 'another long password'})
    r = admin.post('/account/password', data={'csrf_token': admin.csrf, 'current': PASSWORD,
                                              'new': 'another long password'})
    assert r.status_code == 429 and 'Too many attempts' in r.text and int(r.headers['retry-after']) >= 1


def test_imports_keep_the_bank_history(admin):
    import_synthetic(admin)
    upload(admin, 'export.csv', (FIX / 'synthetic' / 'bank.csv').read_bytes())
    data = (FIX / 'workbook' / 'checkbook.xlsx').read_bytes()
    p = admin.post('/api/h/%d/import/workbook/preview' % admin.hid, files={'file': ('c.xlsx', data)},
                   headers={'X-CSRF-Token': admin.csrf}).json()
    assert admin.api('POST', '/import/workbook/commit', {'token': p['token'], 'mapping': p['guess']}).status_code == 200
    assert len(admin.api('GET', '/uploads').json()['uploads']) == 1
    yaml = (FIX / 'synthetic' / 'household.yaml').read_bytes()
    r = admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('h.yaml', yaml)},
                   data={'mode': 'replace', 'dry_run': '0'}, headers={'X-CSRF-Token': admin.csrf})
    assert r.status_code == 200          # a plain bill system needs no confirmation and keeps history
    assert len(admin.api('GET', '/uploads').json()['uploads']) == 1


def test_backup_dry_run_counts_the_history_it_would_replace(admin):
    import_synthetic(admin)
    upload(admin, 'export.csv', (FIX / 'synthetic' / 'bank.csv').read_bytes())
    backup = admin.get('/api/h/%d/export/backup.json' % admin.hid).content
    r = admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('b.json', backup)},
                   data={'mode': 'replace', 'dry_run': '1'}, headers={'X-CSRF-Token': admin.csrf})
    d = r.json()
    assert d['is_backup'] and d['replaces']['uploads'] == 1 and d['replaces']['bank_txns'] == 7
    assert d['replaces']['rows'] > 0
