"""Money > Investments (D-102): accounts and dated balances through the page's API, the agent API and MCP; kept
apart from the debts; in the backup."""
from __future__ import annotations

from test_api_v1 import anon, get, make_token, post, tool  # noqa: F401 - anon is a fixture


def add(admin, name='Retirement plan', balance=1000000, **kw):
    r = admin.api('POST', '/investments', {'name': name, 'balance': balance, **kw})
    assert r.status_code == 200, r.text
    return r.json()['id']


def test_accounts_balances_and_the_summary(admin):
    assert admin.get('/h/%d/investments' % admin.hid).status_code == 200
    empty = admin.api('GET', '/investments').json()
    assert (empty['accounts'], empty['total'], empty['series'], empty['as_of']) == ([], 0, [], None)
    a = add(admin, as_of='2028-03-01', notes='at work')
    b = add(admin, 'Brokerage', 500000)                                  # today, 2029-03-01
    assert admin.api('POST', '/investments/%s/balance' % a, {'balance': 1150000, 'as_of': '2029-02-01'}).json() == {
        'id': a, 'balance': 1150000, 'as_of': '2029-02-01'}
    # an older statement fills the history; the balance stays the newest one
    assert admin.api('POST', '/investments/%s/balance' % a, {'balance': 1050000, 'as_of': '2028-09-01'}).json()[
        'balance'] == 1150000
    d = admin.api('GET', '/investments').json()
    ra, rb = d['accounts']
    assert (ra['name'], ra['balance'], ra['as_of'], ra['notes'], ra['source'], ra['bank']) == (
        'Retirement plan', 1150000, '2029-02-01', 'at work', 'typed', None)
    assert ra['history'] == [['2028-03-01', 1000000], ['2028-09-01', 1050000], ['2029-02-01', 1150000]]
    assert ra['change']['all'] == {'since': '2028-03-01', 'was': 1000000, 'amount': 150000, 'pct_bp': 1500}
    assert ra['change']['year'] == ra['change']['all']
    # 30 days back the newest balance known was the one from September
    assert ra['change']['month'] == {'since': '2028-09-01', 'was': 1050000, 'amount': 100000, 'pct_bp': 952}
    assert rb['change'] == {'month': None, 'quarter': None, 'year': None, 'all': None} and not rb['stale']
    assert d['total'] == 1650000 and d['as_of'] == '2029-02-01'
    assert d['series'] == [['2028-03-01', 1000000], ['2028-09-01', 1050000], ['2029-02-01', 1150000],
                           ['2029-03-01', 1650000]]
    assert d['change']['all']['amount'] == 650000
    # rename; take a mistyped balance out; the last one cannot go
    assert admin.api('PUT', '/investments/%s' % a, {'name': 'Plan at work', 'notes': ''}).status_code == 200
    assert admin.api('DELETE', '/investments/%s/balance/2029-02-01' % a).status_code == 200
    ra = admin.api('GET', '/investments').json()['accounts'][0]
    assert (ra['name'], ra['balance'], ra['as_of'], len(ra['history'])) == ('Plan at work', 1050000, '2028-09-01', 2)
    assert ra['stale']                                                    # typed more than 35 days ago
    assert admin.api('DELETE', '/investments/%s/balance/2029-03-01' % b).status_code == 400
    assert admin.api('DELETE', '/investments/%s/balance/2020-01-01' % a).status_code == 400
    assert admin.api('DELETE', '/investments/%s' % b).status_code == 200
    assert [x['id'] for x in admin.api('GET', '/investments').json()['accounts']] == [a]


def test_refused_input(admin):
    a = add(admin)
    for body in ({'name': 'Plan 12345678', 'balance': 1}, {'name': '', 'balance': 1}, {'name': 'X', 'balance': -1},
                 {'name': 'X', 'balance': 1, 'as_of': '2029-03-02'}, {'name': 'X'}):
        assert admin.api('POST', '/investments', body).status_code in (400, 422), body
    assert admin.api('POST', '/investments/%s/balance' % a, {'balance': 5, 'as_of': '2030-01-01'}).status_code == 400
    assert admin.api('POST', '/investments/nope/balance', {'balance': 5}).status_code == 404
    assert admin.api('PUT', '/investments/nope', {'name': 'X'}).status_code == 404
    assert admin.api('DELETE', '/investments/nope').status_code == 404


