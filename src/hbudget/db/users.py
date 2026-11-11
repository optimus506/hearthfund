"""Users, password hashes, sessions and login throttling (ARCHITECTURE 14.3, 16; D-035, D-036).

Times are UTC. Functions take `now` so tests can control the clock; the web layer passes nothing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .connection import transaction

__all__ = [
    'ABSOLUTE_SESSION', 'IP_FAILURES', 'IP_LOCK', 'MIN_PASSWORD', 'USER_FAILURES', 'WINDOW', 'Session', 'SetupDone',
    'User', 'check_password', 'create_first_admin', 'create_session', 'create_user', 'delete_session',
    'delete_user_sessions', 'get_session', 'get_user', 'get_user_by_name', 'hash_password', 'list_users', 'purge',
    'record_attempt', 'set_admin', 'set_disabled', 'set_password', 'too_many_attempts', 'user_count', 'verify_login',
]

MIN_PASSWORD = 10
_N, _R, _P, _DKLEN = 2 ** 15, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024
WINDOW = timedelta(minutes=15)       # failures are counted over this window
USER_FAILURES = 5                    # per username from one address: then a growing "reject until T" there
NAME_FAILURES = 25                   # per username from all addresses: then every address that failed is held
IP_FAILURES = 20                     # per IP, across usernames: then IP_LOCK
IP_LOCK = timedelta(minutes=15)
MAX_USER_DELAY = 60                  # seconds
ABSOLUTE_SESSION = timedelta(days=7)
ATTEMPT_RETENTION = timedelta(days=30)


class SetupDone(RuntimeError):
    """create_first_admin found an existing user: setup is already done."""


@dataclass(frozen=True, slots=True)
class User:
    id: int
    username: str
    is_admin: bool
    disabled: bool
    created_at: str
    password_changed_at: str | None
    two_factor: bool = False      # two-factor sign-in is on (hbudget.db.twofactor)


@dataclass(frozen=True, slots=True)
class Session:
    token_hash: str
    user_id: int
    csrf_token: str
    created_at: str
    last_seen: str
    expires_at: str


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec='seconds')


# ---------------------------------------------------------------- passwords
def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """scrypt(n=2**15, r=8, p=1) with a 16-byte salt: "scrypt$32768$8$1$<salt b64>$<hash b64>"."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)
    return 'scrypt$%d$%d$%d$%s$%s' % (_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def check_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split('$')
        if algo != 'scrypt':
            return False
        salt, want = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        got = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(want),
                             maxmem=_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


# A fixed hash checked for unknown usernames, so the timing does not tell which usernames exist.
DUMMY_HASH = hash_password('not a real password', salt=b'household-budget')


def _check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError('The password must have at least %d characters.' % MIN_PASSWORD)


# ---------------------------------------------------------------- users
def _user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(row['id'], row['username'], bool(row['is_admin']), bool(row['disabled']), row['created_at'],
                row['password_changed_at'], row['totp_enabled_at'] is not None)


def user_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0])


def create_user(conn: sqlite3.Connection, username: str, password: str, *, is_admin: bool = False,
                now: datetime | None = None) -> int:
    """A new user. ValueError for a short password or a username that is taken (case-insensitive)."""
    name = username.strip()
    if not name or len(name) > 64 or any(c.isspace() for c in name):
        raise ValueError('A username is 1 to 64 characters without spaces.')
    _check_new_password(password)
    return _insert_user(conn, name, hash_password(password), is_admin=is_admin, now=now)


def _insert_user(conn: sqlite3.Connection, name: str, password_hash: str, *, is_admin: bool,
                 now: datetime | None) -> int:
    at = _iso(_now(now))
    try:
        with transaction(conn):
            cur = conn.execute('INSERT INTO users (username, password_hash, is_admin, created_at, password_changed_at)'
                               ' VALUES (?, ?, ?, ?, ?)', (name, password_hash, int(is_admin), at, at))
    except sqlite3.IntegrityError:
        raise ValueError('That username is taken.') from None
    assert cur.lastrowid is not None
    return cur.lastrowid


def create_first_admin(conn: sqlite3.Connection, username: str, password: str, household_name: str, *,
                       now: datetime | None = None) -> tuple[int, int]:
    """First-run setup (D-035): in one BEGIN IMMEDIATE transaction, check there are no users, create the admin
    and the first household, make the admin its owner. Raises SetupDone when a user already exists."""
    from .repo import create_household

    name = username.strip()
    if not name or len(name) > 64 or any(c.isspace() for c in name):
        raise ValueError('A username is 1 to 64 characters without spaces.')
    _check_new_password(password)
    password_hash = hash_password(password)       # the slow part, before the write lock is taken
    with transaction(conn, immediate=True):
        if user_count(conn):
            raise SetupDone('setup is already done')
        uid = _insert_user(conn, name, password_hash, is_admin=True, now=now)
        hid = create_household(conn, household_name, now=now)
        conn.execute("INSERT INTO memberships (user_id, household_id, role) VALUES (?, ?, 'owner')", (uid, hid))
    return uid, hid


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    return _user(conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone())


