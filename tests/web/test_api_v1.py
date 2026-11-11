"""Access tokens, /api/v1 and /mcp (D-068): who may do what, CSRF, rate limits, audit, and the MCP protocol."""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_api import bill, pay

from hbudget.web.routes import api_v1
from webhelpers import Client, add_user, login


def make_token(c: Client, scope: str = 'read', name: str = 'Agent', **kw: Any) -> str:
    r = c.api('POST', '/tokens', {'name': name, 'scope': scope, **kw})
    assert r.status_code == 200, r.text
    return str(r.json()['secret'])


@pytest.fixture
def anon(app) -> TestClient:  # type: ignore[no-untyped-def]
    """No cookies at all, like a script or an agent."""
    return TestClient(app, base_url='http://testserver')


def get(client: TestClient, path: str, token: str | None, **kw: Any) -> Any:
    h = {'Authorization': 'Bearer ' + token} if token else {}
    return client.get('/api/v1' + path, headers=h, **kw)


def post(client: TestClient, path: str, token: str | None, body: Any) -> Any:
    h = {'Authorization': 'Bearer ' + token} if token else {}
    return client.post('/api/v1' + path, headers=h, json=body)


def rpc(client: TestClient, token: str | None, method: str, params: Any = None, mid: Any = 1, **headers: str) -> Any:
    h = {'Authorization': 'Bearer ' + token} if token else {}
    msg: dict[str, Any] = {'jsonrpc': '2.0', 'method': method}
    if mid is not None:
        msg['id'] = mid
    if params is not None:
        msg['params'] = params
    return client.post('/mcp', headers={**h, **headers}, json=msg)


def seed(admin: Client) -> None:
    pay(admin, 300000, 15)
    bill(admin, 'Electric', 15000, 6)
    bill(admin, 'Rent', 100000, 1)


# ---------------------------------------------------------------- tokens and the owner page

def test_owner_manages_tokens(admin):
    secret = make_token(admin, 'write', days=30)
    assert secret.startswith('hbt_')
    toks = admin.api('GET', '/tokens').json()['tokens']
    assert len(toks) == 1 and toks[0]['hint'] == secret[-4:] and 'secret' not in toks[0] and toks[0]['expires_at']
    assert secret not in admin.api('GET', '/tokens').text            # never listed again
    assert admin.api('POST', '/tokens', {'name': '', 'scope': 'read'}).status_code == 400
    assert admin.api('POST', '/tokens', {'name': 'x', 'scope': 'owner'}).status_code == 400
    page = admin.get('/h/%d/access' % admin.hid)
    assert page.status_code == 200 and '/js/access.js"' in page.text
    assert '"base_url": "http://testserver"' in page.text
    assert admin.api('DELETE', '/tokens/%d' % toks[0]['id']).status_code == 200
    assert admin.api('DELETE', '/tokens/%d' % toks[0]['id']).status_code == 404
    actions = [e['action'] for e in admin.api('GET', '/audit').json()['entries']]
    assert actions[:2] == ['token.revoke', 'token.create']


def test_only_owners_manage_tokens(admin, app, tmp_path):
    add_user(tmp_path, admin.hid, 'ed1', 'editor')
    ed = login(app, 'ed1')
    ed.hid, ed.csrf = admin.hid, admin.csrf
    from webhelpers import page_csrf
    ed.csrf = page_csrf(ed, '/h/%d/bills' % admin.hid)
    assert ed.api('GET', '/tokens').status_code == 403
    assert ed.api('POST', '/tokens', {'name': 'x', 'scope': 'read'}).status_code == 403
    assert ed.get('/h/%d/access' % admin.hid).status_code == 403


# ---------------------------------------------------------------- /api/v1

def test_no_token_bad_token_revoked_token(admin, anon):
    for t in (None, 'hbt_nope', 'Basic abc'):
        r = get(anon, '/summary', t)
        assert r.status_code == 401 and r.headers['www-authenticate'] == 'Bearer' and r.json()['error'] == 'auth'
    secret = make_token(admin)
    assert get(anon, '/summary', secret).status_code == 200
    tid = admin.api('GET', '/tokens').json()['tokens'][0]['id']
    admin.api('DELETE', '/tokens/%d' % tid)
    assert get(anon, '/summary', secret).status_code == 401


def test_session_cookie_is_not_enough(admin):
    """A signed-in browser cannot use /api/v1: only the bearer token counts."""
    assert admin.get('/api/v1/summary').status_code == 401
    r = admin.post('/api/v1/expected', json={'label': 'x', 'amount': 1, 'direction': 'out', 'date': '2029-03-09'},
                   headers={'X-CSRF-Token': admin.csrf, 'Origin': 'http://testserver'})
    assert r.status_code == 401


def test_token_routes_skip_csrf_but_not_the_origin_check(admin, anon):
    secret = make_token(admin, 'write')
    body = {'label': 'Refund', 'amount': 2000, 'direction': 'in', 'date': '2029-03-09'}
    assert post(anon, '/expected', secret, body).status_code == 200
    r = anon.post('/api/v1/expected', json=body, headers={'Authorization': 'Bearer ' + secret,
                                                         'Origin': 'https://evil.example'})
    assert r.status_code == 403
    # without a bearer token: the route's own 401, which tells a client to authenticate
    assert anon.post('/api/v1/expected', json=body).status_code == 401


