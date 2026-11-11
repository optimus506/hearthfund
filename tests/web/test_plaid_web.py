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
        elif path == '/item/remove':
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
    fake.pages = [{'added': [tx('p1', '2029-03-02', 20.00, 'CORNER CAFE')], 'next_cursor': 'c1'}]
    admin.api('POST', '/plaid/sync', {})
    conn = connection.connect(tmp_path / 'hbudget.db')
    try:
        repo.wipe_household(conn, admin.hid)
        assert conn.execute('SELECT COUNT(*) FROM plaid_txns').fetchone()[0] == 0
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
