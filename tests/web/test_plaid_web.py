"""The optional Plaid connection end to end against a fake Plaid (no network): off by default, the two switches,
linking, account roles, sync -> a waiting bank upload that reconciles, card purchases, pending, dedupe against a
file upload, sealed tokens, unlinking, wipe, and the Link page's own CSP."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hbudget.db import connection
from hbudget.io import plaid as pl
from hbudget.web.app import create_app
from hbudget.web.config import ConfigError, Settings
from webhelpers import TODAY, Client, add_user, login, make_settings

ACCOUNTS = [
    {'account_id': 'chk', 'name': 'Everyday Checking', 'mask': '1234', 'type': 'depository', 'subtype': 'checking',
     'balances': {'current': 1180.00}},
    {'account_id': 'cc', 'name': 'Rewards Card', 'mask': '9876', 'type': 'credit', 'subtype': 'credit card',
     'balances': {'current': 52.40}},
    {'account_id': 'sav', 'name': 'Savings', 'mask': '5555', 'type': 'depository', 'subtype': 'savings',
     'balances': {'current': 5000.00}},
]


def tx(tid: str, day: str, amount: float, desc: str, account: str = 'chk', pending: bool = False) -> dict[str, Any]:
    return {'transaction_id': tid, 'account_id': account, 'date': day, 'amount': amount, 'name': desc,
            'original_description': desc, 'pending': pending}


class FakePlaid:
    """Answers the five calls the app makes; `pages` is the next /transactions/sync answer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.accounts = [dict(a) for a in ACCOUNTS]
        self.pages: list[dict[str, Any]] = []
        self.fail: dict[str, dict[str, Any]] = {}
        self.liabilities: dict[str, Any] = {}

    def __call__(self, url: str, body: bytes) -> tuple[int, bytes]:
        assert url.startswith('https://sandbox.plaid.com/')
        path = url.removeprefix('https://sandbox.plaid.com')
        data = json.loads(body)
        assert data['client_id'] == 'cid' and data['secret'] == 'sec'
        self.calls.append((path, data))
        if path in self.fail:
            return 400, json.dumps(self.fail[path]).encode()
        out: dict[str, Any]
        if path == '/link/token/create':
            out = {'link_token': 'link-sandbox-1'}
        elif path == '/item/public_token/exchange':
            assert data['public_token'] == 'public-1'
            out = {'access_token': 'access-sandbox-SECRET', 'item_id': 'item-1'}
        elif path == '/accounts/get':
            out = {'accounts': self.accounts}
        elif path == '/transactions/sync':
            page = self.pages.pop(0) if self.pages else {}
            out = {'added': [], 'modified': [], 'removed': [], 'has_more': False,
                   'next_cursor': data.get('cursor', 'c0'), **page}
        elif path == '/liabilities/get':
            out = {'accounts': self.accounts, 'liabilities': self.liabilities}
        elif path in ('/item/remove', '/transactions/refresh'):
            out = {}
        else:
            raise AssertionError(path)
        return 200, json.dumps(out).encode()


@pytest.fixture
def fake() -> FakePlaid:
    return FakePlaid()


@pytest.fixture
def app(tmp_path: Path, fake: FakePlaid):  # type: ignore[no-untyped-def]
    a = create_app(make_settings(tmp_path, plaid='sandbox', plaid_client_id='cid', plaid_secret='sec',
                                 public_url='https://budget.example'))
    a.state.clock = lambda: TODAY
    a.state.plaid_client = pl.PlaidClient('sandbox', 'cid', 'sec', transport=fake)
    yield a
    a.state.stop.set()


def link(c: Client) -> dict[str, Any]:
    assert c.api('POST', '/plaid/enable', {'confirm': 'test HOUSEHOLD'}).status_code == 200
    assert c.api('POST', '/plaid/link-token', {'days': 180}).json() == {'link_token': 'link-sandbox-1'}
    r = c.api('POST', '/plaid/exchange', {'public_token': 'public-1', 'institution': 'First Test Bank'})
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------- off by default

def test_off_by_default_nothing_is_loaded_or_routed(tmp_path: Path) -> None:
    """With HBUDGET_PLAID unset the network module is never imported and there is no Plaid route."""
    code = ('import sys; from pathlib import Path; from hbudget.web.app import create_app; '
            'from hbudget.web.config import Settings; '
            'a = create_app(Settings(data_dir=Path(sys.argv[1]))); a.state.stop.set(); '
            'paths = {getattr(r, "path", "") for r in a.routes}; '
            'assert "hbudget.io.plaid" not in sys.modules, "plaid imported"; '
            'assert not any("plaid" in p or "connections" in p for p in paths), "plaid routed"; '
            'assert "cryptography" not in sys.modules, "crypto imported"')
    r = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, text=True, check=False,
                       timeout=60)
    assert r.returncode == 0, r.stderr[-500:]


def test_off_household_pages_say_nothing_leaves(tmp_path: Path) -> None:
    from webhelpers import page_csrf, setup_admin
    a = create_app(make_settings(tmp_path))
    try:
        with Client(a, base_url='http://testserver') as c:
            setup_admin(c, tmp_path)
            page_csrf(c, '/h/1/more')
            more = c.get('/h/1/more').text
            assert 'Bank connection' not in more and 'nothing leaves it' in more
            assert 'Bank connection' not in c.get('/h/1/rules').text
            assert c.get('/h/1/connections').status_code == 404
            r = c.get('/api/h/1/plaid')
            assert r.status_code == 404
            assert 'cdn.plaid.com' not in c.get('/h/1/settings').headers['content-security-policy']
    finally:
        a.state.stop.set()


def test_refresh_setting_needs_plaid_on(tmp_path: Path) -> None:
    env = {'HBUDGET_DATA_DIR': str(tmp_path), 'HBUDGET_PLAID_REFRESH': 'true'}
    assert Settings.from_env(env).plaid_refresh is False                           # Plaid off: nothing to refresh
    on = {**env, 'HBUDGET_PLAID': 'sandbox', 'HBUDGET_PLAID_CLIENT_ID': 'cid', 'HBUDGET_PLAID_SECRET': 'sec'}
    assert Settings.from_env(on).plaid_refresh is True
    assert Settings.from_env({**on, 'HBUDGET_PLAID_REFRESH': ''}).plaid_refresh is False


