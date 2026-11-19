"""Choosing the rows a bank line paid (D-107): the page's API, the token API and the MCP tool."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_api_v1 import call, make_token, post
from test_reconcile_flow import FIX, import_synthetic, upload


@pytest.fixture
def anon(app) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(app, base_url='http://testserver')


def start(admin, app):  # type: ignore[no-untyped-def]
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 3, 12)
    up = upload(admin, 'export.csv', (FIX / 'synthetic' / 'bank.csv').read_bytes()).json()
    return up, admin.api('POST', '/reconcile/preview', {'upload_id': up['id']}).json()


def test_choose_rows_apply_and_undo(admin, app):
    up, p = start(admin, app)
    card = next(x for x in p['proposals'] if x['kind'] == 'card_bundle')
    line, own = card['txn'], [r['id'] for r in card['rows']]
    # a money-out line offers the open rows it may have paid, its proposal's own rows first
    assert line['amount'] < 0 and [r['id'] for r in card['candidates']][:len(own)] and set(own) <= {
        r['id'] for r in card['candidates']}
    assert all(r['amount'] > 0 and r['kind'] != 'income' for r in card['candidates'])
    body = {'upload_id': up['id'], 'fingerprint': p['fingerprint']}
    # refused: with its proposal accepted too, more than the payment, a line that is not waiting
    r = admin.api('POST', '/reconcile/apply', {**body, 'accept': {card['id']: 'clear'}, 'assign': {line['id']: own}})
    assert r.status_code == 400 and 'chose rows for' in r.json()['message']
    big = [r['id'] for r in card['candidates']]
    if sum(r['amount'] for r in card['candidates']) > -line['amount']:
        r = admin.api('POST', '/reconcile/apply', {**body, 'assign': {line['id']: big}})
        assert r.status_code == 400 and 'more than the payment' in r.json()['message']
    assert admin.api('POST', '/reconcile/apply', {**body, 'assign': {'nope': own}}).status_code == 400
    assert admin.api('POST', '/reconcile/apply', {**body, 'assign': {line['id']: {'rows': own}}}).status_code == 400
    # one row of the proposal, chosen by hand: cleared by that line, the rest of the payment stays unbudgeted
    first = card['rows'][0]
    r = admin.api('POST', '/reconcile/apply', {**body, 'assign': {line['id']: {'clear': [first['id']]}}})
    assert r.status_code == 200, r.text
    assert '| chosen |' in r.json()['report_md'] and 'the rest paid the rows you chose' in r.json()['report_md']
    rows = {x['id']: x for x in admin.api('GET', '/ledger?start=2029-01-01').json()['rows']}
    assert first['id'] not in rows                       # the ledger lists open rows
    rec = admin.api('GET', '/reconciliations').json()['reconciliations'][0]
    # undo reopens the chosen row with everything else
    assert admin.api('POST', '/reconciliations/%s/undo' % rec['id'], {}).status_code == 200
    rows = {x['id']: x for x in admin.api('GET', '/ledger?start=2029-01-01').json()['rows']}
    assert first['id'] in rows


def test_choose_rows_with_a_token_and_mcp(admin, anon, app):
    up, _ = start(admin, app)
    t = make_token(admin, 'write')
    p = post(anon, '/uploads/%s/preview' % up['id'], t, None).json()
    card = next(x for x in p['proposals'] if x['kind'] == 'card_bundle')
    assert card['candidates'] and all(r['amount'] >= 0 and r['direction'] == 'out' for r in card['candidates'])
    pay, first = card['txn']['amount'], card['rows'][0]
    assert first['amount'] < pay
    # the token API: a plain list of row ids clears them; here the fingerprint is stale on purpose first
    r = post(anon, '/uploads/%s/apply' % up['id'], t, {'fingerprint': 'x' * 32,
                                                      'assign': {card['txn']['id']: [first['id']]}})
    assert r.status_code == 409
    # MCP: the first row in full, and a larger row keeps the rest when the payment does not cover it whole
    others = [r for r in card['candidates'] if r['id'] != first['id'] and r['amount'] > pay - first['amount']]
    choice = {'clear': [first['id']], **({'reduce': others[0]['id']} if others else {})}
    res = call(anon, t, 'reconcile_apply', {'upload_id': up['id'], 'fingerprint': p['fingerprint'],
                                            'assign': {card['txn']['id']: choice}})['result']
    assert res['isError'] is False, res
    det = call(anon, t, 'get_reconciliation', {'rec_id': res['structuredContent']['id']})['result']
    det = det['structuredContent']['details']
    assert [x['row'] for x in det['assigned'][0]['rows']] == [first['id']]
    assert (det['assigned'][0]['reduced'] or {}).get('row') == (others[0]['id'] if others else None)
    assert next(x for x in det['proposals'] if x['id'] == card['id'])['decision'] == 'chose rows'
    rows = {x['id']: x for x in admin.api('GET', '/ledger?start=2029-01-01').json()['rows']}
    assert first['id'] not in rows                       # the ledger lists open rows
    if others:
        assert rows[others[0]['id']]['amount'] == others[0]['amount'] - (pay - first['amount'])