def test_read_endpoints(admin, anon):
    seed(admin)
    t = make_token(admin)
    me = get(anon, '/me', t).json()
    assert me['household'] == {'id': admin.hid, 'name': 'Test household'} and me['role'] == 'viewer'
    assert me['token']['scope'] == 'read' and me['today'] == '2029-03-01'
    assert get(anon, '/goals', t).json()['goals'] == []
    assert tool(anon, t, 'get_goals')['goals'] == []
    s = get(anon, '/summary', t).json()
    assert {'balance', 'low', 'low_date', 'alerts', 'unscheduled'} <= set(s)
    b = get(anon, '/bills', t, params={'days': 10}).json()
    assert [x['name'] for x in b['bills']] == ['Rent', 'Electric'] and b['through'] == '2029-03-11'
    assert set(b['bills'][0]) == {'id', 'name', 'kind', 'amount', 'direction', 'due', 'status', 'late', 'card',
                                  'balance_after', 'note'}
    assert get(anon, '/bills', t, params={'days': 0}).status_code == 400
    f = get(anon, '/forecast', t, params={'days': 20}).json()
    assert all(p['date'] <= '2029-03-21' for p in f['points'])
    assert f['horizon_low'] == s['low'] and f['low'] == min([f['start']] + [p['balance'] for p in f['points']])
    p = get(anon, '/paydays', t).json()
    assert p['periods'][0]['end'] == '2029-03-15'
    assert [x['name'] for x in p['periods'][0]['bills']] == ['Rent', 'Electric']
    w = post(anon, '/what-if', t, {'items': [{'label': 'TV', 'amount': 900000, 'direction': 'out',
                                              'date': '2029-03-05'}]}).json()
    assert w['verdict'] == 'no' and w['after']['low'] < w['before']['low']
    assert get(anon, '/expected', t).json() == {'items': []}
    assert get(anon, '/debts', t).json() == {'debts': [], 'stale_after_days': 35}
    assert 'debt_free_on' in post(anon, '/debts/payoff', t, {'method': 'snowball'}).json()
    assert 'flow' in get(anon, '/report', t, params={'view': 'planned'}).json()
    assert 'checkup' in get(anon, '/checkup', t).json()


def test_agent_reports(admin, anon):
    """The problems an agent found with 0.2.0 (issues #10 to #18)."""
    pid = pay(admin, 300000, 15)
    bill(admin, 'Rent', 100000, 1)
    card = admin.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 50000,
                                           'payment': 5000, 'promo_ends': '2029-06-01', 'apr_after_bp': 2964,
                                           'deferred_interest': True}).json()['id']
    admin.api('POST', '/accounts', {'name': 'Checking A', 'kind': 'checking', 'balance': 0})
    bill(admin, 'Streaming', 1500, 3, card=card)
    assert admin.api('POST', '/rows', {'name': 'Old bill', 'kind': 'bill', 'amount': 2500,
                                       'due': '2029-02-20'}).status_code == 200
    t = make_token(admin)

    # #11 and #10: only debts, with the promo's APR after and the deferred-interest flag
    d = get(anon, '/debts', t).json()['debts']
    assert [x['name'] for x in d] == ['Card A']
    assert d[0]['apr_after_bp'] == 2964 and d[0]['deferred_interest'] is True
    # #12: the promo verdicts and the debts the plan warns about
    p = post(anon, '/debts/payoff', t, {}).json()
    assert p['promos'][0]['name'] == 'Card A' and p['promos'][0]['deferred_interest'] is True
    assert 'monthly_needed' in p['promos'][0] and p['not_covering'] == [] and p['excluded'] == []
    # #13: dates never go back; the late row counts on the first date
    f = get(anon, '/forecast', t, params={'days': 400}).json()
    dates = [x['date'] for x in f['points']]
    assert dates == sorted(dates) and dates[0] == '2029-03-01'
    # #14: low is the window's; horizon_low the whole forecast's
    f10 = get(anon, '/forecast', t, params={'days': 10}).json()
    assert f10['low_date'] <= f10['through'] and f10['horizon_end'] > f10['through']
    # #15: the whole horizon can be read
    end = get(anon, '/summary', t).json()['end_date']
    assert get(anon, '/forecast', t, params={'days': api_v1.MAX_DAYS}).json()['points'][-1]['date'] == end
    assert get(anon, '/bills', t, params={'days': api_v1.MAX_DAYS + 1}).status_code == 400
    # #16: amounts are positive with a direction
    rows = {x['name']: x for x in get(anon, '/bills', t, params={'days': 20}).json()['bills']}
    assert (rows['Pay A']['amount'], rows['Pay A']['direction']) == (300000, 'in')
    assert (rows['Rent']['amount'], rows['Rent']['direction']) == (100000, 'out')
    assert rows['Streaming']['card'] == card and rows['Old bill']['late'] is True
    assert pid
    # #17: a what-if before today is refused
    r = post(anon, '/what-if', t, {'items': [{'amount': 500, 'direction': 'out', 'date': '2029-02-01'}]})
    assert r.status_code == 400 and 'before today' in r.json()['message']
    assert post(anon, '/what-if', t, {'items': [{'amount': 500, 'direction': 'out',
                                                 'date': '2029-03-01'}]}).status_code == 200
    # #18: a bill paid by card is not "not scheduled"
    s = get(anon, '/summary', t).json()
    flagged = [a['row_id'] for a in s['alerts'] if a['code'] == 'unscheduled']
    assert flagged and not any(x.startswith(rows['Streaming']['id'].split(':')[0]) for x in flagged)


def test_read_token_cannot_write(admin, anon):
    seed(admin)
    t = make_token(admin, 'read')
    rid = get(anon, '/bills', t).json()['bills'][0]['id']
    r = post(anon, '/bills/%s/status' % rid, t, {'status': 'manual'})
    assert r.status_code == 403 and r.json()['message'] == 'This token is read-only.'
    r = post(anon, '/expected', t, {'label': 'x', 'amount': 1, 'direction': 'out', 'date': '2029-03-09'})
    assert r.status_code == 403


def test_write_token_writes_and_the_audit_names_it(admin, anon):
    seed(admin)
    t = make_token(admin, 'write', name='Laptop Claude')
    rid = get(anon, '/bills', t).json()['bills'][0]['id']
    assert post(anon, '/bills/%s/status' % rid, t, {'status': 'manual'}).json() == {'ok': True}
    assert get(anon, '/bills', t).json()['bills'][0]['status'] == 'manual'
    eid = post(anon, '/expected', t, {'label': 'Refund', 'amount': 2000, 'direction': 'in',
                                      'date': '2029-03-09'}).json()['id']
    assert post(anon, '/expected/%s/status' % eid, t, {'status': 'done'}).json() == {'ok': True}
    assert post(anon, '/bills/nope/status', t, {'status': 'manual'}).status_code == 404
    entries = admin.api('GET', '/audit').json()['entries']
    by_token = [(e['action'], e['user'], e['via']) for e in entries if e['via']]
    assert by_token == [('expected.status', 'admin', 'token: Laptop Claude'),
                        ('expected.add', 'admin', 'token: Laptop Claude'),
                        ('row.status', 'admin', 'token: Laptop Claude')]