def test_kept_apart_from_the_debts(admin, anon):  # noqa: F811 - the anon fixture
    card = admin.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 100000,
                                           'apr_bp': 2000, 'payment': 5000}).json()['id']
    a = add(admin)
    ro = make_token(admin, 'read')
    assert [x['id'] for x in get(anon, '/debts', ro).json()['debts']] == [card]
    assert admin.api('GET', '/goals').json()['debt'] == 100000
    p = admin.api('POST', '/payoff', {}).json()
    assert a not in str(p)
    # the debt endpoints do not touch an investment account, and the other way round
    body = {'name': 'X', 'kind': 'loan', 'balance': 1}
    assert admin.api('PUT', '/accounts/%s' % a, body).status_code == 404
    assert admin.api('DELETE', '/accounts/%s' % a).status_code == 404
    assert admin.api('POST', '/accounts', {**body, 'kind': 'investment'}).status_code in (400, 422)
    assert admin.api('POST', '/investments/%s/balance' % card, {'balance': 5}).status_code == 404
    assert admin.api('DELETE', '/investments/%s' % card).status_code == 404
    assert admin.api('GET', '/investments').json()['total'] == 1000000


def test_agent_api_and_mcp(admin, anon):  # noqa: F811 - the anon fixture
    a = add(admin, as_of='2029-01-01')
    ro, rw = make_token(admin, 'read'), make_token(admin, 'write', 'Writer')
    d = get(anon, '/investments', ro).json()
    assert d == admin.api('GET', '/investments').json()
    assert {k: v for k, v in tool(anon, ro, 'get_investments').items() if k != 'note'} == d
    assert post(anon, '/investments/%s/balance' % a, ro, {'balance': 1}).status_code == 403
    r = post(anon, '/investments/%s/balance' % a, rw, {'balance': 1100000})
    assert r.status_code == 200 and r.json() == {'id': a, 'balance': 1100000, 'as_of': '2029-03-01'}
    t = tool(anon, rw, 'set_investment_balance', investment_id=a, balance=1090000, as_of='2029-02-01')
    assert (t['id'], t['balance'], t['as_of']) == (a, 1100000, '2029-03-01')
    assert 'No investment account' in tool(anon, rw, 'set_investment_balance', investment_id='nope', balance=1)
    assert 'Refused' in tool(anon, rw, 'set_investment_balance', investment_id=a, balance=1, as_of='2029-13-01')
    got = tool(anon, ro, 'get_investments')['accounts'][0]
    assert got['history'] == [['2029-01-01', 1000000], ['2029-02-01', 1090000], ['2029-03-01', 1100000]]
    assert got['change']['month'] == {'since': '2029-01-01', 'was': 1000000, 'amount': 100000, 'pct_bp': 1000}


def test_in_the_backup_restore_and_wipe(admin):
    a = add(admin, as_of='2028-03-01')
    admin.api('POST', '/investments/%s/balance' % a, {'balance': 1200000})
    before = admin.api('GET', '/investments').json()
    text = admin.get('/api/h/%d/export/backup.json' % admin.hid).content
    assert admin.api('POST', '/wipe', {'confirm': 'Test household'}).status_code == 200
    assert admin.api('GET', '/investments').json()['accounts'] == []
    r = admin.post('/api/h/%d/import/billsystem' % admin.hid, files={'file': ('backup.json', text)},
                   data={'mode': 'replace', 'dry_run': '0', 'confirm': 'Test household'},
                   headers={'X-CSRF-Token': admin.csrf, 'Origin': 'http://testserver'})
    assert r.status_code == 200, r.text
    assert admin.api('GET', '/investments').json() == before