def test_config_needs_both_credentials(tmp_path: Path) -> None:
    assert Settings.from_env({}).plaid == 'off'
    with pytest.raises(ConfigError, match='CLIENT_ID'):
        Settings.from_env({'HBUDGET_PLAID': 'production'})
    with pytest.raises(ConfigError, match='SECRET'):
        Settings.from_env({'HBUDGET_PLAID': 'production', 'HBUDGET_PLAID_CLIENT_ID': 'x'})
    with pytest.raises(ConfigError, match='off, sandbox or production'):
        Settings.from_env({'HBUDGET_PLAID': 'development'})
    secret = tmp_path / 'plaid_secret'
    secret.write_text('from-file\n')
    s = Settings.from_env({'HBUDGET_PLAID': 'sandbox', 'HBUDGET_PLAID_CLIENT_ID': 'x',
                           'HBUDGET_PLAID_SECRET_FILE': str(secret)})
    assert s.plaid_secret == 'from-file' and 'from-file' not in repr(s)
    with pytest.raises(ConfigError, match='not both'):
        Settings.from_env({'HBUDGET_PLAID': 'sandbox', 'HBUDGET_PLAID_CLIENT_ID': 'x', 'HBUDGET_PLAID_SECRET': 'a',
                           'HBUDGET_PLAID_SECRET_FILE': str(secret)})
    # a secret in the environment is ignored while Plaid is off
    assert Settings.from_env({'HBUDGET_PLAID_SECRET': 'a'}).plaid_secret is None
    assert Settings(public_url='http://lan:8080').plaid_redirect_uri is None
    assert Settings(public_url='https://b.example/').plaid_redirect_uri == 'https://b.example/plaid/oauth'


# ---------------------------------------------------------------- on

def test_the_household_switch_needs_an_owner_and_the_name(admin: Client, app: Any, tmp_path: Path) -> None:
    s = admin.api('GET', '/plaid').json()
    assert (s['server'], s['enabled'], s['items'], s['oauth']) == ('sandbox', False, [], True)
    assert admin.api('POST', '/plaid/link-token', {}).status_code == 409          # off for this household
    assert admin.api('POST', '/plaid/enable', {'confirm': 'nope'}).status_code == 400
    add_user(tmp_path, admin.hid, 'ed', 'editor')
    ed = login(app, 'ed')
    ed.hid, ed.csrf = admin.hid, admin.csrf
    from webhelpers import page_csrf
    ed.csrf = page_csrf(ed, '/h/%d/bills' % admin.hid)
    assert ed.api('POST', '/plaid/enable', {'confirm': 'Test household'}).status_code == 403
    assert admin.api('POST', '/plaid/enable', {'confirm': ' test household '}).json()['enabled'] is True


def test_link_stores_a_sealed_token_and_default_roles(admin: Client, fake: FakePlaid, tmp_path: Path) -> None:
    s = link(admin)
    it = s['items'][0]
    assert it['institution'] == 'First Test Bank'
    assert {a['account_id']: a['role'] for a in it['accounts']} == {'chk': 'bank', 'cc': 'card', 'sav': 'ignore'}
    assert fake.calls[0][1]['redirect_uri'] == 'https://budget.example/plaid/oauth'
    assert fake.calls[0][1]['transactions'] == {'days_requested': 180}
    raw = (tmp_path / 'hbudget.db').read_bytes()
    for f in tmp_path.glob('hbudget.db*'):
        raw += f.read_bytes()
    assert b'access-sandbox-SECRET' not in raw
    assert 'SECRET' not in json.dumps(s)
    # one reconciled account: choosing savings moves the role
    s = admin.api('POST', '/plaid/accounts/sav/role', {'role': 'bank'}).json()
    assert {a['account_id']: a['role'] for a in s['items'][0]['accounts']}['chk'] == 'ignore'
    assert admin.api('POST', '/plaid/accounts/nope/role', {'role': 'bank'}).status_code == 404


def test_sync_makes_a_waiting_upload_that_reconciles(admin: Client, fake: FakePlaid, app: Any) -> None:
    # set_start: $1,000.00 on TODAY (2029-03-01); Plaid's current balance is $1,180.00
    link(admin)
    assert admin.api('GET', '/checkin').json()['plaid'] is True
    # p0 is the posting that left the checkbook at its start balance: Plaid's history reaches back past it
    fake.pages = [{'added': [tx('p0', '2029-02-27', 10.00, 'PHARMACY'),
                             tx('p1', '2029-03-02', 20.00, 'CORNER CAFE'),
                             tx('p2', '2029-03-03', -250.00, 'EMPLOYER PAY'),
                             tx('p3', '2029-03-04', 50.00, 'GAS STATION'),
                             tx('p4', '2029-03-05', 9.99, 'PENDING THING', pending=True),
                             tx('c1', '2029-03-03', 12.50, 'BOOK SHOP', account='cc'),
                             tx('s1', '2029-03-03', -1.00, 'INTEREST', account='sav')],
                   'next_cursor': 'c1'}]
    app.state.clock = lambda: date(2029, 3, 6)
    r = admin.api('POST', '/plaid/sync', {})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out['added'], out['pending'], out['problems']) == (6, 1, [])
    bank = out['bank']
    assert bank['status'] == 'waiting' and bank['rows'] == 4 and bank['has_balance'] is True
    assert bank['latest_balance'] == 118000 and 'Balance checked' in bank['note']
    assert [c['rows'] for c in out['cards']] == [1]
    p = admin.api('POST', '/reconcile/preview', {'upload_id': bank['id']}).json()
    assert p['latest_balance'] == 118000 and p['basis']['description'] == 'PHARMACY'
    assert admin.api('POST', '/reconcile/apply', {'upload_id': bank['id'], 'accept': {}}).status_code == 200
    assert admin.api('GET', '/card').json()['summary']['monthly_spend'] == 1250      # the card purchase
    # the cursor is kept; the next sync sends it and nothing new makes no upload
    fake.pages = [{'next_cursor': 'c2'}]
    again = admin.api('POST', '/plaid/sync', {}).json()
    assert again['bank'] is None and again['cards'] == [] and again['added'] == 0
    assert [d.get('cursor') for p_, d in fake.calls if p_ == '/transactions/sync'] == [None, 'c1']
    # a new posting next week: the upload reaches back a week and the balance chain still lines up
    fake.accounts[0]['balances'] = {'current': 1150.00}
    fake.pages = [{'added': [tx('p5', '2029-03-09', 30.00, 'HARDWARE')], 'next_cursor': 'c3'}]
    nxt = admin.api('POST', '/plaid/sync', {}).json()['bank']
    assert nxt['has_balance'] is True and nxt['rows'] == 5
    p = admin.api('POST', '/reconcile/preview', {'upload_id': nxt['id']}).json()
    assert p['latest_balance'] == 115000
    log = [a for a in admin.api('GET', '/checkin').json()['changes']]
    assert isinstance(log, list)


