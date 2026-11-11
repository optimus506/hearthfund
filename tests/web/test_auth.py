"""First-run setup, sign-in, throttling, sessions, password changes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hbudget.db import connection, repo, users
from hbudget.web.app import create_app
from webhelpers import PASSWORD, Client, add_user, csrf_from, login, make_settings, page_csrf, setup_admin


def test_first_run_redirects_to_setup_and_writes_a_token(app, tmp_path):
    with Client(app) as c:
        r = c.get('/', follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'] == '/setup'
        assert c.get('/login', follow_redirects=False).headers['location'] == '/setup'
        token = (tmp_path / 'setup-token').read_text().strip()
        assert len(token) >= 40
        assert oct((tmp_path / 'setup-token').stat().st_mode)[-3:] == '600'


def test_setup_needs_the_token_and_closes_afterwards(app, tmp_path):
    with Client(app) as c:
        page = c.get('/setup')
        csrf = csrf_from(page.text)
        r = c.post('/setup', data={'csrf_token': csrf, 'token': 'wrong', 'username': 'admin', 'password': PASSWORD,
                                   'household': 'H'})
        assert r.status_code == 403
        conn = connection.connect(tmp_path / 'hbudget.db')
        assert users.user_count(conn) == 0
        conn.close()
        # a token in the query string is ignored
        token = (tmp_path / 'setup-token').read_text().strip()
        r = c.post('/setup?token=' + token, data={'csrf_token': csrf, 'username': 'admin', 'password': PASSWORD,
                                                  'household': 'H'})
        assert r.status_code == 403
        setup_admin(c, tmp_path)
        assert not (tmp_path / 'setup-token').exists()
        assert c.get('/setup').status_code == 404
        csrf = page_csrf(c, '/account')       # a valid token for this (now signed-in) browser: only setup is closed
        r = c.post('/setup', data={'csrf_token': csrf, 'token': token, 'username': 'x', 'password': PASSWORD,
                                   'household': 'H'})
        assert r.status_code == 404


def test_setup_rejects_a_short_password(app, tmp_path):
    with Client(app) as c:
        r = c.get('/setup')
        token = (tmp_path / 'setup-token').read_text().strip()
        r = c.post('/setup', data={'csrf_token': csrf_from(r.text), 'token': token, 'username': 'admin',
                                   'password': 'short', 'household': 'H'})
        assert r.status_code == 400 and 'at least 10' in r.text


def test_setup_without_csrf_is_refused(app, tmp_path):
    with Client(app) as c:
        c.get('/setup')
        token = (tmp_path / 'setup-token').read_text().strip()
        r = c.post('/setup', data={'token': token, 'username': 'admin', 'password': PASSWORD, 'household': 'H'})
        assert r.status_code == 403


def test_admin_from_environment_and_ignored_later(tmp_path, caplog):
    s = make_settings(tmp_path, admin_username='boss', admin_password=PASSWORD)
    a = create_app(s)
    a.state.stop.set()
    assert not (tmp_path / 'setup-token').exists()
    c = login(a, 'boss')
    assert c.get('/', follow_redirects=False).headers['location'].startswith('/h/')
    b = create_app(s)
    b.state.stop.set()
    assert 'ignored' in caplog.text


def test_login_logout_and_session_cookie(admin, app, tmp_path):
    r = admin.get('/h/%d/bills' % admin.hid)
    assert r.status_code == 200
    cookie = admin.cookies.get('hb_session')
    assert cookie
    page = admin.get('/logout')
    assert page.status_code == 200          # GET only shows a button
    assert admin.get('/h/%d/bills' % admin.hid).status_code == 200
    r = admin.post('/logout', data={'csrf_token': admin.csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert admin.get('/h/%d/bills' % admin.hid, follow_redirects=False).headers['location'] == '/login'
    assert admin.get('/api/h/%d/summary' % admin.hid).status_code == 401


def test_secure_cookie_name_when_https(tmp_path):
    a = create_app(make_settings(tmp_path, cookie_secure='true', admin_username='boss', admin_password=PASSWORD))
    a.state.stop.set()
    with Client(a, base_url='https://testserver') as c:
        r = c.get('/login')
        set_cookie = r.headers['set-cookie']
        assert 'HttpOnly' in set_cookie and 'SameSite=strict' in set_cookie and 'Secure' in set_cookie
        r = c.post('/login', data={'csrf_token': csrf_from(r.text), 'username': 'boss', 'password': PASSWORD},
                   headers={'Origin': 'https://testserver'}, follow_redirects=False)
        assert r.status_code == 303
        sc = r.headers.get_list('set-cookie')
        assert any(x.startswith('__Host-hb_session=') and 'Secure' in x and 'HttpOnly' in x
                   and 'samesite=lax' in x.lower() for x in sc), sc
        assert r.headers['strict-transport-security']


def test_wrong_password_same_message_for_unknown_user(admin, app):
    c = Client(app)
    page = c.get('/login')
    r1 = c.post('/login', data={'csrf_token': csrf_from(page.text), 'username': 'admin', 'password': 'nope nope nope'})
    r2 = c.post('/login', data={'csrf_token': csrf_from(page.text), 'username': 'ghost', 'password': 'nope nope nope'})
    assert r1.status_code == r2.status_code == 401
    assert 'do not match' in r1.text and 'do not match' in r2.text


def test_throttling_returns_429_with_retry_after(admin, app):
    c = Client(app)
    page = c.get('/login')
    tok = csrf_from(page.text)
    codes = [c.post('/login', data={'csrf_token': tok, 'username': 'admin', 'password': 'bad password!'}).status_code
             for _ in range(6)]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429
    r = c.post('/login', data={'csrf_token': tok, 'username': 'admin', 'password': PASSWORD})
    assert r.status_code == 429 and int(r.headers['retry-after']) >= 1      # even the right password waits


def test_busy_hashing_answers_503(admin, app):
    import asyncio

    from hbudget.web.security import HashSlots
    app.state.hash_slots = HashSlots(1, 0.01)
    asyncio.run(app.state.hash_slots._sem.acquire())    # the only slot is taken
    c = Client(app)
    page = c.get('/login')
    r = c.post('/login', data={'csrf_token': csrf_from(page.text), 'username': 'admin', 'password': PASSWORD})
    assert r.status_code == 503 and r.headers['retry-after']


def test_password_change_signs_out_other_sessions(admin, app, tmp_path):
    other = login(app, 'admin')
    assert other.get('/account').status_code == 200
    r = admin.post('/account/password', data={'csrf_token': admin.csrf, 'current': PASSWORD,
                                              'new': 'another long password'})
    assert r.status_code == 200 and 'Password changed' in r.text
    assert admin.get('/account').status_code == 200
    assert other.get('/account', follow_redirects=False).status_code == 303
    r = admin.post('/account/password', data={'csrf_token': admin.csrf, 'current': 'wrong', 'new': 'x' * 12})
    assert r.status_code == 400


def test_disabled_user_is_signed_out_at_once(admin, app, tmp_path):
    add_user(tmp_path, admin.hid, 'partner', 'editor')
    p = login(app, 'partner')
    assert p.get('/h/%d/bills' % admin.hid).status_code == 200
    conn = connection.connect(tmp_path / 'hbudget.db')
    users.set_disabled(conn, users.get_user_by_name(conn, 'partner').id, True)
    conn.close()
    assert p.get('/h/%d/bills' % admin.hid, follow_redirects=False).status_code == 303


def test_role_change_applies_on_the_next_request(admin, app, tmp_path):
    add_user(tmp_path, admin.hid, 'partner', 'editor')
    p = login(app, 'partner')
    p.hid = admin.hid
    p.csrf = admin.csrf
    from webhelpers import page_csrf
    p.csrf = page_csrf(p, '/h/%d/bills' % admin.hid)
    assert p.api('PUT', '/notes', {'notes': 'hello'}).status_code == 200
    conn = connection.connect(tmp_path / 'hbudget.db')
    repo.set_role(conn, admin.hid, users.get_user_by_name(conn, 'partner').id, 'viewer')
    conn.close()
    assert p.api('PUT', '/notes', {'notes': 'again'}).status_code == 403


def test_logins_are_audited_without_passwords(admin, app, tmp_path):
    c = Client(app)
    page = c.get('/login')
    c.post('/login', data={'csrf_token': csrf_from(page.text), 'username': 'admin', 'password': 'wrong password'})
    conn = connection.connect(tmp_path / 'hbudget.db')
    actions = [a['action'] for a in repo.list_audit(conn, None)]
    rows = conn.execute('SELECT detail_json FROM audit_log').fetchall()
    conn.close()
    assert 'login.failed' in actions and 'setup.done' in actions
    assert not any('wrong password' in r[0] or PASSWORD in r[0] for r in rows)


def test_expired_session_is_refused(admin, app, tmp_path):
    conn = connection.connect(tmp_path / 'hbudget.db')
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec='seconds')
    conn.execute('UPDATE sessions SET expires_at = ?', (old,))
    conn.commit()
    conn.close()
    assert admin.get('/api/h/%d/summary' % admin.hid).status_code == 401


def test_home_without_household_goes_to_account(admin, app, tmp_path):
    add_user(tmp_path, admin.hid, 'loner', None)
    c = login(app, 'loner')
    assert c.get('/', follow_redirects=False).headers['location'] == '/account'
    assert c.get('/switch?hid=%d' % admin.hid).status_code == 404


def test_error_pages_link_back_to_the_household(admin, app, tmp_path):
    add_user(tmp_path, admin.hid, 'viewer1', 'viewer')
    v = login(app, 'viewer1')
    home = '/h/%d' % admin.hid
    r = v.get(home + '/import')                    # owner only: 403 inside a household the viewer can see
    assert r.status_code == 403 and 'href="%s"' % home in r.text and 'Back to Test household' in r.text
    r = admin.get(home + '/no-such-tab')           # 404 inside the household
    assert r.status_code == 404 and 'href="%s"' % home in r.text
    for path in ('/h/%d/bills' % (admin.hid + 99), '/no-such-page'):   # no household known: back to /
        r = admin.get(path)
        assert r.status_code == 404 and 'href="/"' in r.text and 'Back to' not in r.text
    add_user(tmp_path, admin.hid + 99, 'outsider', None)
    r = login(app, 'outsider').get(home + '/bills')   # not a member: no link to it, no name
    assert r.status_code == 404 and 'href="/"' in r.text and 'Test household' not in r.text
    r = Client(app).get(home + '/import', follow_redirects=False)   # signed out: the login redirect as before
    assert r.status_code == 303