def _file(admin, name, data):
    return admin.post('/api/h/%d/investments/file' % admin.hid, files={'file': (name, data)},
                      headers={'X-CSRF-Token': admin.csrf})


def test_a_file_is_read_shown_and_then_saved(admin):
    """D-103: the upload stores nothing; the chosen balances go into an account, old or new, as one change."""
    plan = add(admin, 'Plan at work', 900000, as_of='2029-01-15')
    r = _file(admin, 'mine.csv', b'Date,Plan at work,Brokerage\n2028-12-31,8000,100\n2029-02-28,9500,120\n'
                                 b'2029-06-30,1,1\n')
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d['kind'], d['skipped'], d['today']) == ('history', 2, '2029-03-01')       # June is after today
    a, b = d['readings']
    assert (a['label'], a['match'], a['dated'], a['found']) == ('Plan at work', plan, True, 2)
    assert a['points'] == [['2028-12-31', 800000], ['2029-02-28', 950000]] and b['match'] is None
    assert len(admin.api('GET', '/investments').json()['accounts']) == 1               # nothing stored yet
    out = admin.api('POST', '/investments/import', {'items': [
        {'account_id': plan, 'points': [{'as_of': x, 'balance': v} for x, v in a['points']]},
        {'name': 'Brokerage', 'points': [{'as_of': x, 'balance': v} for x, v in b['points']]}]})
    assert out.status_code == 200, out.text
    assert (out.json()['accounts'], out.json()['balances'], len(out.json()['added'])) == (2, 4, 1)
    got = {x['name']: x for x in admin.api('GET', '/investments').json()['accounts']}
    assert got['Plan at work']['history'] == [['2028-12-31', 800000], ['2029-01-15', 900000], ['2029-02-28', 950000]]
    assert (got['Plan at work']['balance'], got['Brokerage']['balance']) == (950000, 12000)
    assert got['Brokerage']['history'] == [['2028-12-31', 10000], ['2029-02-28', 12000]]
    # a positions file has no date: today is filled in; a statement shows holdings and cash apart
    d = _file(admin, 'p.csv', b'Symbol,Current Value\nAAA,"$1,000.00"\nBBB,$5.50\n').json()
    assert (d['kind'], d['readings'][0]['dated'], d['readings'][0]['points']) == (
        'positions', False, [['2029-03-01', 100550]])
    ofx = (b'<OFX><INVSTMTRS><DTASOF>20290228<INVPOS><MKTVAL>100.00</INVPOS><INVBAL><AVAILCASH>5.00</INVBAL>'
           b'</INVSTMTRS></OFX>')
    r0 = _file(admin, 's.qfx', ofx).json()['readings'][0]
    assert (r0['points'], r0['holdings'], r0['cash']) == ([['2029-02-28', 10500]], 10000, 500)


def test_file_and_import_refusals(admin):
    plan = add(admin)
    assert _file(admin, 'x.pdf', b'%PDF').status_code == 415
    assert _file(admin, 'x.csv', b'hello\nworld\n').status_code == 400
    assert _file(admin, 'x.csv', b'Date,Balance\n2030-01-01,5\n').status_code == 400   # only a date after today
    pt = [{'as_of': '2029-02-01', 'balance': 5}]
    for items in ([{'points': pt}], [{'account_id': plan, 'name': 'Both', 'points': pt}],
                  [{'account_id': 'nope', 'points': pt}], [{'name': 'Card 1234567', 'points': pt}],
                  [{'account_id': plan, 'points': [{'as_of': '2030-01-01', 'balance': 5}]}],
                  [{'account_id': plan, 'points': [{'as_of': '2029-02-01', 'balance': -5}]}],
                  [{'account_id': plan, 'points': []}]):
        r = admin.api('POST', '/investments/import', {'items': items})
        assert r.status_code in (400, 404, 422), (items, r.text)
    assert len(admin.api('GET', '/investments').json()['accounts']) == 1               # a refused import adds nothing
