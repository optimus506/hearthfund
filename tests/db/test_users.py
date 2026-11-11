from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hbudget.db import connection, repo, users

T0 = datetime(2029, 1, 5, 12, 0, tzinfo=UTC)
PW = 'correct horse battery'


def test_hash_and_check_password() -> None:
    h = users.hash_password(PW)
    assert h.startswith('scrypt$32768$8$1$') and users.check_password(PW, h)
    assert not users.check_password('wrong password', h)
    assert users.hash_password(PW) != h                                    # random salt
    assert not users.check_password(PW, 'md5$x') and not users.check_password(PW, 'garbage')
    assert not users.check_password(PW, 'md5$32768$8$1$AA==$AA==')            # six parts, wrong algorithm
    assert not users.check_password(PW, 'scrypt$x$8$1$AA==$AA==')


def test_create_user_rules(conn: sqlite3.Connection) -> None:
    uid = users.create_user(conn, ' alice ', PW, now=T0)
    u = users.get_user(conn, uid)
    assert u is not None and u.username == 'alice' and not u.is_admin and not u.disabled
    with pytest.raises(ValueError, match='taken'):
        users.create_user(conn, 'ALICE', PW)                               # usernames are case-insensitive
    with pytest.raises(ValueError, match='at least 10'):
        users.create_user(conn, 'bob', 'short')
    with pytest.raises(ValueError, match='without spaces'):
        users.create_user(conn, 'bo b', PW)
    with pytest.raises(ValueError, match='without spaces'):
        users.create_user(conn, '', PW)
    assert users.get_user_by_name(conn, 'Alice') == u and users.get_user(conn, 999) is None
    assert [x.username for x in users.list_users(conn)] == ['alice'] and users.user_count(conn) == 1


def test_verify_login(conn: sqlite3.Connection) -> None:
    uid = users.create_user(conn, 'alice', PW)
    assert users.verify_login(conn, 'alice', PW) == uid
    assert users.verify_login(conn, 'alice', 'wrong password') is None
    assert users.verify_login(conn, 'nobody', PW) is None
    users.set_disabled(conn, uid, True)
    assert users.verify_login(conn, 'alice', PW) is None
    users.set_disabled(conn, uid, False)
    users.set_admin(conn, uid, True)
    assert users.get_user(conn, uid).is_admin  # type: ignore[union-attr]
    for fn in (lambda: users.set_disabled(conn, 99, True), lambda: users.set_admin(conn, 99, True),
               lambda: users.set_password(conn, 99, PW)):
        with pytest.raises(KeyError):
            fn()


def test_sessions_slide_expire_and_are_hashed(conn: sqlite3.Connection) -> None:
    uid = users.create_user(conn, 'alice', PW)
    token, csrf = users.create_session(conn, uid, user_agent='ua' * 200, now=T0)
    stored = conn.execute('SELECT token_hash, user_agent FROM sessions').fetchone()
    assert stored[0] != token and len(stored[1]) == 200                    # only the hash is stored
    s = users.get_session(conn, token, now=T0 + timedelta(hours=11))
    assert s is not None and s.user_id == uid and s.csrf_token == csrf
    assert users.get_session(conn, token, now=T0 + timedelta(hours=22)) is not None    # idle time slides
    assert users.get_session(conn, token, now=T0 + timedelta(hours=40)) is None        # 18 h idle: expired
    assert users.get_session(conn, token) is None and users.get_session(conn, '') is None
    token, _ = users.create_session(conn, uid, now=T0)
    for h in range(10, 24 * 7, 10):
        assert users.get_session(conn, token, now=T0 + timedelta(hours=h)) is not None
    assert users.get_session(conn, token, now=T0 + timedelta(days=7, minutes=1)) is None   # absolute limit


def test_disabled_users_lose_sessions_and_logout(conn: sqlite3.Connection) -> None:
    uid = users.create_user(conn, 'alice', PW)
    a, _ = users.create_session(conn, uid, now=T0)
    b, _ = users.create_session(conn, uid, now=T0)
    users.delete_session(conn, a)
    assert users.get_session(conn, a, now=T0) is None and users.get_session(conn, b, now=T0) is not None
    conn.execute('UPDATE users SET disabled = 1 WHERE id = ?', (uid,))
    assert users.get_session(conn, b, now=T0) is None                      # re-read on every request


def test_password_change_drops_other_sessions(conn: sqlite3.Connection) -> None:
    uid = users.create_user(conn, 'alice', PW)
    keep, _ = users.create_session(conn, uid, now=T0)
    other, _ = users.create_session(conn, uid, now=T0)
    users.set_password(conn, uid, 'another long password', keep_token=keep, now=T0)
    assert users.get_session(conn, keep, now=T0) is not None and users.get_session(conn, other, now=T0) is None
    assert users.verify_login(conn, 'alice', 'another long password') == uid
    users.set_password(conn, uid, 'a third long password')
    assert users.get_session(conn, keep, now=T0) is None
    with pytest.raises(ValueError):
        users.set_password(conn, uid, 'short')
    assert users.delete_user_sessions(conn, uid) == 0