def test_token_never_exceeds_its_creators_role(admin, app, tmp_path, anon):
    """A write token made by an owner who is later made a viewer can only read; removing them ends it."""
    seed(admin)
    add_user(tmp_path, admin.hid, 'own2', 'owner')
    o2 = login(app, 'own2')
    o2.hid = admin.hid
    from webhelpers import page_csrf
    o2.csrf = page_csrf(o2, '/h/%d/bills' % admin.hid)
    t = make_token(o2, 'write')
    rid = get(anon, '/bills', t).json()['bills'][0]['id']
    assert get(anon, '/me', t).json()['role'] == 'editor'           # write scope: at most an editor
    uid = next(m['user_id'] for m in admin.api('GET', '/members').json()['members'] if m['username'] == 'own2')
    admin.api('PUT', '/members/%d' % uid, {'role': 'viewer'})
    assert get(anon, '/me', t).json()['role'] == 'viewer'
    r = post(anon, '/bills/%s/status' % rid, t, {'status': 'manual'})
    assert r.status_code == 403 and 'member who made this token' in r.json()['message']
    admin.api('DELETE', '/members/%d' % uid)
    assert get(anon, '/me', t).status_code == 401


def test_expired_token(admin, anon, app):
    t = make_token(admin, days=1)
    assert get(anon, '/me', t).status_code == 200
    from datetime import UTC, datetime, timedelta

    from hbudget.db import connection, tokens
    conn = connection.connect(app.state.settings.db_path)
    try:
        assert tokens.lookup(conn, t, now=datetime.now(UTC) + timedelta(days=2)) is None
    finally:
        conn.close()


def test_rate_limit(admin, anon, app):
    t = make_token(admin)
    app.state.api_rate = api_v1.RateLimiter(3, 60)
    assert [get(anon, '/me', t).status_code for _ in range(4)] == [200, 200, 200, 429]
    r = get(anon, '/me', t)
    assert r.json()['error'] == 'too_many' and int(r.headers['retry-after']) >= 1


def test_openapi_only_for_token_holders_and_members(admin, anon):
    assert anon.get('/api/v1/openapi.json').status_code == 401
    spec = admin.get('/api/v1/openapi.json').json()
    assert spec['openapi'].startswith('3.')
    assert '/api/v1/summary' in spec['paths'] and '/api/v1/what-if' in spec['paths']
    assert not any(p.startswith('/api/h/') for p in spec['paths'])         # internal routes stay out
    assert spec['components']['securitySchemes']['token']['scheme'] == 'bearer'
    t = make_token(admin)
    assert get(anon, '/openapi.json', t).status_code == 200
    assert admin.get('/openapi.json').status_code == 404                    # the app's own spec stays off


# ---------------------------------------------------------------- /mcp

def test_mcp_handshake_and_tools(admin, anon):
    seed(admin)
    ro, rw = make_token(admin, 'read'), make_token(admin, 'write', name='rw')
    r = rpc(anon, ro, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                      'clientInfo': {'name': 'test', 'version': '1'}})
    res = r.json()['result']
    assert r.status_code == 200 and res['protocolVersion'] == '2025-06-18' and res['capabilities'] == {
        'tools': {'listChanged': False}} and res['serverInfo']['name'] == 'hearthfund'
    old = rpc(anon, ro, 'initialize', {'protocolVersion': '1999-01-01'}).json()['result']
    assert old['protocolVersion'] == '2025-11-25'          # an unknown version: the newest one offered
    assert rpc(anon, ro, 'notifications/initialized', mid=None).status_code == 202
    assert rpc(anon, ro, 'ping').json()['result'] == {}
    read_tools = {t['name'] for t in rpc(anon, ro, 'tools/list').json()['result']['tools']}
    all_tools = {t['name']: t for t in rpc(anon, rw, 'tools/list').json()['result']['tools']}
    assert 'get_summary' in read_tools and 'add_expected_item' not in read_tools and 'set_bill_status' not in read_tools
    assert set(all_tools) - read_tools == {
        'add_expected_item', 'set_bill_status', 'set_expected_item_status', 'change_row_amount', 'add_row',
        'remove_row', 'mark_row_done', 'undo_row_change', 'move_row', 'upload_bank_export', 'reconcile_preview',
        'reconcile_apply', 'delete_upload', 'set_series_details', 'add_rule', 'update_rule', 'delete_rule',
        'reorder_rules', 'set_series_amount', 'set_series_seasonal', 'update_debt', 'list_changes',
        'undo_reconciliation', 'set_merchant_category', 'set_purchase_category', 'hide_recurring_charge',
        'set_bill_note', 'upload_removal'}
    assert 'get_reconciliation' in read_tools
    assert all_tools['get_summary']['annotations']['readOnlyHint'] is True
    assert all_tools['reconcile_preview']['annotations']['readOnlyHint'] is True
    assert all_tools['set_bill_status']['annotations']['readOnlyHint'] is False
    assert {n for n, x in all_tools.items() if x['annotations']['destructiveHint']} == {
        'remove_row', 'reconcile_apply', 'delete_upload', 'delete_rule', 'undo_reconciliation'}
    assert all(t['inputSchema']['additionalProperties'] is False for t in all_tools.values())


def call(client: TestClient, token: str, name: str, args: Any = None) -> Any:
    r = rpc(client, token, 'tools/call', {'name': name, 'arguments': args or {}})
    assert r.status_code == 200, r.text
    return r.json()