def test_a_balance_that_does_not_line_up_falls_back_to_dates(admin: Client, fake: FakePlaid) -> None:
    link(admin)
    fake.accounts[0]['balances'] = {'current': 777.77}
    fake.pages = [{'added': [tx('p1', '2029-03-02', 20.00, 'CORNER CAFE')]}]
    bank = admin.api('POST', '/plaid/sync', {}).json()['bank']
    assert bank['has_balance'] is False and 'does not line up' in bank['note']
    p = admin.api('POST', '/reconcile/preview', {'upload_id': bank['id']}).json()
    assert p['latest_balance'] == 100000 - 2000


def test_postings_from_an_earlier_file_are_not_counted_twice(admin: Client, fake: FakePlaid) -> None:
    from test_reconcile_flow import upload
    csv = (b'Date,Description,Amount,Balance\n2029-03-02,CORNER CAFE #12,-20.00,980.00\n'
           b'2029-02-27,PHARMACY,-10.00,1000.00\n')
    up = upload(admin, 'bank.csv', csv).json()
    r = admin.api('POST', '/reconcile/apply', {'upload_id': up['id'], 'accept': {}})
    assert r.status_code == 200, r.text
    link(admin)
    fake.accounts[0]['balances'] = {'current': 930.00}
    fake.pages = [{'added': [tx('p1', '2029-03-02', 20.00, 'Corner Cafe'),
                             tx('p2', '2029-03-03', 50.00, 'GAS STATION')]}]
    bank = admin.api('POST', '/plaid/sync', {}).json()['bank']
    assert bank['has_balance'] is True
    p = admin.api('POST', '/reconcile/preview', {'upload_id': bank['id']}).json()
    assert p['latest_balance'] == 93000 and p['basis']['description'] == 'CORNER CAFE #12'
    hist = admin.api('GET', '/recap').status_code
    assert hist == 200


def test_problems_are_reported_and_sign_in_again_is_offered(admin: Client, fake: FakePlaid) -> None:
    link(admin)
    fake.fail['/transactions/sync'] = {'error_code': 'ITEM_LOGIN_REQUIRED', 'display_message': 'Sign in again'}
    out = admin.api('POST', '/plaid/sync', {}).json()
    assert out['problems'] == [{'item_id': 'item-1', 'institution': 'First Test Bank', 'message': 'Sign in again',
                                'relink': True}]
    assert admin.api('GET', '/plaid').json()['items'][0]['last_error'] == 'Sign in again'
    del fake.fail['/transactions/sync']
    r = admin.api('POST', '/plaid/link-token', {'item_id': 'item-1'})
    assert r.status_code == 200
    assert fake.calls[-1][1]['access_token'] == 'access-sandbox-SECRET' and 'products' not in fake.calls[-1][1]
    assert admin.api('POST', '/plaid/items/item-1/relinked', {}).json()['items'][0]['last_error'] == ''


def test_a_balance_gap_is_explained_instead_of_nothing_new(admin: Client, fake: FakePlaid, app: Any,
                                                          tmp_path: Path) -> None:
    """The bank's balance runs ahead of what Plaid has sent as posted: say so, and why."""
    link(admin)                                                   # the checkbook starts at $1,000.00
    def count() -> int:
        conn = connection.connect(tmp_path / 'hbudget.db')
        try:
            return int(conn.execute('SELECT COUNT(*) FROM plaid_pending').fetchone()[0])
        finally:
            conn.close()
    # 1. a deposit is in the bank's balance, but Plaid has sent nothing at all yet
    fake.accounts[0]['balances'] = {'current': 1250.00}
    out = admin.api('POST', '/plaid/sync', {}).json()
    bal = out['balance']
    assert out['bank'] is None and out['added'] == 0
    assert (bal['bank'], bal['checkbook'], bal['ahead'], bal['reason']) == (125000, 100000, 25000, 'unexplained')
    assert bal['message'].startswith('The bank shows $1,250.00, $250.00 more than the checkbook ($1,000.00).')
    assert 'not been sent yet' in bal['message'] and 'pending' not in bal['message']
    assert admin.api('GET', '/plaid').json()['balance'] == bal    # the Bank connection page says the same
    assert admin.api('GET', '/checkin').json()['plaid_balance'] == bal
    # 2. Plaid sends it as pending: it makes up the difference, and it is kept (amount and date only)
    fake.pages = [{'added': [tx('pend1', '2029-03-01', -250.00, 'EMPLOYER PAY', pending=True),
                             tx('pend2', '2029-03-01', 5.00, 'CARD HOLD', account='cc', pending=True)]}]
    out = admin.api('POST', '/plaid/sync', {}).json()
    bal = out['balance']
    assert out['bank'] is None and (bal['reason'], bal['pending'], bal['pending_total']) == ('pending', 1, 25000)
    assert 'The pending transaction makes up the difference' in bal['message'] and count() == 2
    # a pending transaction two weeks old no longer explains anything
    app.state.clock = lambda: date(2029, 3, 20)
    assert admin.api('GET', '/plaid').json()['balance']['reason'] == 'unexplained'
    app.state.clock = lambda: TODAY
    # 3. it posts: Plaid withdraws the pending one and sends the posting; the upload to review explains the gap
    fake.pages = [{'added': [tx('p1', '2029-03-01', -250.00, 'EMPLOYER PAY')], 'removed': [
        {'transaction_id': 'pend1'}]}]
    out = admin.api('POST', '/plaid/sync', {}).json()
    bal = out['balance']
    assert out['bank']['status'] == 'waiting' and (bal['reason'], bal['postings'], bal['pending']) == ('postings', 1, 0)
    assert 'The newest posting makes up the difference' in bal['message'] and count() == 1
    # 4. reviewed: the balances agree and there is nothing to say
    assert admin.api('POST', '/reconcile/apply', {'upload_id': out['bank']['id'], 'accept': {}}).status_code == 200
    bal = admin.api('POST', '/plaid/sync', {}).json()['balance']
    assert (bal['reason'], bal['ahead'], bal['message'], bal['checkbook']) == ('agree', 0, '', 125000)
    # the bank behind the checkbook reads "less than"
    fake.accounts[0]['balances'] = {'current': 1200.00}
    assert '$50.00 less than the checkbook ($1,250.00)' in admin.api('POST', '/plaid/sync', {}).json()['balance'][
        'message']
    # unlinking forgets the pending transactions too; without a linked checking account there is no balance
    admin.api('DELETE', '/plaid/items/item-1')
    assert count() == 0 and admin.api('GET', '/plaid').json()['balance'] is None


