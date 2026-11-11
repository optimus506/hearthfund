"""Two-factor sign-in through the pages (D-095): enrol, the code step, recovery codes, back-off, tokens, the ways
out for a lost phone, and HBUDGET_REQUIRE_2FA."""
from __future__ import annotations

import re
import time
from pathlib import Path

from test_cli import data, run  # noqa: F401 - `data` is a fixture

from hbudget.core import totp
from hbudget.db import connection, twofactor, users
from hbudget.web.app import create_app
from webhelpers import PASSWORD, Client, add_user, csrf_from, login, make_settings, page_csrf, setup_admin


def db(tmp: Path):  # type: ignore[no-untyped-def]
    return connection.connect(tmp / 'hbudget.db')


def code(tmp: Path, secret: str, username: str = 'admin') -> str:
    """A code that is good right now. Each time step works once, so the step used last is forgotten first
    (the replay tests below do not use this)."""
    conn = db(tmp)
    conn.execute('UPDATE users SET totp_last_step = NULL WHERE username = ?', (username,))
    conn.commit()
    conn.close()
    return totp.code_at(secret, time.time())


def post(c: Client, path: str, **form: str):  # type: ignore[no-untyped-def]
    page = c.get('/account/2fa')
    return c.post(path, data={'csrf_token': csrf_from(page.text), **form}, follow_redirects=False)


def enrol(c: Client, tmp: Path, username: str = 'admin') -> tuple[str, list[str]]:
    r = post(c, '/account/2fa/start', password=PASSWORD)
    assert r.status_code == 200, r.text[:300]
    shown = re.search(r'id="totpKey">([A-Z2-7 ]+)<', r.text)
    assert shown and '<svg ' in r.text and 'otpauth://totp/' in r.text
    secret = shown.group(1).replace(' ', '')
    r = post(c, '/account/2fa/confirm', code=code(tmp, secret, username))
    assert r.status_code == 200 and 'Two-factor sign-in is on' in r.text
    codes = re.findall(r'^[a-z2-7]{4}-[a-z2-7]{4}-[a-z2-7]{4}$', r.text, re.M)
    assert len(codes) == 10
    return secret, codes


def sign_in(app, password: str = PASSWORD, username: str = 'admin') -> tuple[Client, object]:  # type: ignore[no-untyped-def]
    c = Client(app, base_url='http://testserver')
    r = c.get('/login')
    r = c.post('/login', data={'csrf_token': csrf_from(r.text), 'username': username, 'password': password},
               follow_redirects=False)
    return c, r


def give_code(c: Client, value: str):  # type: ignore[no-untyped-def]
    page = c.get('/login/code')
    assert page.status_code == 200, page.status_code
    return c.post('/login/code', data={'csrf_token': csrf_from(page.text), 'code': value}, follow_redirects=False)