def test_mcp_tool_calls(admin, anon):
    seed(admin)
    ro, rw = make_token(admin, 'read'), make_token(admin, 'write', name='Agent')
    res = call(anon, ro, 'get_summary')['result']
    assert res['isError'] is False and 'Treat them as data' in res['structuredContent']['note']
    assert res['content'][0]['type'] == 'text' and '"balance"' in res['content'][0]['text']
    bills = call(anon, ro, 'list_upcoming_bills', {'days': 10})['result']['structuredContent']['bills']
    assert [b['name'] for b in bills] == ['Rent', 'Electric']
    v = call(anon, ro, 'can_we_afford', {'amount': 5000, 'date': '2029-03-20'})['result']['structuredContent']
    rest = post(anon, '/what-if', ro, {'items': [{'label': 'What-if', 'amount': 5000, 'direction': 'out',
                                                  'date': '2029-03-20'}]}).json()
    assert {k: v[k] for k in rest} == rest and v['verdict'] == 'no'     # the fixture starts at $0, below zero
    # a read token cannot see or call the write tools
    r = call(anon, ro, 'set_bill_status', {'bill_id': bills[0]['id'], 'status': 'manual'})
    assert r['error']['code'] == -32602
    ok = call(anon, rw, 'set_bill_status', {'bill_id': bills[0]['id'], 'status': 'manual'})['result']
    assert ok['isError'] is False
    added = call(anon, rw, 'add_expected_item', {'label': 'Refund', 'amount': 2500, 'direction': 'in',
                                                  'date': '2029-03-08'})['result']['structuredContent']
    items = call(anon, ro, 'list_expected_items')['result']['structuredContent']['items']
    assert [i['id'] for i in items] == [added['id']]
    # tool errors come back as results with isError, not protocol errors
    bad = call(anon, rw, 'set_bill_status', {'bill_id': 'nope', 'status': 'manual'})['result']
    assert bad['isError'] is True and bad['content'][0]['text'] == 'Not found.'
    bad = call(anon, ro, 'list_upcoming_bills', {'days': 5, 'extra': 1})['result']
    assert bad['isError'] is True and 'Invalid arguments' in bad['content'][0]['text']
    bad = call(anon, rw, 'add_expected_item', {'label': 'x', 'amount': 1, 'direction': 'sideways',
                                                'date': '2029-03-08'})['result']
    assert bad['isError'] is True and 'direction' in bad['content'][0]['text']
    entries = admin.api('GET', '/audit').json()['entries']
    assert {e['via'] for e in entries if e['via']} == {'token: Agent'}


def test_mcp_protocol_errors(admin, anon):
    t = make_token(admin)
    assert rpc(anon, None, 'ping').status_code == 401
    assert rpc(anon, None, 'notifications/initialized', mid=None).status_code == 401
    assert rpc(anon, t, 'resources/list').json()['error']['code'] == -32601
    assert call(anon, t, 'no_such_tool')['error']['code'] == -32602
    h = {'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json'}
    assert anon.post('/mcp', headers=h, content=b'{not json').json()['error']['code'] == -32700
    r = anon.post('/mcp', headers=h, json=[{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}])
    assert r.status_code == 400 and r.json()['error']['code'] == -32600
    assert anon.post('/mcp', headers=h, json={'id': 1, 'method': 'ping'}).status_code == 400
    r = rpc(anon, t, 'ping', **{'MCP-Protocol-Version': '2000-01-01'})
    assert r.status_code == 400
    assert rpc(anon, t, 'ping', **{'MCP-Protocol-Version': '2025-06-18'}).status_code == 200
    assert anon.get('/mcp', headers=h).status_code == 405
    assert anon.delete('/mcp', headers=h).status_code == 405
    # a browser with only a session cookie gets nothing
    assert admin.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                      headers={'X-CSRF-Token': admin.csrf, 'Origin': 'http://testserver'}).status_code == 401


# ---------------------------------------------------------------- row edits and reconciling (issues #20, #21)

def tool(anon: TestClient, token: str, tool_name: str, **args: Any) -> Any:
    r = rpc(anon, token, 'tools/call', {'name': tool_name, 'arguments': args}).json()['result']
    return r['structuredContent'] if not r['isError'] else r['content'][0]['text']


def test_row_edits_with_a_token(admin, anon):
    seed(admin)
    t, ro = make_token(admin, 'write', name='Sync'), make_token(admin, 'read')
    rent = next(x for x in get(anon, '/bills', t).json()['bills'] if x['name'] == 'Rent')
    change = '/rows/%s/change' % rent['id']
    assert post(anon, change, ro, {'amount': 90000, 'expected_amount': 100000}).status_code == 403
    r = post(anon, change, t, {'amount': 90000, 'expected_amount': 99999})
    assert r.status_code == 409 and 'now 100000' in r.json()['message']
    assert post(anon, change, t, {'amount': 90000, 'expected_amount': 100000}).status_code == 200
    bills = {x['id']: x for x in get(anon, '/bills', t).json()['bills']}
    assert bills[rent['id']]['amount'] == 90000
    assert post(anon, '/rows/%s/undo' % rent['id'], t, None).status_code == 200
    assert {x['id']: x for x in get(anon, '/bills', t).json()['bills']}[rent['id']]['amount'] == 100000
    # remove, then undo
    assert tool(anon, t, 'remove_row', row_id=rent['id'], expected_amount=100000) == {'note': api_v1.DATA_NOTE,
                                                                                         'ok': True}
    assert rent['id'] not in {x['id'] for x in get(anon, '/bills', t).json()['bills']}
    assert tool(anon, t, 'undo_row_change', row_id=rent['id'])['ok'] is True
    assert rent['id'] in {x['id'] for x in get(anon, '/bills', t).json()['bills']}
    # already paid: it leaves the upcoming list without being removed, and undo opens it again
    assert tool(anon, t, 'mark_row_done', row_id=rent['id'], expected_amount=100000)['ok'] is True
    assert rent['id'] not in {x['id'] for x in get(anon, '/bills', t).json()['bills']}
    assert 'cannot clear' in tool(anon, t, 'mark_row_done', row_id=rent['id'], expected_amount=100000)
    assert tool(anon, t, 'undo_row_change', row_id=rent['id'])['ok'] is True
    assert post(anon, '/rows/%s/done' % rent['id'], ro, {'expected_amount': 100000}).status_code == 403
    assert post(anon, '/rows/%s/done' % rent['id'], t, {'expected_amount': 100000}).status_code == 200
    assert post(anon, '/rows/%s/undo' % rent['id'], t, None).status_code == 200
    # a one-off income row, listed after the payday
    pay_id = next(x['id'] for x in get(anon, '/bills', t).json()['bills'] if x['name'] == 'Pay A')
    rid = tool(anon, t, 'add_row', name='Refund A', kind='income', amount=2500, due='2029-03-15', after=pay_id)['id']
    row = {x['id']: x for x in get(anon, '/bills', t).json()['bills']}[rid]
    assert (row['amount'], row['direction'], row['due']) == (2500, 'in', '2029-03-15')
    assert 'changed since you read it' in tool(anon, t, 'change_row_amount', row_id=rid, amount=1, expected_amount=9)
    assert 'No row' in tool(anon, t, 'remove_row', row_id='nope', expected_amount=1)
    assert post(anon, '/rows/%s/move' % rid, t, {'after': pay_id}).status_code == 200
    via = [(e['action'], e['via']) for e in admin.api('GET', '/audit').json()['entries'] if e['via']]
    assert {('row.add', 'token: Sync'), ('row.remove', 'token: Sync'), ('row.done', 'token: Sync')} <= set(via)