def test_the_api_and_mcp_answer_why_the_balances_differ(admin: Client, fake: FakePlaid, app: Any) -> None:
    from fastapi.testclient import TestClient
    link(admin)
    fake.accounts[0]['balances'] = {'current': 1250.00}
    fake.pages = [{'added': [tx('pend1', '2029-03-01', -250.00, 'EMPLOYER PAY', pending=True)]}]
    admin.api('POST', '/plaid/sync', {})
    secret = admin.api('POST', '/tokens', {'name': 'Agent', 'scope': 'read'}).json()['secret']
    h = {'Authorization': 'Bearer ' + secret}
    with TestClient(app, base_url='http://testserver') as anon:
        got = anon.get('/api/v1/bank-connection', headers=h).json()
        assert (got['available'], got['enabled'], got['auto_sync'], got['waiting_postings']) == (True, True, 'off', 0)
        bank = got['banks'][0]
        assert bank['institution'] == 'First Test Bank' and bank['last_sync_at'] and bank['problem'] is None
        assert {'name': 'Everyday Checking', 'mask': '1234', 'used_for': 'bank', 'balance': 125000,
                'keeps_debt': None} in bank['accounts']
        assert got['balance']['reason'] == 'pending' and got['balance']['ahead'] == 25000
        assert 'access' not in json.dumps(got) and 'item-1' not in json.dumps(got)      # no token, no Plaid ids
        r = anon.post('/mcp', headers=h, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'get_bank_connection', 'arguments': {}}}).json()['result']
        assert r['isError'] is False and r['structuredContent']['balance'] == got['balance']
        assert anon.get('/api/v1/bank-connection').status_code == 401


def test_refresh_from_the_bank_is_off_unless_the_server_turns_it_on(admin: Client, fake: FakePlaid, app: Any) -> None:
    from dataclasses import replace
    link(admin)
    asked = lambda: [p for p, _ in fake.calls if p == '/transactions/refresh']   # noqa: E731
    assert admin.api('GET', '/plaid').json()['refresh'] is False
    assert admin.api('GET', '/checkin').json()['plaid_refresh'] is False
    r = admin.api('POST', '/plaid/refresh', {})
    assert r.status_code == 409 and 'HBUDGET_PLAID_REFRESH' in r.text and asked() == []
    # turned on: the bank is asked first, then the normal sync brings the posting in
    app.state.settings = replace(app.state.settings, plaid_refresh=True)
    assert admin.api('GET', '/plaid').json()['refresh'] is True
    assert admin.api('GET', '/checkin').json()['plaid_refresh'] is True
    fake.accounts[0]['balances'] = {'current': 1250.00}
    fake.pages = [{'added': [tx('p1', '2029-03-01', -250.00, 'EMPLOYER PAY')]}]
    out = admin.api('POST', '/plaid/refresh', {}).json()
    assert (out['refreshed'], out['refresh_notes'], out['added']) == (1, [], 1)
    assert out['bank']['status'] == 'waiting' and out['balance']['reason'] == 'postings'
    paths = [p for p, _ in fake.calls]
    assert paths.index('/transactions/refresh') < len(paths) - 1 - paths[::-1].index('/transactions/sync')
    assert fake.calls[paths.index('/transactions/refresh')][1]['access_token'] == 'access-sandbox-SECRET'
    # once an hour per bank: the second press only syncs, and says so
    again = admin.api('POST', '/plaid/refresh', {}).json()
    assert again['refreshed'] == 0 and 'last hour' in again['refresh_notes'][0]['message'] and len(asked()) == 1
    assert 'refresh_notes' not in admin.api('POST', '/plaid/sync', {}).json()      # a plain sync never asks


def test_a_refused_refresh_says_why_and_does_not_use_up_the_hour(admin: Client, fake: FakePlaid, app: Any) -> None:
    from dataclasses import replace
    link(admin)
    app.state.settings = replace(app.state.settings, plaid_refresh=True)
    for code, words in (('PRODUCT_NOT_ENABLED', 'not part of this Plaid account'),
                        ('PRODUCTS_NOT_SUPPORTED', 'does not support'),
                        ('TRANSACTIONS_REFRESH_LIMIT', 'too often'),
                        ('INTERNAL_SERVER_ERROR', 'did not work: boom')):
        fake.fail['/transactions/refresh'] = {'error_code': code, 'error_message': 'boom'}
        out = admin.api('POST', '/plaid/refresh', {}).json()
        assert out['refreshed'] == 0 and words in out['refresh_notes'][0]['message'], code
        assert out['refresh_notes'][0]['institution'] == 'First Test Bank' and out['problems'] == []   # the sync ran
    del fake.fail['/transactions/refresh']
    assert admin.api('POST', '/plaid/refresh', {}).json()['refreshed'] == 1        # not made to wait an hour


def test_a_refresh_with_no_answer_keeps_the_hour(admin: Client, fake: FakePlaid, app: Any) -> None:
    """No answer from Plaid may still mean the refresh was done and charged: do not offer a second one at once."""
    from dataclasses import replace
    link(admin)
    app.state.settings = replace(app.state.settings, plaid_refresh=True)
    fake.fail['/transactions/refresh'] = {}                        # an error status with no Plaid error code
    out = admin.api('POST', '/plaid/refresh', {}).json()
    assert out['refreshed'] == 0 and 'may not have gone through' in out['refresh_notes'][0]['message']
    del fake.fail['/transactions/refresh']
    again = admin.api('POST', '/plaid/refresh', {}).json()
    assert again['refreshed'] == 0 and 'last hour' in again['refresh_notes'][0]['message']
    assert len([p for p, _ in fake.calls if p == '/transactions/refresh']) == 1