def get_user_by_name(conn: sqlite3.Connection, username: str) -> User | None:
    return _user(conn.execute('SELECT * FROM users WHERE username = ?', (username.strip(),)).fetchone())


def list_users(conn: sqlite3.Connection) -> list[User]:
    return [u for u in (_user(r) for r in conn.execute('SELECT * FROM users ORDER BY username')) if u]


def set_password(conn: sqlite3.Connection, user_id: int, password: str, *, keep_token: str | None = None,
                 now: datetime | None = None) -> None:
    """New password; every session of the user is deleted except `keep_token` (a self-service change)."""
    _check_new_password(password)
    with transaction(conn):
        cur = conn.execute('UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?',
                           (hash_password(password), _iso(_now(now)), user_id))
        if cur.rowcount != 1:
            raise KeyError(user_id)
        delete_user_sessions(conn, user_id, except_token=keep_token)
        conn.execute('DELETE FROM login_challenges WHERE user_id = ?', (user_id,))   # a half-done sign-in (D-095)


def set_disabled(conn: sqlite3.Connection, user_id: int, disabled: bool) -> None:
    with transaction(conn):
        if conn.execute('UPDATE users SET disabled = ? WHERE id = ?', (int(disabled), user_id)).rowcount != 1:
            raise KeyError(user_id)
        if disabled:
            delete_user_sessions(conn, user_id)
            conn.execute('DELETE FROM login_challenges WHERE user_id = ?', (user_id,))


def set_admin(conn: sqlite3.Connection, user_id: int, is_admin: bool) -> None:
    with transaction(conn):
        if conn.execute('UPDATE users SET is_admin = ? WHERE id = ?', (int(is_admin), user_id)).rowcount != 1:
            raise KeyError(user_id)


def verify_login(conn: sqlite3.Connection, username: str, password: str) -> int | None:
    """The user id when the password is right and the user is enabled. Unknown users hash a dummy (same timing)."""
    row = conn.execute('SELECT id, password_hash, disabled FROM users WHERE username = ?',
                       (username.strip(),)).fetchone()
    if row is None:
        check_password(password, DUMMY_HASH)
        return None
    ok = check_password(password, row['password_hash'])
    return int(row['id']) if ok and not row['disabled'] else None