def test_reconcile_with_a_token(admin, anon, app):
    from test_reconcile_flow import FIX, import_synthetic
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 3, 12)
    t, ro = make_token(admin, 'write', name='Sync'), make_token(admin, 'read')
    data = (FIX / 'synthetic' / 'bank.csv').read_bytes()
    h = {'Authorization': 'Bearer ' + t}
    assert anon.post('/api/v1/uploads', headers={'Authorization': 'Bearer ' + ro},
                     files={'file': ('export.csv', data)}).status_code == 403
    assert anon.post('/api/v1/uploads', headers=h, files={'file': ('export.exe', data)}).status_code == 415
    r = anon.post('/api/v1/uploads', headers=h, files={'file': ('export.csv', data)}, data={'kind': 'bank'})
    assert r.status_code == 200, r.text
    up = r.json()
    assert up['status'] == 'waiting' and up['latest_balance'] == 63500
    assert [u['id'] for u in get(anon, '/uploads', ro).json()['uploads']] == [up['id']]
    p = post(anon, '/uploads/%s/preview' % up['id'], t, None).json()
    assert {x['id'] for x in p['proposals']} == {'P1', 'P2'} and p['fingerprint']
    line = p['proposals'][0]['txn']
    assert line['amount'] > 0 and line['direction'] in ('in', 'out')
    assert all(r['amount'] >= 0 for x in p['proposals'] for r in x['rows'])
    r = post(anon, '/uploads/%s/apply' % up['id'], t, {'fingerprint': 'x' * 32, 'accept': {'P1': 'clear'}})
    assert r.status_code == 409
    r = post(anon, '/uploads/%s/apply' % up['id'], t, {'fingerprint': p['fingerprint'],
                                                      'accept': {'P1': 'clear', 'P2': 'clear'}})
    assert r.status_code == 200, r.text
    assert r.json()['balance'] == 63500 and get(anon, '/summary', t).json()['balance'] == 63500
    assert post(anon, '/uploads/%s/preview' % up['id'], t, None).status_code == 409
    via = [(e['action'], e['via']) for e in admin.api('GET', '/audit').json()['entries'] if e['via']]
    assert ('upload.bank', 'token: Sync') in via and ('reconcile.apply', 'token: Sync') in via


def test_reconcile_over_mcp(admin, anon, app):
    from test_reconcile_flow import FIX, import_synthetic
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 3, 12)
    t = make_token(admin, 'write')
    text = (FIX / 'synthetic' / 'bank.csv').read_text()
    up = tool(anon, t, 'upload_bank_export', filename='export.csv', content=text)
    assert up['status'] == 'waiting'
    p = tool(anon, t, 'reconcile_preview', upload_id=up['id'])
    assert p['note'] == api_v1.DATA_NOTE and len(p['proposals']) == 2
    done = tool(anon, t, 'reconcile_apply', upload_id=up['id'], fingerprint=p['fingerprint'],
                accept={'P1': 'clear', 'P2': 'clear'})
    assert done['balance'] == 63500
    assert 'already imported' in tool(anon, t, 'reconcile_preview', upload_id=up['id'])
    # a file problem reads as the page says it, not as bad arguments
    lines = (FIX / 'synthetic' / 'bank.csv').read_text().splitlines()
    gap = tool(anon, t, 'upload_bank_export', filename='gap.csv', content='\n'.join(lines[:3] + lines[4:]))
    msg = tool(anon, t, 'reconcile_preview', upload_id=gap['id'])
    assert isinstance(msg, str) and not msg.startswith('Invalid')
    assert tool(anon, t, 'delete_upload', upload_id=gap['id'])['ok'] is True
    # an unknown layout is refused with what to do; nothing is stored
    msg = tool(anon, t, 'upload_bank_export', filename='odd.csv', content='When,What,How much\n2029-03-01,x,1\n')
    assert 'No saved layout' in msg and 'When' in msg
    assert len(tool(anon, t, 'list_uploads')['uploads']) == 1
    assert 'Use a file ending' in tool(anon, t, 'upload_bank_export', filename='a.txt', content='x')
    assert tool(anon, t, 'delete_upload', upload_id=up['id'])['outcome'] == 'hidden'


def test_csv_layout_saved_from_an_agent(admin, anon, app):
    from test_reconcile_flow import FIX, import_synthetic
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 3, 12)
    t = make_token(admin, 'write')
    lines = (FIX / 'synthetic' / 'bank.csv').read_text().splitlines()
    native = '\n'.join(['Transaction ID,Posting Date,Amount,Description,Posting Status,Balance']
                       + ['T%d,%s,%s,%s,Posted,%s' % (i, *line.split(',')[0:1], line.split(',')[2],
                                                       line.split(',')[1], line.split(',')[3])
                          for i, line in enumerate(lines[1:])])
    msg = tool(anon, t, 'upload_bank_export', filename='native.csv', content=native)
    assert 'No saved layout' in msg and 'save_as' in msg
    guess = json.loads(msg[msg.index('{'):])
    assert guess == {'date_col': 'Posting Date', 'description_cols': ['Description'], 'balance_col': 'Balance',
                     'id_col': 'Transaction ID', 'status_col': 'Posting Status', 'amount_col': 'Amount',
                     'posted_values': ['Posted']}
    bad = tool(anon, t, 'upload_bank_export', filename='native.csv', content=native,
               mapping={**guess, 'date_col': 'Nope'})
    assert isinstance(bad, str) and len(tool(anon, t, 'list_uploads')['uploads']) == 0
    up = tool(anon, t, 'upload_bank_export', filename='native.csv', content=native, mapping=guess,
              save_as='Bank native')
    assert up['status'] == 'waiting'
    layouts = tool(anon, t, 'list_uploads')['layouts']
    assert [x['name'] for x in layouts] == ['Bank native']
    assert tool(anon, t, 'delete_upload', upload_id=up['id'])['ok'] is True
    # the next export with the same columns is read without a mapping, over REST too
    r = anon.post('/api/v1/uploads', headers={'Authorization': 'Bearer ' + t},
                  files={'file': ('again.csv', native.encode())})
    assert r.status_code == 200 and r.json()['status'] == 'waiting'
    r = anon.post('/api/v1/uploads', headers={'Authorization': 'Bearer ' + t},
                  files={'file': ('x.csv', b'When,What\n2029-03-01,x\n')}, data={'mapping': '[1]'})
    assert r.status_code == 400


