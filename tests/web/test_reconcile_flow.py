"""Upload -> preview -> accept -> apply on the synthetic fixture (hand-checked numbers in tests/core), plus uploads,
column mapping, card exports, workbook import, limits and headers."""
from __future__ import annotations

from pathlib import Path

from webhelpers import Client

FIX = Path(__file__).parents[1] / 'fixtures'


def upload(c: Client, name: str, data: bytes, kind: str = 'bank', **form: str):  # type: ignore[no-untyped-def]
    return c.post('/api/h/%d/uploads' % c.hid, files={'file': (name, data, 'text/csv')}, data={'kind': kind, **form},
                  headers={'X-CSRF-Token': c.csrf})


def import_synthetic(c: Client) -> None:
    data = (FIX / 'synthetic' / 'household.yaml').read_bytes()
    r = c.post('/api/h/%d/import/billsystem' % c.hid, files={'file': ('household.yaml', data)},
               data={'mode': 'replace', 'dry_run': '0'}, headers={'X-CSRF-Token': c.csrf})
    assert r.status_code == 200, r.text


def test_reconcile_end_to_end(admin, app):
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 3, 12)
    r = upload(admin, 'export.csv', (FIX / 'synthetic' / 'bank.csv').read_bytes())
    assert r.status_code == 200, r.text
    up = r.json()
    assert up['status'] == 'waiting' and up['latest_balance'] == 63500
    p = admin.api('POST', '/reconcile/preview', {'upload_id': up['id']}).json()
    assert p['basis']['description'] == 'CORNER CAFE' and p['latest_balance'] == 63500
    kinds = {x['id']: x['kind'] for x in p['proposals']}
    assert kinds == {'P1': 'tolerance', 'P2': 'card_bundle'}
    assert admin.api('POST', '/reconcile/apply', {'upload_id': up['id'], 'accept': {'P9': 'clear'}}).status_code == 400
    r = admin.api('POST', '/reconcile/apply', {'upload_id': up['id'], 'accept': {'P1': 'clear', 'P2': 'clear'}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['balance'] == 63500 and 'Lowest balance: **$595.00 on 03/14/2029**' in d['report_md']
    s = admin.api('GET', '/summary').json()
    assert (s['balance'], s['low'], s['low_date'], s['high'], s['end']) == (63500, 59500, '2029-03-14', 497500, 159500)
    assert admin.api('GET', '/uploads').json()['uploads'][0]['status'] == 'imported'
    assert admin.api('POST', '/reconcile/preview', {'upload_id': up['id']}).status_code == 409
    hist = admin.api('GET', '/reconciliations').json()['reconciliations']
    assert len(hist) == 1
    full = admin.api('GET', '/reconciliations/%s' % hist[0]['id']).json()
    assert full['report_md'] == d['report_md']
    assert admin.api('GET', '/reconciliations/nope').status_code == 404
    # the imported upload can only be hidden; its transactions stay in the history behind the result
    assert admin.api('DELETE', '/uploads/%s' % up['id']).json()['outcome'] == 'hidden'
    assert admin.api('GET', '/uploads').json()['uploads'] == []
    assert admin.api('GET', '/checkup').json()['checkup']['transactions'] > 0


def test_broken_chain_is_rejected_with_a_note(admin):
    import_synthetic(admin)
    lines = (FIX / 'synthetic' / 'bank.csv').read_text().splitlines()
    data = '\n'.join(lines[:3] + lines[4:]).encode()        # one posting missing
    up = upload(admin, 'gap.csv', data).json()
    r = admin.api('POST', '/reconcile/preview', {'upload_id': up['id']})
    assert r.status_code == 400 and r.json()['error'] == 'chain_break'
    u = admin.api('GET', '/uploads').json()['uploads'][0]
    assert u['status'] == 'rejected' and u['note']


def test_waiting_upload_can_be_removed(admin):
    import_synthetic(admin)
    up = upload(admin, 'export.csv', (FIX / 'synthetic' / 'bank.csv').read_bytes()).json()
    # #88: the confirmation says what goes; plain delete hides and keeps the postings in the history
    r = admin.api('GET', '/uploads/%s/removal' % up['id']).json()
    assert r['can_delete'] is True and r['bank_postings'] > 0 and r['first'] <= r['last']
    assert admin.api('DELETE', '/uploads/%s' % up['id']).json()['outcome'] == 'hidden'
    assert admin.api('GET', '/uploads').json()['uploads'] == []
    up = upload(admin, 'export.csv', (FIX / 'synthetic' / 'bank.csv').read_bytes()).json()
    assert admin.api('DELETE', '/uploads/%s?postings=true' % up['id']).json()['outcome'] == 'deleted'
    assert admin.api('DELETE', '/uploads/%s' % up['id']).status_code == 404
    assert admin.api('GET', '/uploads/%s/removal' % up['id']).status_code == 404


def test_unknown_layout_goes_through_column_mapping(admin):
    import_synthetic(admin)
    src = (FIX / 'synthetic' / 'bank.csv').read_text().replace('Date,Description,Amount,Balance',
                                                               'When,What,Value,Running')
    r = upload(admin, 'odd account 123456789012.csv', src.encode())
    d = r.json()
    assert d['needs_mapping'] and d['header'] == ['When', 'What', 'Value', 'Running']
    m = {'date_col': 'When', 'description_cols': ['What'], 'amount_col': 'Value', 'balance_col': 'Running'}
    r = admin.api('POST', '/uploads/%s/mapping' % d['id'], {'mapping': m, 'save_as': 'Odd bank'})
    assert r.status_code == 200, r.text
    u = r.json()
    assert u['rows'] == 7 and '123456789012' not in u['filename']
    assert any(p['name'] == 'Odd bank' for p in admin.api('GET', '/profiles').json()['profiles'])
    # the next file with that layout is detected
    again = upload(admin, 'next.csv', src.encode()).json()
    assert not again.get('needs_mapping')
    assert admin.api('POST', '/uploads/nope/mapping', {'mapping': m}).status_code == 400
    assert admin.api('POST', '/uploads/%s/mapping' % d['id'], {'mapping': {'nope': 1}}).status_code == 400


def test_card_export(admin):
    admin.api('PUT', '/card/categories', {'categories': [{'name': 'Grocery', 'keywords': ['MARKET']}]})
    data = b'Date,Description,Amount\n03/02/2029,CORNER MARKET,-40.00\n03/05/2029,PAYMENT THANK YOU,100.00\n'
    r = upload(admin, 'card.csv', data, kind='card')
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'imported'
    by = dict(admin.api('GET', '/card').json()['summary']['by_category'])
    assert by.get('Grocery') == 4000


def test_ofx_upload(admin):
    import_synthetic(admin)
    ofx = (b'OFXHEADER:100\nDATA:OFXSGML\n\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>'
           b'<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20290305<TRNAMT>-10.00<FITID>a1<NAME>SHOP</STMTTRN>'
           b'</BANKTRANLIST><LEDGERBAL><BALAMT>990.00<DTASOF>20290305</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>')
    r = admin.post('/api/h/%d/uploads' % admin.hid, files={'file': ('x.ofx', ofx)}, data={'kind': 'bank'},
                   headers={'X-CSRF-Token': admin.csrf})
    assert r.status_code == 200, r.text
    assert r.json()['rows'] == 1


def test_upload_limits_and_types(admin, app):
    big = b'Date,Description,Amount\n' + b'03/02/2029,X,-1.00\n' * 400_000
    r = upload(admin, 'big.csv', big)
    assert r.status_code == 413
    assert upload(admin, 'x.exe', b'MZ').status_code == 415
    assert upload(admin, 'x.csv', b'PK\x03\x04zip').status_code == 415
    assert upload(admin, 'x.ofx', b'random text').status_code == 415
    assert upload(admin, 'x.csv', b'').status_code == 400
    assert upload(admin, 'x.csv', b'\xff\xfe\x00garbage\x00\x01').status_code == 400
    r = admin.request('PUT', '/api/h/%d/notes' % admin.hid, content=b'{"notes": "' + b'a' * (1024 * 1024 + 10) + b'"}',
                      headers={'X-CSRF-Token': admin.csrf, 'Content-Type': 'application/json'})
    assert r.status_code == 413


def test_workbook_import_with_mapping(admin):
    data = (FIX / 'workbook' / 'checkbook.xlsx').read_bytes()
    r = admin.post('/api/h/%d/import/workbook/preview' % admin.hid, files={'file': ('checkbook.xlsx', data)},
                   headers={'X-CSRF-Token': admin.csrf})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p['token'] and p['columns'] and p['guess']['name_col']
    body = {'token': p['token'], 'mapping': p['guess'], 'save_profile': 'Mine'}
    r = admin.api('POST', '/import/workbook/commit', body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['counts']['rows'] > 5
    if d['stored_check']:
        assert all(d['stored_check'].values()), d['stored_check']
    assert admin.api('POST', '/import/workbook/commit', {'token': 'gone', 'mapping': p['guess']}).status_code == 400


def test_security_headers_and_health(admin, app):
    r = admin.get('/h/%d/bills' % admin.hid)
    h = r.headers
    csp = h['content-security-policy']
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert h['x-content-type-options'] == 'nosniff' and h['x-frame-options'] == 'DENY'
    assert h['referrer-policy'] == 'same-origin' and h['cache-control'] == 'no-store'
    assert 'strict-transport-security' not in h
    assert 'style=' not in r.text and '<script>' not in r.text      # no inline styles or scripts (CSP)
    assert Client(app).get('/healthz').json() == {'status': 'ok'}
    assert Client(app).get('/static/js/api.js').status_code == 200
    bad = Client(app, base_url='http://evil.example')
    assert bad.get('/healthz').status_code == 400       # TrustedHost blocks DNS rebinding
    assert Client(app).get('/api/h/%d/summary' % admin.hid).status_code == 401