# ---------------------------------------------------------------- sessions
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int, *, user_agent: str = '', idle_hours: float = 12,
                   now: datetime | None = None) -> tuple[str, str]:
    """(token, csrf_token). Only sha256(token) is stored."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    t = _now(now)
    expires = min(t + timedelta(hours=idle_hours), t + ABSOLUTE_SESSION)
    with transaction(conn):
        conn.execute('INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen, expires_at, '
                     'user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (_token_hash(token), user_id, csrf, _iso(t), _iso(t), _iso(expires), user_agent[:200]))
    return token, csrf


def get_session(conn: sqlite3.Connection, token: str, *, idle_hours: float = 12,
                now: datetime | None = None) -> Session | None:
    """The live session for a cookie token (sliding idle expiry, 7-day absolute limit), or None.

    A session of a disabled or deleted user is deleted."""
    if not token:
        return None
    th = _token_hash(token)
    row = conn.execute('SELECT s.*, u.disabled FROM sessions s JOIN users u ON u.id = s.user_id '
                       'WHERE s.token_hash = ?', (th,)).fetchone()
    t = _now(now)
    if row is None:
        return None
    absolute = datetime.fromisoformat(row['created_at']) + ABSOLUTE_SESSION
    if row['disabled'] or datetime.fromisoformat(row['expires_at']) <= t or absolute <= t:
        with transaction(conn):
            conn.execute('DELETE FROM sessions WHERE token_hash = ?', (th,))
        return None
    expires = min(t + timedelta(hours=idle_hours), absolute)
    with transaction(conn):
        conn.execute('UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?',
                     (_iso(t), _iso(expires), th))
    return Session(th, int(row['user_id']), row['csrf_token'], row['created_at'], _iso(t), _iso(expires))


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    with transaction(conn):
        conn.execute('DELETE FROM sessions WHERE token_hash = ?', (_token_hash(token),))


def delete_user_sessions(conn: sqlite3.Connection, user_id: int, *, except_token: str | None = None) -> int:
    keep = _token_hash(except_token) if except_token else ''
    with transaction(conn):
        return conn.execute('DELETE FROM sessions WHERE user_id = ? AND token_hash != ?', (user_id, keep)).rowcount


# ---------------------------------------------------------------- throttling (D-036)
def _pair(name: str, ip: str) -> str:
    return '%s\x1f%s' % (name, ip)       # a username cannot hold a control character


def _hold(conn: sqlite3.Connection, kind: str, key: str, until: datetime, failures: int) -> None:
    conn.execute('INSERT INTO login_backoff (kind, key, reject_until, failures) VALUES (?, ?, ?, ?) '
                 'ON CONFLICT (kind, key) DO UPDATE SET reject_until = excluded.reject_until, '
                 'failures = excluded.failures', (kind, key, _iso(until), failures))


def _delay(failures: int, limit: int) -> timedelta:
    return timedelta(seconds=min(2 ** (failures - limit + 1), MAX_USER_DELAY))


def record_attempt(conn: sqlite3.Connection, username: str, ip: str, ok: bool, *, clears: bool = True,
                   now: datetime | None = None) -> None:
    """Log a sign-in attempt and update the back-off (D-036, D-097).

    - Per username and address: after 5 failures in 15 minutes a growing "reject until T" (2 s, 4 s, ... 60 s)
      for that address. Someone guessing from elsewhere does not hold back the real user.
    - Per username, all addresses: after 25 failures, the same delay for every address that has failed for
      that name in the window, so spreading guesses over many addresses buys one try each, not five.
    - Per address: 15 minutes after 20 failures, whatever the usernames.
    A success clears the back-off of that name at that address, unless `clears` is False (a right password of
    a user with two-factor sign-in proves only half)."""
    t = _now(now)
    since = _iso(t - WINDOW)
    name = username.strip()[:64]
    with transaction(conn):
        conn.execute('INSERT INTO login_attempts (username, ip, at, ok) VALUES (?, ?, ?, ?)',
                     (name, ip, _iso(t), int(ok)))
        if ok:
            if clears:
                conn.execute("DELETE FROM login_backoff WHERE kind = 'user' AND key = ?", (_pair(name, ip),))
            return
        pair_fail = conn.execute('SELECT COUNT(*) FROM login_attempts WHERE username = ? AND ip = ? AND ok = 0 '
                                 'AND at > ?', (name, ip, since)).fetchone()[0]
        if pair_fail >= USER_FAILURES:
            _hold(conn, 'user', _pair(name, ip), t + _delay(pair_fail, USER_FAILURES), pair_fail)
        name_fail = conn.execute('SELECT COUNT(*) FROM login_attempts WHERE username = ? AND ok = 0 AND at > ?',
                                 (name, since)).fetchone()[0]
        if name_fail >= NAME_FAILURES:
            _hold(conn, 'user', name, t + _delay(name_fail, NAME_FAILURES), name_fail)
        ip_fail = conn.execute('SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND ok = 0 AND at > ?',
                               (ip, since)).fetchone()[0]
        if ip_fail >= IP_FAILURES:
            _hold(conn, 'ip', ip, t + IP_LOCK, ip_fail)


def too_many_attempts(conn: sqlite3.Connection, username: str, ip: str, *, now: datetime | None = None) -> int | None:
    """Seconds to wait (for a 429 Retry-After) when this name at this address, or the address, is held back,
    else None. The hold on a name across all addresses applies only to addresses that failed for it lately."""
    t = _now(now)
    name = username.strip()[:64]
    rows = conn.execute("SELECT kind, key, reject_until FROM login_backoff WHERE (kind = 'user' AND key IN (?, ?)) "
                        "OR (kind = 'ip' AND key = ?)", (_pair(name, ip), name, ip)).fetchall()
    waits = []
    for r in rows:
        if r['kind'] == 'user' and r['key'].lower() == name.lower():
            failed_here = conn.execute('SELECT 1 FROM login_attempts WHERE username = ? AND ip = ? AND ok = 0 AND '
                                       'at > ? LIMIT 1', (name, ip, _iso(t - WINDOW))).fetchone()
            if failed_here is None:
                continue
        waits.append((datetime.fromisoformat(r['reject_until']) - t).total_seconds())
    wait = max(waits, default=0.0)
    return int(wait) + 1 if wait > 0 else None


def purge(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, int]:
    """Housekeeping (at start-up and daily): expired sessions, login attempts older than 30 days, old back-offs."""
    t = _now(now)
    with transaction(conn):
        sessions = conn.execute('DELETE FROM sessions WHERE expires_at <= ? OR created_at <= ?',
                                (_iso(t), _iso(t - ABSOLUTE_SESSION))).rowcount
        attempts = conn.execute('DELETE FROM login_attempts WHERE at < ?', (_iso(t - ATTEMPT_RETENTION),)).rowcount
        backoff = conn.execute('DELETE FROM login_backoff WHERE reject_until < ?', (_iso(t - WINDOW),)).rowcount
        conn.execute('DELETE FROM login_challenges WHERE expires_at <= ?', (_iso(t),))
        conn.execute('DELETE FROM code_failures WHERE at < ?', (_iso(t - ATTEMPT_RETENTION),))
    return {'sessions': sessions, 'login_attempts': attempts, 'login_backoff': backoff}