def test_series_card_without_resetting_amounts(admin, anon, app):
    from dataclasses import replace

    from test_reconcile_flow import import_synthetic

    from hbudget.db import connection, repo
    import_synthetic(admin)
    t, ro = make_token(admin, 'write'), make_token(admin, 'read')
    listed = tool(anon, ro, 'list_series')
    assert listed['cards'] == [{'id': 'rewards', 'name': 'Rewards card'}]
    assert {s['id']: s['card'] for s in listed['series']}['electric'] is None

    def rows() -> list[Any]:
        conn = connection.connect(app.state.settings.db_path)
        try:
            return [r for r in repo.load_budget(conn, admin.hid).rows if r.series_id == 'electric']
        finally:
            conn.close()
    # an imported row whose amount differs from the series amount, not marked as changed by hand (#24)
    target = [r for r in rows() if r.state.value == 'open'][1]
    conn = connection.connect(app.state.settings.db_path)
    repo.edit_budget(conn, admin.hid, lambda b: replace(b, rows=tuple(
        replace(r, amount=r.amount + 123) if r.id == target.id else r for r in b.rows)), user_id=None, action='test')
    conn.close()
    assert 'error' in rpc(anon, ro, 'tools/call', {'name': 'set_series_details', 'arguments': {}}).json()
    assert tool(anon, t, 'set_series_details', series_id='electric', card='rewards')['ok'] is True
    now = {r.id: r for r in rows()}[target.id]
    assert (now.amount, now.card) == (target.amount + 123, 'rewards')
    # the page's editor keeps it too when only the card or notes change
    s = admin.api('GET', '/series').json()['series']
    body = {k: v for k, v in next(x for x in s if x['id'] == 'electric').items()
            if k not in ('id', 'month_amounts', 'cycle_amounts', 'generated_through')}
    assert admin.api('PUT', '/series/electric', {**body, 'notes': 'n', 'card': None}).status_code == 200
    assert {r.id: r for r in rows()}[target.id].amount == target.amount + 123
    assert admin.api('PUT', '/series/electric', {**body, 'amount': 16000}).status_code == 200
    assert {r.id: r for r in rows()}[target.id].amount == 16000
    assert 'No credit card' in tool(anon, t, 'set_series_details', series_id='electric', card='nope')
    assert 'Not found' in tool(anon, t, 'set_series_details', series_id='nope', status='manual')
    r = anon.patch('/api/v1/series/electric', headers={'Authorization': 'Bearer ' + t}, json={'card': None})
    assert r.status_code == 200
    assert {s['id']: s['card'] for s in tool(anon, ro, 'list_series')['series']}['electric'] is None


def test_rules_with_a_token(admin, anon):
    from test_reconcile_flow import import_synthetic
    import_synthetic(admin)
    t, ro = make_token(admin, 'write', name='Sync'), make_token(admin, 'read')
    listed = tool(anon, ro, 'list_rules')
    assert [r['id'] for r in listed['rules']][:2] == ['payroll-a', 'mortgage'] and 'track' in listed['kinds']
    hit = tool(anon, ro, 'test_rule', description='ONLINE MARKETPLACE 123', amount=-999)
    assert hit['first']['id'] == 'shopping'
    assert 'error' in rpc(anon, ro, 'tools/call', {'name': 'add_rule', 'arguments': {}}).json()
    rid = tool(anon, t, 'add_rule', name='Streaming', kind='bill', contains=['ONLINE MARKETPLACE'],
               min_amount=999, max_amount=999, targets=['electric'], before='shopping')['id']
    order = [r['id'] for r in tool(anon, ro, 'list_rules')['rules']]
    assert order.index(rid) == order.index('shopping') - 1
    assert tool(anon, ro, 'test_rule', description='ONLINE MARKETPLACE', amount=-999)['first']['id'] == rid
    assert tool(anon, ro, 'test_rule', description='ONLINE MARKETPLACE', amount=-1000)['first']['id'] == 'shopping'
    assert tool(anon, t, 'update_rule', rule_id=rid, max_amount=1099)['ok'] is True
    changed = next(r for r in tool(anon, ro, 'list_rules')['rules'] if r['id'] == rid)
    assert (changed['min_amount'], changed['max_amount'], changed['targets']) == (999, 1099, ['electric'])
    assert 'do not exist' in tool(anon, t, 'update_rule', rule_id=rid, targets=['nope'])
    assert 'every rule' in tool(anon, t, 'reorder_rules', ids=[rid])
    assert tool(anon, t, 'reorder_rules', ids=list(reversed(order)))['ok'] is True
    assert tool(anon, t, 'delete_rule', rule_id=rid)['ok'] is True
    assert 'Not found' in tool(anon, t, 'delete_rule', rule_id=rid)
    r = anon.patch('/api/v1/rules/shopping', headers={'Authorization': 'Bearer ' + t}, json={'note': 'n'})
    assert r.status_code == 200
    via = [(e['action'], e['via']) for e in admin.api('GET', '/audit').json()['entries'] if e['via']]
    assert ('rule.add', 'token: Sync') in via and ('rule.delete', 'token: Sync') in via


def test_allowance_rule_over_mcp(admin, anon, app):
    from test_reconcile_flow import FIX, import_synthetic

    from hbudget.core.reconcile import match, prepare
    from hbudget.web import views
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 3, 12)
    t = make_token(admin, 'write')
    assert tool(anon, t, 'update_rule', rule_id='shopping', kind='allowance', targets=['grocery'])['ok'] is True
    up = tool(anon, t, 'upload_bank_export', filename='e.csv', content=(FIX / 'synthetic' / 'bank.csv').read_text())
    p = tool(anon, t, 'reconcile_preview', upload_id=up['id'])
    [a] = [x for x in p['auto'] if x['rule'] == 'shopping']
    assert (a['amount'], a['row']['name'], a['row']['amount']) == (3000, 'Grocery', 16000)
    # a leftover proposal has no bank line
    from factories import D, budget, chain, row, rule
    b = budget(as_of=D(2029, 2, 28), rows=(row('s3', 'Subscriptions', 15500, D(2029, 3, 18), series_id='subs'),),
               rules=(rule('subs', 'allowance', ('PROCESSOR',), targets=('subs',)),))
    prep = prepare(chain(100000, [('x', D(2029, 4, 2), -10, 'OTHER')]), b)
    out = api_v1._preview(views.preview(b, prep, match(b, prep)))
    assert [(x['kind'], x['txn'], x['rows'][0]['amount']) for x in out['proposals']] == [('leftover', None, 15500)]