def test_user_back_off_grows_and_clears(conn: sqlite3.Connection) -> None:
    """Counted per username and address: the address that keeps failing is held back (D-097)."""
    for _ in range(4):
        users.record_attempt(conn, 'alice', '10.0.0.5', False, now=T0)
    assert users.too_many_attempts(conn, 'alice', '10.0.0.5', now=T0) is None
    users.record_attempt(conn, 'alice', '10.0.0.5', False, now=T0)          # 5th failure from this address
    assert users.too_many_attempts(conn, 'ALICE', '10.0.0.5', now=T0) == 3  # 2 s, rounded up
    assert users.too_many_attempts(conn, 'alice', '10.0.0.5', now=T0 + timedelta(seconds=3)) is None
    for _ in range(10):
        users.record_attempt(conn, 'alice', '10.0.0.5', False, now=T0)
    assert users.too_many_attempts(conn, 'alice', '10.0.0.5', now=T0) == 61   # capped at 60 s
    users.record_attempt(conn, 'alice', '10.0.0.5', True, clears=False, now=T0)
    assert users.too_many_attempts(conn, 'alice', '10.0.0.5', now=T0) == 61   # half a sign-in clears nothing
    users.record_attempt(conn, 'alice', '10.0.0.5', True, now=T0)
    assert users.too_many_attempts(conn, 'alice', '10.0.0.5', now=T0) is None  # a success clears it
    assert users.too_many_attempts(conn, 'alice', '10.0.0.5', now=T0 + timedelta(minutes=16)) is None


def test_someone_guessing_elsewhere_does_not_lock_the_real_user_out(conn: sqlite3.Connection) -> None:
    for _ in range(15):
        users.record_attempt(conn, 'admin', '203.0.113.9', False, now=T0)
    assert users.too_many_attempts(conn, 'admin', '203.0.113.9', now=T0) == 61
    assert users.too_many_attempts(conn, 'admin', '192.168.0.20', now=T0) is None   # the household's own address


def test_guesses_spread_over_many_addresses_hold_back_every_address_that_failed(conn: sqlite3.Connection) -> None:
    for i in range(users.NAME_FAILURES):
        users.record_attempt(conn, 'admin', '198.51.100.%d' % i, False, now=T0)   # one guess from each
    assert users.too_many_attempts(conn, 'admin', '198.51.100.1', now=T0) == 3     # held, though it failed once
    assert users.too_many_attempts(conn, 'admin', '198.51.100.99', now=T0) is None  # a fresh address: one try
    users.record_attempt(conn, 'admin', '198.51.100.99', False, now=T0)
    assert users.too_many_attempts(conn, 'admin', '198.51.100.99', now=T0) is not None
    assert users.too_many_attempts(conn, 'someone else', '198.51.100.1', now=T0) is None


def test_ip_back_off(conn: sqlite3.Connection) -> None:
    for i in range(20):
        users.record_attempt(conn, 'user%d' % i, '192.0.2.1', False, now=T0)
    wait = users.too_many_attempts(conn, 'someone', '192.0.2.1', now=T0)
    assert wait is not None and 14 * 60 < wait <= 15 * 60 + 1
    assert users.too_many_attempts(conn, 'someone', '192.0.2.2', now=T0) is None
    users.record_attempt(conn, 'user0', '192.0.2.1', False, now=T0 + timedelta(minutes=20))
    assert users.too_many_attempts(conn, 'someone', '192.0.2.1', now=T0 + timedelta(minutes=20)) is None


def test_purge(conn: sqlite3.Connection) -> None:
    uid = users.create_user(conn, 'alice', PW)
    users.create_session(conn, uid, now=T0)
    users.record_attempt(conn, 'alice', 'ip', False, now=T0)
    for _ in range(5):
        users.record_attempt(conn, 'bob', 'ip', False, now=T0)
    out = users.purge(conn, now=T0 + timedelta(days=31))
    assert out == {'sessions': 1, 'login_attempts': 6, 'login_backoff': 1}


def test_first_admin_once(conn: sqlite3.Connection) -> None:
    uid, hid = users.create_first_admin(conn, 'admin', PW, 'Our home', now=T0)
    assert users.get_user(conn, uid).is_admin and repo.get_role(conn, uid, hid) == 'owner'  # type: ignore[union-attr]
    assert repo.get_household(conn, hid).name == 'Our home'  # type: ignore[union-attr]
    with pytest.raises(users.SetupDone):
        users.create_first_admin(conn, 'second', PW, 'Other')
    with pytest.raises(ValueError):
        users.create_first_admin(conn, 'x', 'short', 'Other')
    with pytest.raises(ValueError, match='without spaces'):
        users.create_first_admin(conn, 'two words', PW, 'Other')


def test_first_admin_race(db_file: Path, conn: sqlite3.Connection) -> None:
    results: list[str] = []
    start = threading.Barrier(4)

    def claim(name: str) -> None:
        c = connection.open(db_file)
        start.wait()                     # all four claims hit the database at the same moment
        try:
            users.create_first_admin(c, name, PW, 'H')
            results.append('ok')
        except users.SetupDone:
            results.append('done')
        finally:
            c.close()
    threads = [threading.Thread(target=claim, args=('admin%d' % i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ['done', 'done', 'done', 'ok'] and users.user_count(conn) == 1