def test_an_agent_can_sync_and_refresh(admin: Client, fake: FakePlaid, app: Any) -> None:
    from dataclasses import replace

    from fastapi.testclient import TestClient
    link(admin)

    def bearer(name: str, scope: str) -> dict[str, str]:
        return {'Authorization': 'Bearer ' + admin.api('POST', '/tokens', {'name': name, 'scope': scope}).json()[
            'secret']}
    rw, ro = bearer('Sync', 'write'), bearer('Look', 'read')

    def call(anon: TestClient, h: dict[str, str], **args: Any) -> Any:
        r = anon.post('/mcp', headers=h, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'sync_bank', 'arguments': args}}).json()
        return r['result'] if 'result' in r else r['error']
    with TestClient(app, base_url='http://testserver') as anon:
        assert 'structuredContent' not in call(anon, ro)                          # a read token cannot sync
        fake.accounts[0]['balances'] = {'current': 1250.00}
        fake.pages = [{'added': [tx('p1', '2029-03-01', -250.00, 'EMPLOYER PAY'),
                                 tx('c1', '2029-03-01', 12.50, 'BOOK SHOP', account='cc')]}]
        out = call(anon, rw)['structuredContent']
        assert (out['added'], out['card_uploads'], out['problems']) == (2, 1, [])
        assert out['bank_upload']['rows'] == 1 and out['balance']['reason'] == 'postings' and 'refreshed' not in out
        assert anon.get('/api/v1/bank-connection', headers=ro).json()['can_refresh'] is False
        off = call(anon, rw, refresh=True)
        assert off['isError'] is True and 'HBUDGET_PLAID_REFRESH' in off['content'][0]['text']
        app.state.settings = replace(app.state.settings, plaid_refresh=True)
        assert anon.get('/api/v1/bank-connection', headers=ro).json()['can_refresh'] is True
        r = anon.post('/api/v1/bank-connection/sync', headers=rw, json={'refresh': True})
        assert r.status_code == 200 and r.json()['refreshed'] == 1 and r.json()['bank_upload']['rows'] == 1
        assert anon.post('/api/v1/bank-connection/sync', headers=rw).json()['added'] == 0     # no body: a plain sync
        assert anon.post('/api/v1/bank-connection/sync', headers=ro).status_code == 403
    said = [c['what'] for c in admin.api('GET', '/checkin').json()['changes']]
    assert 'asked the bank for a refresh' in said and 'synced with the bank' in said


def test_unlink_and_turn_off_tell_plaid_and_keep_history(admin: Client, fake: FakePlaid) -> None:
    link(admin)
    fake.pages = [{'added': [tx('p1', '2029-03-02', 20.00, 'CORNER CAFE')]}]
    admin.api('POST', '/plaid/sync', {})
    s = admin.api('DELETE', '/plaid/items/item-1').json()
    assert s['items'] == [] and fake.calls[-1] == ('/item/remove', {'client_id': 'cid', 'secret': 'sec',
                                                                    'access_token': 'access-sandbox-SECRET'})
    assert admin.api('POST', '/plaid/sync', {}).status_code == 409
    link(admin)
    fake.fail['/item/remove'] = {'error_code': 'INTERNAL_SERVER_ERROR', 'error_message': 'x'}
    s = admin.api('POST', '/plaid/disable', {}).json()
    assert s['enabled'] is False and 'could not be told' in s['note']
    assert admin.api('GET', '/checkin').json()['plaid'] is False
    assert admin.api('GET', '/uploads').json()['uploads'][0]['filename'].startswith('Plaid')


def test_wipe_keeps_the_link_and_starts_the_history_over(admin: Client, fake: FakePlaid, tmp_path: Path) -> None:
    from hbudget.db import repo
    link(admin)
    fake.pages = [{'added': [tx('p1', '2029-03-02', 20.00, 'CORNER CAFE'),
                             tx('p2', '2029-03-02', 5.00, 'HOLD', pending=True)], 'next_cursor': 'c1'}]
    admin.api('POST', '/plaid/sync', {})
    conn = connection.connect(tmp_path / 'hbudget.db')
    try:
        assert conn.execute('SELECT COUNT(*) FROM plaid_pending').fetchone()[0] == 1
        repo.wipe_household(conn, admin.hid)
        assert conn.execute('SELECT COUNT(*) FROM plaid_txns').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM plaid_pending').fetchone()[0] == 0
        assert conn.execute('SELECT cursor FROM plaid_items').fetchone()[0] is None
    finally:
        conn.close()
    assert admin.api('GET', '/plaid').json()['items'][0]['item_id'] == 'item-1'


# ---------------------------------------------------------------- the daily sync

DAY1 = datetime(2029, 3, 3, 6, 0, tzinfo=UTC)


def bank_waiting(c: Client) -> list[dict[str, Any]]:
    return [u for u in c.api('GET', '/uploads').json()['uploads'] if u['kind'] == 'bank' and u['status'] == 'waiting']


def test_daily_sync_is_off_by_default_and_set_by_an_owner(admin: Client, app: Any, fake: FakePlaid,
                                                          tmp_path: Path) -> None:
    from hbudget.web.routes import plaid
    link(admin)
    assert admin.api('GET', '/plaid').json()['auto_sync'] == 'off'
    assert plaid.auto_sync(app, DAY1) == 0 and not any(p_ == '/transactions/sync' for p_, _ in fake.calls)
    add_user(tmp_path, admin.hid, 'ed', 'editor')
    ed = login(app, 'ed')
    ed.hid = admin.hid
    from webhelpers import page_csrf
    ed.csrf = page_csrf(ed, '/h/%d/bills' % admin.hid)
    assert ed.api('POST', '/plaid/auto-sync', {'value': 'daily'}).status_code == 403
    assert admin.api('POST', '/plaid/auto-sync', {'value': 'hourly'}).status_code == 400
    assert admin.api('POST', '/plaid/auto-sync', {'value': 'daily'}).json()['auto_sync'] == 'daily'
    assert plaid.auto_sync(app, DAY1) == 1
    assert plaid.auto_sync(app, DAY1 + timedelta(hours=5)) == 0               # once a day, restarts included
    assert plaid.auto_sync(app, DAY1 + timedelta(hours=21)) == 1
    admin.api('POST', '/plaid/sync', {})                                      # a sync by hand counts as today's
    assert plaid.auto_sync(app, datetime.now(UTC) + timedelta(hours=2)) == 0
    assert admin.api('POST', '/plaid/auto-sync', {'value': 'off'}).json()['auto_sync'] == 'off'
    assert plaid.auto_sync(app, datetime.now(UTC) + timedelta(days=3)) == 0