def test_undo_a_reconciliation(admin, anon, app):
    from test_reconcile_flow import FIX, import_synthetic
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 3, 12)
    t = make_token(admin, 'write', name='Sync')
    before = tool(anon, t, 'get_summary')
    open_before = {b['id']: b['amount'] for b in tool(anon, t, 'list_upcoming_bills', days=60)['bills']}
    text = (FIX / 'synthetic' / 'bank.csv').read_text()
    up = tool(anon, t, 'upload_bank_export', filename='export.csv', content=text)
    p = tool(anon, t, 'reconcile_preview', upload_id=up['id'])
    done = tool(anon, t, 'reconcile_apply', upload_id=up['id'], fingerprint=p['fingerprint'],
                accept={'P1': 'clear', 'P2': 'clear'})
    [rec] = tool(anon, t, 'list_reconciliations')['reconciliations']
    assert rec['id'] == done['id'] and tool(anon, t, 'get_summary')['balance'] == 63500
    # a row it cleared changed since: refused
    cleared = [x for x in open_before if x not in {b['id'] for b in tool(anon, t, 'list_upcoming_bills',
                                                                          days=60)['bills']}]
    assert cleared
    undone = tool(anon, t, 'undo_reconciliation', rec_id=rec['id'])
    assert undone['ok'] is True and undone['upload_id'] == up['id'] and undone['rows_restored'] >= len(cleared)
    assert tool(anon, t, 'get_summary')['balance'] == before['balance']
    assert {b['id']: b['amount'] for b in tool(anon, t, 'list_upcoming_bills', days=60)['bills']} == open_before
    assert tool(anon, t, 'list_reconciliations')['reconciliations'] == []
    assert next(u for u in tool(anon, t, 'list_uploads')['uploads'] if u['id'] == up['id'])['status'] == 'waiting'
    # it can be reconciled again, and then undo is refused once a row it cleared has changed
    p = tool(anon, t, 'reconcile_preview', upload_id=up['id'])
    rec_id = tool(anon, t, 'reconcile_apply', upload_id=up['id'], fingerprint=p['fingerprint'], accept={})['id']
    pay = next(b for b in tool(anon, t, 'list_upcoming_bills', days=60)['bills'] if b['name'] == 'Car loan')
    assert tool(anon, t, 'change_row_amount', row_id=pay['id'], amount=1, expected_amount=pay['amount'])['ok']
    # a row the reconciliation did not touch may change: undo still works
    assert tool(anon, t, 'undo_reconciliation', rec_id=rec_id)['ok'] is True
    assert 'Not found' in tool(anon, t, 'undo_reconciliation', rec_id='nope')
    actions = [e['action'] for e in admin.api('GET', '/audit').json()['entries']]
    assert 'reconcile.undo' in actions


def test_undo_refused_when_a_touched_row_changed(admin, anon, app):
    from test_reconcile_flow import FIX, import_synthetic
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 3, 12)
    t = make_token(admin, 'write')
    up = tool(anon, t, 'upload_bank_export', filename='e.csv', content=(FIX / 'synthetic' / 'bank.csv').read_text())
    p = tool(anon, t, 'reconcile_preview', upload_id=up['id'])
    [auto] = [a for a in p['auto'] if a['row']['name'] == 'Mortgage']
    rec_id = tool(anon, t, 'reconcile_apply', upload_id=up['id'], fingerprint=p['fingerprint'], accept={})['id']
    hid = admin.hid
    from dataclasses import replace

    from hbudget.db import connection, repo
    conn = connection.connect(app.state.settings.db_path)
    repo.edit_budget(conn, hid, lambda b: replace(b, rows=tuple(replace(r, note='x') if r.id == auto['row']['id']
                                                                else r for r in b.rows)), user_id=None, action='t')
    conn.close()
    msg = tool(anon, t, 'undo_reconciliation', rec_id=rec_id)
    assert 'changed after the reconciliation: Mortgage' in msg


def test_changes_feed(admin, anon):
    seed(admin)
    t = make_token(admin, 'write', name='Sync')
    s = tool(anon, t, 'get_summary')
    start = s['last_change']
    assert s['budget_version'] >= 1 and start > 0
    rent = next(x for x in tool(anon, t, 'list_upcoming_bills')['bills'] if x['name'] == 'Rent')
    tool(anon, t, 'change_row_amount', row_id=rent['id'], amount=90000, expected_amount=rent['amount'])
    assert admin.api('POST', '/rows/%s/undo' % rent['id'], {}).status_code == 200
    got = tool(anon, t, 'list_changes', since=start)
    assert [(x['action'], x['by'], x['item']) for x in got['changes']] == [
        ('row.change', 'token: Sync', rent['id']), ('row.undo', 'admin', rent['id'])]
    assert got['cursor'] == tool(anon, t, 'get_summary')['last_change'] and got['more'] is False
    assert tool(anon, t, 'list_changes', since=got['cursor'])['changes'] == []
    one = tool(anon, t, 'list_changes', since=start, limit=1)
    assert len(one['changes']) == 1 and one['more'] is True
    ro = make_token(admin, 'read')
    assert 'error' in rpc(anon, ro, 'tools/call', {'name': 'list_changes', 'arguments': {}}).json()


def test_update_debt(admin, anon, app):
    from test_reconcile_flow import import_synthetic
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 5, 1)
    t = make_token(admin, 'write')
    [d] = tool(anon, t, 'get_debts')['debts']
    assert d['id'] == 'rewards'
    assert 'changed since you read it' in tool(anon, t, 'update_debt', debt_id='rewards', balance=1, expected_balance=7)
    out = tool(anon, t, 'update_debt', debt_id='rewards', balance=150000, expected_balance=d['balance'],
               apr_bp=2299)
    assert out['ok'] is True
    [d2] = tool(anon, t, 'get_debts')['debts']
    assert (d2['balance'], d2['apr_bp'], d2['as_of'], d2['payment'], d2['stale']) == (
        150000, 2299, '2029-05-01', d['payment'], False)
    app.state.clock = lambda: __import__('datetime').date(2029, 6, 10)
    assert tool(anon, t, 'get_debts')['debts'][0]['stale'] is True
    assert 'No debt' in tool(anon, t, 'update_debt', debt_id='checking', balance=1)
    assert 'Send one or more' in tool(anon, t, 'update_debt', debt_id='rewards')
    r = anon.patch('/api/v1/debts/rewards', headers={'Authorization': 'Bearer ' + t}, json={'name': 'x'})
    assert r.status_code == 400