def test_enrol_then_sign_in_needs_the_code(admin, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert 'Set up' in admin.get('/account').text
    wrong = post(admin, '/account/2fa/start', password='not the password')
    assert wrong.status_code == 400 and 'The password is not right' in wrong.text
    r = post(admin, '/account/2fa/start', password=PASSWORD)
    secret = re.search(r'id="totpKey">([A-Z2-7 ]+)<', r.text).group(1).replace(' ', '')  # type: ignore[union-attr]
    assert post(admin, '/account/2fa/confirm', code='000000').status_code == 400
    assert 'Off' in admin.get('/account').text                  # a pending set-up changes nothing yet
    c, r = sign_in(app)
    assert r.headers['location'] == '/'                          # still password only
    other = login(app, 'admin')
    r = post(admin, '/account/2fa/confirm', code=code(tmp_path, secret))
    assert r.status_code == 200 and r.headers['cache-control'] == 'no-store' and 'shown only this once' in r.text
    assert 'Manage' in admin.get('/account').text                # this session stays
    assert other.get('/account', follow_redirects=False).headers['location'] == '/login'     # the others do not

    # the password alone now opens nothing
    c, r = sign_in(app)
    assert r.status_code == 303 and r.headers['location'] == '/login/code'
    assert 'hb_session' not in c.cookies and 'hb_2fa' in r.headers['set-cookie']
    assert 'HttpOnly' in r.headers['set-cookie'] and 'Path=/login' in r.headers['set-cookie']
    assert c.get('/h/%d/bills' % admin.hid, follow_redirects=False).headers['location'] == '/login'
    assert c.get('/account/2fa', follow_redirects=False).headers['location'] == '/login'
    assert c.get('/api/h/%d/series' % admin.hid, headers={'Accept': 'application/json'}).status_code == 401
    bad = give_code(c, '000000')
    assert bad.status_code == 401 and 'That code is not right' in bad.text and 'hb_session' not in c.cookies
    good = totp.code_at(secret, time.time() + 30)                # the next step: the current one confirmed enrolment
    r = give_code(c, good)
    assert r.status_code == 303 and r.headers['location'] == '/' and 'hb_session' in c.cookies
    assert c.get('/h/%d/bills' % admin.hid).status_code == 200
    # the same code does not work a second time, in this browser or another
    c2, r = sign_in(app)
    assert give_code(c2, good).status_code == 401
    # without the step in between there is no code page, and a made-up cookie is no challenge
    anon = Client(app, base_url='http://testserver')
    assert anon.get('/login/code', follow_redirects=False).headers['location'] == '/login'
    anon.cookies.set('hb_2fa', 'made-up', path='/login')
    assert anon.get('/login/code', follow_redirects=False).headers['location'] == '/login'
    page = anon.get('/login')
    r = anon.post('/login/code', data={'csrf_token': csrf_from(page.text), 'code': good}, follow_redirects=False)
    assert r.headers['location'] == '/login' and 'hb_session' not in anon.cookies
    # a wrong password is answered as before, with no hint that the account has 2FA
    _, r = sign_in(app, 'wrong password!')
    assert r.status_code == 401 and 'do not match' in r.text


def test_a_borrowed_session_cannot_take_over_a_set_up_in_progress(admin, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The key is shown to, and confirmed by, only the session that gave the password."""
    r = post(admin, '/account/2fa/start', password=PASSWORD)
    secret = re.search(r'id="totpKey">([A-Z2-7 ]+)<', r.text).group(1).replace(' ', '')  # type: ignore[union-attr]
    thief = login(app, 'admin')
    seen = thief.get('/account/2fa').text
    assert 'totpKey' not in seen and 'otpauth://' not in seen and '<svg ' not in seen and secret[:8] not in seen
    r = post(thief, '/account/2fa/confirm', code=code(tmp_path, secret))
    assert r.status_code == 400 and 'run out' in r.text
    assert 'Off' in admin.get('/account').text
    assert post(thief, '/account/2fa/cancel').status_code == 200           # cancelling is harmless...
    assert 'totpKey' not in admin.get('/account/2fa').text
    secret, _ = enrol(admin, tmp_path)
    assert post(admin, '/account/2fa/cancel').status_code == 200           # ...and never turns off what is on
    conn = db(tmp_path)
    assert twofactor.state(conn, users.get_user_by_name(conn, 'admin').id).enabled   # type: ignore[union-attr]
    conn.close()
    # wrong codes while confirming count like any other wrong code
    post(admin, '/account/2fa/off', password=PASSWORD, code=code(tmp_path, secret))
    post(admin, '/account/2fa/start', password=PASSWORD)
    for _ in range(users.USER_FAILURES):
        assert post(admin, '/account/2fa/confirm', code='000000').status_code == 400
    assert 'Too many attempts' in post(admin, '/account/2fa/confirm', code='000000').text


def test_odd_input_is_wrong_not_an_error(admin, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    enrol(admin, tmp_path)
    c, _ = sign_in(app)
    for odd in ('١٢٣٤٥٦', '²²²²²²', '１２３４５６'):
        assert give_code(c, odd).status_code == 401
    # a long name with accents does not fit the QR codes drawn here: the key is shown to type in
    name = 'é' * 60
    add_user(tmp_path, admin.hid, name, 'viewer')
    u = login(app, name)
    r = post(u, '/account/2fa/start', password=PASSWORD)
    assert r.status_code == 200 and 'totpKey' in r.text and '<svg ' not in r.text and 'by hand' in r.text
    shown = re.search(r'id="totpKey">([A-Z2-7 ]+)<', r.text).group(1).replace(' ', '')  # type: ignore[union-attr]
    assert post(u, '/account/2fa/confirm', code=code(tmp_path, shown, name)).status_code == 200


def test_new_ways_in_ask_for_a_code(admin, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A member, a user, an admin flag, a reset: each would outlive a borrowed session, so each needs a code."""
    add_user(tmp_path, admin.hid, 'sam', 'viewer')
    conn = db(tmp_path)
    sam = users.get_user_by_name(conn, 'sam').id     # type: ignore[union-attr]
    conn.close()
    secret, _ = enrol(admin, tmp_path)
    admin.csrf = csrf_from(admin.get('/account/2fa').text)
    head = {'X-CSRF-Token': admin.csrf, 'Origin': 'http://testserver'}
    h = '/api/h/%d' % admin.hid
    calls = [('POST', h + '/members', {'username': 'pat', 'password': PASSWORD, 'role': 'owner'}),
             ('PUT', '%s/members/%d' % (h, sam), {'role': 'owner'}),
             ('POST', '/api/admin/users', {'username': 'kim', 'password': PASSWORD, 'is_admin': True}),
             ('POST', '/api/admin/users/%d/admin' % sam, {'is_admin': True}),
             ('POST', '/api/admin/users/%d/password' % sam, {'password': 'another long password'}),
             ('POST', '/api/admin/users/%d/2fa/reset' % sam, {})]
    for method, url, body in calls:
        r = admin.request(method, url, json=body, headers=head)
        assert r.status_code == 403 and r.json()['error'] == 'two_factor', (url, r.text)
        r = admin.request(method, url, json=body, headers={**head, 'X-2FA-Code': '000000'})
        assert r.status_code == 403 and r.json()['error'] == 'two_factor', (url, r.text)
        conn = db(tmp_path)
        conn.execute('DELETE FROM login_attempts')
        conn.commit()
        conn.close()
    conn = db(tmp_path)
    assert users.get_user_by_name(conn, 'pat') is None and users.get_user_by_name(conn, 'kim') is None
    assert not users.get_user(conn, sam).is_admin    # type: ignore[union-attr]
    conn.close()
    for method, url, body in calls[:5]:
        r = admin.request(method, url, json=body, headers={**head, 'X-2FA-Code': code(tmp_path, secret)})
        assert r.status_code == 200, (url, r.text)
    # the reset itself, once sam has two-factor sign-in on
    conn = db(tmp_path)
    key = twofactor.begin(conn, sam, session='sams-session')
    assert twofactor.confirm(conn, sam, totp.code_at(key, time.time()), session='sams-session')
    conn.close()
    reset = '/api/admin/users/%d/2fa/reset' % sam
    r = admin.post(reset, json={}, headers={**head, 'X-2FA-Code': code(tmp_path, secret)})
    assert r.status_code == 200, r.text
    # wrong codes in the header are held back like wrong passwords
    for _ in range(users.USER_FAILURES):
        r = admin.post(h + '/tokens', json={'name': 'x'}, headers={**head, 'X-2FA-Code': '000000'})
        assert r.status_code == 403 and r.json()['error'] == 'two_factor'
    r = admin.post(h + '/tokens', json={'name': 'x'}, headers={**head, 'X-2FA-Code': code(tmp_path, secret)})
    assert r.status_code == 429 and int(r.headers['retry-after']) >= 1


def test_recovery_codes_work_once_and_can_be_replaced(admin, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    secret, codes = enrol(admin, tmp_path)
    c, _ = sign_in(app)
    r = give_code(c, codes[0].upper())
    assert r.status_code == 303 and r.headers['location'] == '/account/2fa'
    assert 'Recovery codes left: <b>9</b>' in c.get('/account/2fa').text
    c2, _ = sign_in(app)
    assert give_code(c2, codes[0]).status_code == 401
    # new codes need the app's code, not a recovery code
    assert post(c, '/account/2fa/codes', code=codes[1]).status_code == 400
    r = post(c, '/account/2fa/codes', code=code(tmp_path, secret))
    fresh = re.findall(r'^[a-z2-7]{4}-[a-z2-7]{4}-[a-z2-7]{4}$', r.text, re.M)
    assert r.status_code == 200 and len(fresh) == 10 and not set(fresh) & set(codes)
    c3, _ = sign_in(app)
    assert give_code(c3, codes[2]).status_code == 401 and give_code(c3, fresh[0]).status_code == 303


def test_wrong_codes_are_held_back_and_the_password_does_not_reset_that(admin, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    secret, _ = enrol(admin, tmp_path)
    c, _ = sign_in(app)
    for _ in range(users.USER_FAILURES):
        assert give_code(c, '000000').status_code == 401
    held = give_code(c, code(tmp_path, secret))
    assert held.status_code == 429 and int(held.headers['retry-after']) >= 1 and 'hb_session' not in c.cookies
    # the right password again does not clear the count: the next try is still held back
    _, r = sign_in(app)
    assert r.status_code == 429


def test_tokens_ask_for_a_code(admin, tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert admin.api('POST', '/tokens', {'name': 'before'}).status_code == 200        # no 2FA: as before
    secret, codes = enrol(admin, tmp_path)
    admin.csrf = csrf_from(admin.get('/account/2fa').text)
    r = admin.api('POST', '/tokens', {'name': 'agent'})
    assert r.status_code == 403 and r.json()['error'] == 'two_factor'
    head = {'X-CSRF-Token': admin.csrf, 'Origin': 'http://testserver'}
    url = '/api/h/%d/tokens' % admin.hid
    assert admin.post(url, json={'name': 'agent'}, headers={**head, 'X-2FA-Code': '000000'}).status_code == 403
    assert admin.post(url, json={'name': 'agent'}, headers={**head, 'X-2FA-Code': codes[0]}).status_code == 403
    made = admin.post(url, json={'name': 'agent'}, headers={**head, 'X-2FA-Code': code(tmp_path, secret)})
    assert made.status_code == 200 and made.json()['secret']
    tid = made.json()['token']['id']
    assert admin.delete('%s/%d' % (url, tid), headers=head).status_code == 200      # revoking needs no code
    # the token itself is a separate secret: it works without any code
    bearer = {'Authorization': 'Bearer ' + admin.api('GET', '/tokens').json()['tokens'][0]['hint']}
    assert admin.get('/api/v1/summary', headers=bearer).status_code == 401            # (a hint is not a token)
    assert admin.api('GET', '/tokens').status_code == 200                              # listing needs no code


def test_turning_it_off_needs_password_and_code(admin, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    secret, _ = enrol(admin, tmp_path)
    other, r = sign_in(app)
    assert give_code(other, code(tmp_path, secret)).status_code == 303
    assert post(admin, '/account/2fa/off', password=PASSWORD, code='000000').status_code == 400
    assert post(admin, '/account/2fa/off', password='nope nope nope', code=code(tmp_path, secret)).status_code == 400
    r = post(admin, '/account/2fa/off', password=PASSWORD, code=code(tmp_path, secret))
    assert r.status_code == 200 and 'Two-factor sign-in is off' in r.text
    assert other.get('/account', follow_redirects=False).headers['location'] == '/login'
    _, r = sign_in(app)
    assert r.headers['location'] == '/'
    conn = db(tmp_path)
    assert conn.execute('SELECT totp_secret FROM users').fetchone()[0] is None
    assert conn.execute('SELECT COUNT(*) FROM recovery_codes').fetchone()[0] == 0
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE '2fa.%' ORDER BY id")]
    conn.close()
    assert actions == ['2fa.on', '2fa.off']
    # cancel a set-up that was started
    post(admin, '/account/2fa/start', password=PASSWORD)
    r = post(admin, '/account/2fa/cancel')
    assert 'Set up two-factor sign-in' in r.text and 'totpKey' not in r.text


def test_lost_phone_admin_reset_and_command_line(admin, app, tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    add_user(tmp_path, admin.hid, 'sam', 'editor')
    sam = login(app, 'sam')
    enrol(sam, tmp_path, 'sam')
    conn = db(tmp_path)
    sam_id = users.get_user_by_name(conn, 'sam').id  # type: ignore[union-attr]
    conn.close()
    head = {'X-CSRF-Token': admin.csrf, 'Origin': 'http://testserver'}
    listed = {u['username']: u['two_factor'] for u in admin.get('/api/admin/overview').json()['users']}
    assert listed == {'admin': False, 'sam': True}
    own = {'X-CSRF-Token': page_csrf(sam, '/account'), 'Origin': 'http://testserver'}
    assert sam.post('/api/admin/users/%d/2fa/reset' % sam_id, json={}, headers=own).status_code == 404   # not an admin
    r = admin.post('/api/admin/users/%d/2fa/reset' % sam_id, json={}, headers=head)
    assert r.status_code == 200, r.text
    assert sam.get('/account', follow_redirects=False).headers['location'] == '/login'      # signed out
    assert admin.post('/api/admin/users/%d/2fa/reset' % sam_id, json={}, headers=head).status_code == 400
    me = next(u['id'] for u in admin.get('/api/admin/overview').json()['users'] if u['username'] == 'admin')
    r = admin.post('/api/admin/users/%d/2fa/reset' % me, json={}, headers=head)       # own: only under Account
    assert r.status_code == 400 and 'Account' in r.json()['message']
    sam, r = sign_in(app, username='sam')
    assert r.headers['location'] == '/'                                                    # the password is enough
    # the command line does the same
    enrol(sam, tmp_path, 'sam')
    monkeypatch.setenv('HBUDGET_DATA_DIR', str(tmp_path))
    assert run('disable-2fa', 'sam') == 0 and 'is off for sam' in capsys.readouterr().out
    assert run('disable-2fa', 'sam') == 0 and 'was not on' in capsys.readouterr().out
    conn = db(tmp_path)
    assert not twofactor.state(conn, sam_id).enabled
    assert [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE '2fa.reset%' ORDER BY id")] == [
        '2fa.reset', '2fa.reset.cli']
    conn.close()


def test_required_two_factor_sends_everyone_to_set_it_up(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = create_app(make_settings(tmp_path, require_2fa=True))
    try:
        with Client(app, base_url='http://testserver') as c:
            setup_admin(c, tmp_path)
            assert c.get('/account', follow_redirects=False).headers['location'] == '/account/2fa'
            assert c.get('/h/1/bills', follow_redirects=False).headers['location'] == '/account/2fa'
            api = c.get('/api/h/1/series', headers={'Accept': 'application/json'})
            assert api.status_code == 403 and api.json()['error'] == 'two_factor_required'
            page = c.get('/account/2fa')
            assert page.status_code == 200 and 'requires two-factor sign-in' in page.text
            secret, _ = enrol(c, tmp_path)
            assert c.get('/h/1/bills').status_code == 200
            assert 'cannot be turned off here' in c.get('/account/2fa').text
            off = post(c, '/account/2fa/off', password=PASSWORD, code=code(tmp_path, secret))
            assert off.status_code == 400 and 'requires two-factor' in off.text
            assert c.get('/h/1/bills').status_code == 200
        # someone not set up yet can still sign out
        with Client(app, base_url='http://testserver') as c:
            add_user(tmp_path, 1, 'newcomer', 'viewer')
            r = c.get('/login')
            c.post('/login', data={'csrf_token': csrf_from(r.text), 'username': 'newcomer', 'password': PASSWORD})
            assert c.get('/h/1/bills', follow_redirects=False).headers['location'] == '/account/2fa'
            out = c.post('/logout', data={'csrf_token': csrf_from(c.get('/account/2fa').text)}, follow_redirects=False)
            assert out.status_code == 303 and out.headers['location'] == '/login'
    finally:
        app.state.stop.set()


def test_reusing_a_code_is_not_a_failed_attempt_and_too_many_wrong_codes_lock_them(admin, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    secret, codes = enrol(admin, tmp_path)
    admin.csrf = csrf_from(admin.get('/account/2fa').text)
    head = {'X-CSRF-Token': admin.csrf, 'Origin': 'http://testserver'}
    url = '/api/h/%d/tokens' % admin.hid
    now = code(tmp_path, secret)
    assert admin.post(url, json={'name': 'one'}, headers={**head, 'X-2FA-Code': now}).status_code == 200
    again = admin.post(url, json={'name': 'two'}, headers={**head, 'X-2FA-Code': now})
    assert again.status_code == 403 and 'just used' in again.json()['message']
    conn = db(tmp_path)
    assert conn.execute('SELECT COUNT(*) FROM login_attempts WHERE ok = 0').fetchone()[0] == 0
    conn.close()
    # wrong codes from several addresses add up per user, and lock the app's codes for the day
    c, _ = sign_in(app)
    for k in range(twofactor.CODE_FAILURES):
        conn = db(tmp_path)
        conn.execute('DELETE FROM login_backoff')              # as if from many addresses, slowly
        conn.commit()
        conn.close()
        expect = 429 if k == twofactor.CODE_FAILURES - 1 else 401             # the 10th locks them
        assert give_code(c, '000000' if k % 2 else '999999').status_code == expect, k
    r = give_code(c, code(tmp_path, secret))
    assert r.status_code == 429 and 'recovery codes' in r.text and 'hb_session' not in c.cookies
    conn = db(tmp_path)
    conn.execute('DELETE FROM login_backoff')
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = '2fa.locked'").fetchone()[0] == 1
    conn.close()
    r = give_code(c, codes[0])
    assert r.status_code == 303 and 'hb_session' in c.cookies                 # a recovery code still signs in