def test_days_away_mean_one_upload_to_review(admin: Client, app: Any, fake: FakePlaid) -> None:
    """The daily sync only stores; nothing reaches the budget until the check-in brings it in, as one bank upload
    however many days it covers, and the reconciliation lines up across all of them."""
    from hbudget.web.routes import plaid
    link(admin)
    admin.api('POST', '/plaid/auto-sync', {'value': 'daily'})
    fake.accounts[0]['balances'] = {'current': 1180.00}
    fake.pages = [{'added': [tx('p0', '2029-02-27', 10.00, 'PHARMACY'), tx('p1', '2029-03-02', 20.00, 'CORNER CAFE'),
                             tx('c1', '2029-03-02', 12.50, 'BOOK SHOP', account='cc')], 'next_cursor': 'c1'},
                  {'added': [tx('p2', '2029-03-03', -250.00, 'EMPLOYER PAY'),
                             tx('p9', '2029-03-04', 5.00, 'SOON POSTED', pending=True)], 'next_cursor': 'c2'},
                  {'added': [tx('p3', '2029-03-04', 50.00, 'GAS STATION'),
                             tx('c2', '2029-03-04', 7.00, 'SNACKS', account='cc')], 'next_cursor': 'c3'}]
    for day in range(3):
        assert plaid.auto_sync(app, DAY1 + timedelta(days=day)) == 1
    assert admin.api('GET', '/uploads').json()['uploads'] == []                # nothing reached the budget
    c = admin.api('GET', '/checkin').json()
    assert c['plaid_new'] == 6 and c['plaid_problems'] == [] and c['bank']['waiting'] == []
    assert not [x for x in c['changes'] if 'sync' in x['what']]               # the daily sync is not news
    app.state.clock = lambda: date(2029, 3, 6)
    out = admin.api('POST', '/plaid/sync', {}).json()                          # "Bring them in"
    assert out['bank']['rows'] == 4 and out['bank']['has_balance'] is True
    assert [c_['rows'] for c_ in out['cards']] == [2]
    assert admin.api('GET', '/checkin').json()['plaid_new'] == 0
    p = admin.api('POST', '/reconcile/preview', {'upload_id': out['bank']['id']}).json()
    assert p['latest_balance'] == 118000 and p['basis']['description'] == 'PHARMACY'
    assert admin.api('POST', '/reconcile/apply', {'upload_id': out['bank']['id'], 'accept': {}}).status_code == 200
    assert bank_waiting(admin) == []


def test_a_waiting_upload_is_replaced_not_stacked(admin: Client, app: Any, fake: FakePlaid) -> None:
    """Synced in the check-in but not reviewed, then days of daily syncs: still one waiting upload, holding every
    posting since the last reconciliation and none the bank withdrew."""
    from hbudget.web.routes import plaid
    link(admin)
    admin.api('POST', '/plaid/auto-sync', {'value': 'daily'})
    fake.pages = [{'added': [tx('p0', '2029-02-27', 10.00, 'PHARMACY'), tx('p1', '2029-03-02', 20.00, 'CORNER CAFE'),
                             tx('px', '2029-03-02', 3.00, 'DOUBLE CHARGE')], 'next_cursor': 'c1'}]
    fake.accounts[0]['balances'] = {'current': 977.00}
    first = admin.api('POST', '/plaid/sync', {}).json()['bank']
    assert first['rows'] == 3 and first['has_balance'] is True
    # nothing new: the same upload comes back, no second one
    fake.pages = [{'next_cursor': 'c1'}]
    assert admin.api('POST', '/plaid/sync', {}).json()['bank']['id'] == first['id']
    # the next days: a new posting, and the bank withdraws the double charge
    fake.accounts[0]['balances'] = {'current': 930.00}
    fake.pages = [{'added': [tx('p2', '2029-03-04', 50.00, 'GAS STATION')], 'removed': [{'transaction_id': 'px'}],
                   'next_cursor': 'c2'}]
    assert plaid.auto_sync(app, datetime.now(UTC) + timedelta(days=1)) == 1
    assert admin.api('GET', '/checkin').json()['plaid_new'] == 1
    assert [u['id'] for u in bank_waiting(admin)] == [first['id']]             # untouched until the check-in
    fake.pages = [{'next_cursor': 'c2'}]
    second = admin.api('POST', '/plaid/sync', {}).json()['bank']
    assert second['id'] != first['id'] and second['rows'] == 3 and second['has_balance'] is True
    assert [u['id'] for u in bank_waiting(admin)] == [second['id']]
    assert admin.api('POST', '/reconcile/preview', {'upload_id': first['id']}).status_code == 404
    p = admin.api('POST', '/reconcile/preview', {'upload_id': second['id']}).json()
    assert p['latest_balance'] == 93000
    assert admin.api('POST', '/reconcile/apply', {'upload_id': second['id'], 'accept': {}}).status_code == 200


def test_daily_sync_problems_show_in_the_checkin(admin: Client, app: Any, fake: FakePlaid) -> None:
    from hbudget.web.routes import plaid
    link(admin)
    admin.api('POST', '/plaid/auto-sync', {'value': 'daily'})
    fake.fail['/transactions/sync'] = {'error_code': 'ITEM_LOGIN_REQUIRED', 'display_message': 'Sign in again'}
    assert plaid.auto_sync(app, DAY1) == 1
    assert admin.api('GET', '/checkin').json()['plaid_problems'] == [{'institution': 'First Test Bank',
                                                                      'message': 'Sign in again'}]


def test_only_the_link_pages_load_plaid(admin: Client) -> None:
    page = admin.get('/h/%d/connections' % admin.hid)
    assert page.status_code == 200
    csp = page.headers['content-security-policy']
    assert pl.HOSTS['sandbox'] in csp and 'https://cdn.plaid.com/link/v2/stable/link-initialize.js' in csp
    assert page.headers['cross-origin-opener-policy'] == 'same-origin-allow-popups'
    assert '<script src="https://cdn.plaid.com' not in page.text      # fetched by the page only when Link starts
    assert 'link-initialize.js' in page.text                            # (its address is in the page data)
    assert 'cdn.plaid.com' not in admin.get('/h/%d/bills' % admin.hid).headers['content-security-policy']
    assert admin.get('/plaid/oauth?oauth_state_id=abc').status_code == 200
    more = admin.get('/h/%d/more' % admin.hid).text
    assert 'Bank connection' in more and 'fetches transactions from Plaid' in more
    # a chip in the More row, after Settings, current on its own page
    chips = admin.get('/h/%d/rules' % admin.hid).text
    assert chips.index('>Settings</a>') < chips.index('/connections">Bank connection</a>') < chips.index('>Notes</a>')
    assert '/connections" aria-current="page">Bank connection</a>' in page.text