def test_series_amount_from_a_date_and_seasonal(admin, anon):
    from test_reconcile_flow import import_synthetic
    import_synthetic(admin)
    t = make_token(admin, 'write')

    def food() -> list[Any]:
        return [b for b in tool(anon, t, 'list_upcoming_bills', days=120)['bills'] if b['name'] == 'Grocery']
    rows = food()
    assert len(rows) >= 3 and all(r['amount'] == 16000 for r in rows)
    cut = rows[1]['due']
    msg = tool(anon, t, 'set_series_amount', series_id='grocery', amount=17500, from_date=cut, expected_amount=1)
    assert 'changed since you read it' in msg
    out = tool(anon, t, 'set_series_amount', series_id='grocery', amount=17500, from_date=cut, expected_amount=16000)
    assert [x['now'] for x in out['changed']] == [17500] * (len(rows) - 1) and out['changed'][0]['due'] == cut
    assert [r['amount'] for r in food()] == [16000] + [17500] * (len(rows) - 1)
    assert {s['id']: s['amount'] for s in tool(anon, t, 'list_series')['series']}['grocery'] == 17500
    # seasonal: a dry run changes nothing
    dry = tool(anon, t, 'set_series_seasonal', series_id='electric', months={'3-4': 20000, '5-2': 15000},
               dry_run=True)
    assert dry['dry_run'] is True and dry['changed'] and all(x['now'] in (20000, 15000) for x in dry['changed'])
    assert tool(anon, t, 'set_series_seasonal', series_id='electric', months={'3-4': 20000})['changed']
    assert 'Not found' in tool(anon, t, 'set_series_amount', series_id='nope', amount=1, from_date='2029-03-01')
    assert 'YYYY-MM-DD' in tool(anon, t, 'set_series_amount', series_id='grocery', amount=1, from_date='soon')


def test_series_amount_dry_run_and_end_date(admin, anon):
    """#94: dry_run shows the rows and the forecast low without saving; to_date limits the change to a range and
    leaves the item's own amount alone."""
    from test_reconcile_flow import import_synthetic
    import_synthetic(admin)
    t = make_token(admin, 'write')

    def food() -> list[Any]:
        return [b for b in tool(anon, t, 'list_upcoming_bills', days=120)['bills'] if b['name'] == 'Grocery']
    rows = food()
    assert len(rows) >= 3
    dry = tool(anon, t, 'set_series_amount', series_id='grocery', amount=20000, from_date=rows[0]['due'],
               dry_run=True)
    assert dry['dry_run'] is True and len(dry['changed']) == len(rows) and dry['low'] < dry['low_before']
    assert [r['amount'] for r in food()] == [16000] * len(rows)
    # only the second row: the rows after it and the item's amount stay
    one = tool(anon, t, 'set_series_amount', series_id='grocery', amount=12000, from_date=rows[1]['due'],
               to_date=rows[1]['due'])
    assert [(x['due'], x['now']) for x in one['changed']] == [(rows[1]['due'], 12000)] and one['dry_run'] is False
    assert [r['amount'] for r in food()] == [16000, 12000] + [16000] * (len(rows) - 2)
    assert {s['id']: s['amount'] for s in tool(anon, t, 'list_series')['series']}['grocery'] == 16000
    # a later change from an earlier date keeps the hand-set row
    tool(anon, t, 'set_series_amount', series_id='grocery', amount=17000, from_date=rows[0]['due'])
    assert [r['amount'] for r in food()] == [17000, 12000] + [17000] * (len(rows) - 2)
    assert 'before the start' in tool(anon, t, 'set_series_amount', series_id='grocery', amount=1,
                                      from_date=rows[1]['due'], to_date=rows[0]['due'])
    r = post(anon, '/series/grocery/amount', t, {'amount': 1, 'from_date': rows[0]['due'], 'dry_run': True})
    assert r.status_code == 200 and r.json()['dry_run'] is True


def test_get_reconciliation_shows_each_line(admin, anon, app):
    """#93: what a reconciliation did, line by line, over MCP and /api/v1."""
    from test_reconcile_flow import FIX, import_synthetic
    import_synthetic(admin)
    app.state.clock = lambda: __import__('datetime').date(2029, 3, 12)
    t, ro = make_token(admin, 'write'), make_token(admin, 'read')
    text = (FIX / 'synthetic' / 'bank.csv').read_text()
    up = tool(anon, t, 'upload_bank_export', filename='export.csv', content=text)
    p = tool(anon, t, 'reconcile_preview', upload_id=up['id'])
    done = tool(anon, t, 'reconcile_apply', upload_id=up['id'], fingerprint=p['fingerprint'], accept={'P1': 'clear'})
    rec = tool(anon, ro, 'list_reconciliations')['reconciliations'][0]
    got = tool(anon, ro, 'get_reconciliation', rec_id=rec['id'])
    assert got['new_balance'] == done['balance'] and got['applied_by'] == 'admin' and got['upload_id'] == up['id']
    d = got['details']
    assert {x['id']: x['decision'] for x in d['proposals']} == {'P1': 'clear', 'P2': 'skipped'}
    assert d['balance_before'] != got['new_balance'] and d['balance_checked'] is True
    assert d['auto'] and all(x['row'] and x['txn'] and x['description'] for x in d['auto'])
    assert all('reason' in x for x in d['unknown'])
    seen = {x['txn'] for k in ('auto', 'proposals', 'tracked', 'unknown') for x in d[k]}
    assert {x['txn']['id'] for x in p['proposals']} <= seen
    assert get(anon, '/reconciliations/%s' % rec['id'], ro).json()['details'] == d
    assert get(anon, '/reconciliations/nope', ro).status_code == 404
    assert 'Not found' in tool(anon, ro, 'get_reconciliation', rec_id='nope')