def test_signed_out_oauth_return_goes_to_login(app: Any) -> None:
    with Client(app, base_url='http://testserver') as c:
        r = c.get('/plaid/oauth', follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'] == '/login'


# ---------------------------------------------------------------- investment balances (D-102)

def test_a_linked_account_keeps_an_investment_balance(admin: Client, app: Any, fake: FakePlaid) -> None:
    from hbudget.web.routes import plaid

    def role(account_id: str) -> str:
        accts = admin.api('GET', '/plaid').json()['items'][0]['accounts']
        return str({x['account_id']: x['role'] for x in accts}[account_id])
    fake.accounts.append({'account_id': 'ira', 'name': 'Rollover IRA 123456789', 'mask': '4242',
                          'type': 'investment', 'subtype': 'ira', 'balances': {'current': 20500.25}})
    s = link(admin)
    roles = {a['account_id']: a['role'] for a in s['items'][0]['accounts']}
    assert roles['ira'] == 'ignore'                       # never tracked unless the owner says so
    assert admin.api('GET', '/investments').json()['accounts'] == []
    s = admin.api('POST', '/plaid/accounts/ira/role', {'role': 'invest'}).json()
    assert {a['account_id']: a['role'] for a in s['items'][0]['accounts']}['ira'] == 'invest'
    [inv] = admin.api('GET', '/investments').json()['accounts']
    assert (inv['name'], inv['balance'], inv['as_of'], inv['source']) == (
        'First Test Bank Rollover IRA', 2050025, TODAY.isoformat(), 'bank')      # the long number is left out
    assert inv['bank']['institution'] == 'First Test Bank' and not inv['stale']
    # a sync notes the day's balance; the transactions of the account are not brought in
    fake.accounts[-1]['balances']['current'] = 20700.00
    fake.pages = [{'added': [tx('i1', '2029-02-27', 500.00, 'BUY FUND', 'ira')], 'next_cursor': 'c1'}]
    r = admin.api('POST', '/plaid/sync').json()
    assert r['investments'] == 1 and r['bank'] is None and r['cards'] == []
    [inv] = admin.api('GET', '/investments').json()['accounts']
    assert inv['balance'] == 2070000 and inv['history'] == [[TODAY.isoformat(), 2070000]]   # one a day: the later
    assert role('ira') == 'invest'
    # the daily sync notes it too, with nobody signed in, and adds no audit entry of its own per balance
    admin.api('POST', '/plaid/auto-sync', {'value': 'daily'})
    fake.accounts[-1]['balances']['current'] = 21000.00
    app.state.clock = lambda: TODAY + timedelta(days=1)
    assert plaid.auto_sync(app, datetime.now(UTC) + timedelta(days=1)) == 1
    [inv] = admin.api('GET', '/investments').json()['accounts']
    assert inv['history'] == [[TODAY.isoformat(), 2070000], [(TODAY + timedelta(days=1)).isoformat(), 2100000]]
    assert inv['change']['all']['amount'] == 30000
    # switched off: the account and its history stay, and stop moving; on again: the same account, no second one
    admin.api('POST', '/plaid/accounts/ira/role', {'role': 'ignore'})
    fake.accounts[-1]['balances']['current'] = 30000.00
    assert admin.api('POST', '/plaid/sync').json()['investments'] == 0
    [inv] = admin.api('GET', '/investments').json()['accounts']
    assert (inv['balance'], inv['source']) == (2100000, 'typed')
    admin.api('POST', '/plaid/accounts/ira/role', {'role': 'invest'})
    [inv] = admin.api('GET', '/investments').json()['accounts']
    assert (inv['balance'], inv['source']) == (3000000, 'bank')
    # deleted on the Investments page: the next sync stops keeping it and makes no new account
    assert admin.api('DELETE', '/investments/%s' % inv['id']).status_code == 200
    assert admin.api('POST', '/plaid/sync').json()['investments'] == 0
    assert admin.api('GET', '/investments').json()['accounts'] == []
    assert role('ira') == 'ignore'
    # the reconciled checking account can be turned into a tracked one: no bank account is left
    admin.api('POST', '/plaid/accounts/chk/role', {'role': 'invest'})
    roles = {a['account_id']: a['role'] for a in admin.api('GET', '/plaid').json()['items'][0]['accounts']}
    assert roles['chk'] == 'invest' and 'bank' not in roles.values()


def test_a_linked_card_keeps_its_debts_balance(admin: Client, app: Any, fake: FakePlaid) -> None:
    """D-105: whatever the account is used for stays; every sync writes what the bank says is owed."""
    from hbudget.web.routes import plaid

    did = admin.api('POST', '/accounts', {'name': 'Rewards card', 'kind': 'credit_card', 'balance': 90000,
                                          'apr_bp': 1999, 'payment': 5000, 'as_of': '2028-11-01'}).json()['id']
    s = link(admin)
    assert s['debts'] == [{'id': did, 'name': 'Rewards card'}]
    assert {a['account_id']: a['debt'] for a in s['items'][0]['accounts']}['cc'] is None
    assert admin.api('POST', '/plaid/accounts/cc/debt', {'debt': 'nope'}).status_code == 400
    admin.api('POST', '/plaid/accounts/cc/role', {'role': 'card'})
    s = admin.api('POST', '/plaid/accounts/cc/debt', {'debt': did}).json()
    cc = next(a for a in s['items'][0]['accounts'] if a['account_id'] == 'cc')
    assert (cc['role'], cc['debt']) == ('card', did)

    def debt() -> dict[str, Any]:
        d = admin.api('GET', '/accounts').json()
        return {**next(a for a in d['accounts'] if a['id'] == did), 'stale_ids': d['reminder']['stale']}
    d = debt()                                  # set at once, the typed balance goes to the history
    assert (d['balance'], d['as_of'], d['source'], d['bank']['institution']) == (
        5240, TODAY.isoformat(), 'bank', 'First Test Bank')
    assert d['history'] == [['2028-11-01', 90000], [TODAY.isoformat(), 5240]] and d['stale_ids'] == []
    fake.accounts[1]['balances']['current'] = 61.00
    r = admin.api('POST', '/plaid/sync').json()
    assert r['debts'] == 1 and debt()['balance'] == 6100
    # the daily sync does it too; a credit on the card (a negative balance) is nothing owed
    admin.api('POST', '/plaid/auto-sync', {'value': 'daily'})
    fake.accounts[1]['balances']['current'] = -12.00
    app.state.clock = lambda: TODAY + timedelta(days=1)
    assert plaid.auto_sync(app, datetime.now(UTC) + timedelta(days=1)) == 1
    d = debt()
    assert (d['balance'], d['as_of']) == (0, (TODAY + timedelta(days=1)).isoformat())
    # stopped: the debt keeps its balance and is the household's to update again
    admin.api('POST', '/plaid/accounts/cc/debt', {'debt': None})
    fake.accounts[1]['balances']['current'] = 75.00
    assert admin.api('POST', '/plaid/sync').json()['debts'] == 0 and debt()['source'] == 'typed'
    # a deleted debt: the next sync stops keeping it
    admin.api('POST', '/plaid/accounts/cc/debt', {'debt': did})
    assert admin.api('DELETE', '/accounts/%s' % did).status_code == 200
    assert admin.api('POST', '/plaid/sync').json()['debts'] == 0
    s = admin.api('GET', '/plaid').json()
    assert next(a for a in s['items'][0]['accounts'] if a['account_id'] == 'cc')['debt'] is None


def test_liabilities_are_off_unless_the_server_and_the_owner_turn_them_on(admin: Client, app: Any,
                                                                         fake: FakePlaid) -> None:
    """D-106: a paid Plaid product. Nothing asks for it until the server allows it and the owner turned it on for
    the bank; then every sync reads the rate, the minimum and the due date."""
    did = admin.api('POST', '/accounts', {'name': 'Rewards card', 'kind': 'credit_card', 'balance': 90000,
                                          'apr_bp': 1500, 'payment': 5000}).json()['id']
    promo = admin.api('POST', '/accounts', {'name': 'Store card', 'kind': 'credit_card', 'balance': 40000,
                                            'apr_bp': 0, 'payment': 5000, 'promo_ends': '2029-09-01'}).json()['id']
    fake.accounts.append({'account_id': 'store', 'name': 'Store Card', 'mask': '1111', 'type': 'credit',
                          'subtype': 'credit card', 'balances': {'current': 400.00}})
    fake.liabilities = {'credit': [
        {'account_id': 'cc', 'aprs': [{'apr_type': 'cash_apr', 'apr_percentage': 27.99},
                                      {'apr_type': 'purchase_apr', 'apr_percentage': 19.99}],
         'minimum_payment_amount': 35.0, 'next_payment_due_date': '2029-03-20', 'last_statement_balance': 60.25},
        {'account_id': 'store', 'aprs': [{'apr_type': 'purchase_apr', 'apr_percentage': 29.99}],
         'minimum_payment_amount': 25, 'next_payment_due_date': None, 'last_statement_balance': None},
        {'account_id': None, 'aprs': []}], 'mortgage': [], 'student': []}
    s = link(admin)
    assert s['liabilities'] is False and s['items'][0]['liabilities'] is False
    admin.api('POST', '/plaid/accounts/cc/debt', {'debt': did})
    admin.api('POST', '/plaid/accounts/store/debt', {'debt': promo})

    def asked() -> int:
        return sum(1 for path, _ in fake.calls if path == '/liabilities/get')
    # the server has it off: it cannot be asked for, turned on, or read
    assert admin.api('POST', '/plaid/link-token', {'item_id': 'item-1', 'liabilities': True}).status_code == 400
    assert admin.api('POST', '/plaid/items/item-1/liabilities', {'on': True}).status_code == 400
    admin.api('POST', '/plaid/sync')
    assert asked() == 0
    # the server allows it: still nothing until the owner turns it on for this bank
    from dataclasses import replace
    app.state.settings = replace(app.state.settings, plaid_liabilities=True)
    assert admin.api('GET', '/plaid').json()['liabilities'] is True
    admin.api('POST', '/plaid/sync')
    assert asked() == 0
    assert admin.api('POST', '/plaid/link-token', {'liabilities': True}).status_code == 400      # a new bank: no
    assert admin.api('POST', '/plaid/link-token', {'item_id': 'item-1', 'liabilities': True}).status_code == 200
    sent = fake.calls[-1][1]
    assert sent['additional_consented_products'] == ['liabilities'] and 'products' not in sent
    assert sent['access_token'] == 'access-sandbox-SECRET'
    s = admin.api('POST', '/plaid/items/item-1/liabilities', {'on': True}).json()
    assert asked() == 1 and s['items'][0]['liabilities'] is True and s['items'][0]['liabilities_note'] == ''
    terms = {a['account_id']: a['terms'] for a in s['items'][0]['accounts']}
    assert terms['cc'] == {'apr_bp': 1999, 'minimum': 3500, 'next_due': '2029-03-20', 'statement_balance': 6025,
                           'as_of': TODAY.isoformat()}
    assert terms['chk'] is None and terms['store']['apr_bp'] == 2999
    got = {a['id']: a for a in admin.api('GET', '/accounts').json()['accounts']}
    assert (got[did]['apr_bp'], got[did]['minimum']) == (1999, 3500)
    assert got[did]['bank']['terms']['next_due'] == '2029-03-20'
    assert (got[promo]['apr_bp'], got[promo]['minimum']) == (0, 2500)      # a running promo keeps its own rate
    # every sync reads again; a refusal is a note on the bank, and the sync itself still works
    fake.liabilities['credit'][0]['minimum_payment_amount'] = 40
    assert admin.api('POST', '/plaid/sync').json()['terms'] == 2
    assert next(a for a in admin.api('GET', '/accounts').json()['accounts'] if a['id'] == did)['minimum'] == 4000
    fake.fail['/liabilities/get'] = {'error_code': 'PRODUCTS_NOT_SUPPORTED', 'error_message': 'x'}
    r = admin.api('POST', '/plaid/sync')
    assert r.status_code == 200 and r.json()['terms'] == 0 and r.json()['problems'] == []
    it = admin.api('GET', '/plaid').json()['items'][0]
    assert it['liabilities_note'].startswith('This bank does not give rates') and it['last_error'] == ''
    # off: nothing is asked any more and what was read is forgotten; the debt keeps its figures
    del fake.fail['/liabilities/get']
    n = asked()
    s = admin.api('POST', '/plaid/items/item-1/liabilities', {'on': False}).json()
    admin.api('POST', '/plaid/sync')
    assert asked() == n and s['items'][0]['liabilities'] is False
    assert all(a['terms'] is None for a in s['items'][0]['accounts'])
    assert next(a for a in admin.api('GET', '/accounts').json()['accounts'] if a['id'] == did)['minimum'] == 4000
    assert admin.api('POST', '/plaid/items/nope/liabilities', {'on': False}).status_code == 404
