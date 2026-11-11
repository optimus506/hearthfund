"""Two-factor sign-in: authenticator secrets, used time steps, recovery codes and the half-signed-in step (D-095).

The code maths is in hbudget.core.totp. Times are UTC; functions take `now` so tests control the clock.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hbudget.core import totp

from .connection import transaction
from .users import delete_user_sessions

__all__ = ['CHALLENGE_MINUTES', 'CODE_FAILURES', 'PENDING_MINUTES', 'RECOVERY_CODES', 'REUSED', 'State', 'begin',
           'cancel', 'challenge_user', 'check', 'check_code', 'codes_locked', 'confirm', 'disable', 'drop_challenge',
           'failures', 'new_challenge', 'new_recovery_codes', 'state']

RECOVERY_CODES = 10
CHALLENGE_MINUTES = 5
CODE_FAILURES = 10        # wrong codes in CODE_WINDOW: then codes from the app are refused (D-097)
CODE_WINDOW = timedelta(hours=24)
REUSED = 'reused'         # a right code for a step that was already used: not a guess
PENDING_MINUTES = 10     # an enrolment that was started must be confirmed within this, by the same session


@dataclass(frozen=True, slots=True)
class State:
    enabled: bool
    enabled_at: str | None
    pending_secret: str | None      # enrolment started but not confirmed yet
    codes_left: int


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec='seconds')


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _clean_recovery(code: str) -> str:
    return ''.join(ch for ch in code.lower() if ch.isalnum())


def _pending(row: sqlite3.Row, session: str | None, t: datetime) -> bool:
    """True when an enrolment is waiting that this session started a short while ago."""
    if row['totp_enabled_at'] is not None or not row['totp_secret'] or not session or not row['totp_pending_at']:
        return False
    if row['totp_pending_session'] != _hash(session):
        return False
    return datetime.fromisoformat(row['totp_pending_at']) + timedelta(minutes=PENDING_MINUTES) > t


_SELECT = ('SELECT totp_secret, totp_enabled_at, totp_pending_session, totp_pending_at FROM users '
           'WHERE id = ?')


def state(conn: sqlite3.Connection, user_id: int, *, session: str | None = None,
          now: datetime | None = None) -> State:
    """`pending_secret` is set only for the session that started the enrolment (it gave the password), and only
    for PENDING_MINUTES: another session of the same user never sees a key that is about to be trusted."""
    row = conn.execute(_SELECT, (user_id,)).fetchone()
    if row is None:
        raise KeyError(user_id)
    on = row['totp_enabled_at'] is not None
    left = conn.execute('SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL',
                        (user_id,)).fetchone()[0] if on else 0
    return State(on, row['totp_enabled_at'], row['totp_secret'] if _pending(row, session, _now(now)) else None, left)


def begin(conn: sqlite3.Connection, user_id: int, *, session: str, now: datetime | None = None) -> str:
    """Start (or restart) enrolment for this session: a new secret that does nothing until `confirm`. Refused
    when 2FA is on."""
    secret = base64.b32encode(secrets.token_bytes(20)).decode('ascii')
    with transaction(conn):
        cur = conn.execute('UPDATE users SET totp_secret = ?, totp_last_step = NULL, totp_pending_session = ?, '
                           'totp_pending_at = ? WHERE id = ? AND totp_enabled_at IS NULL',
                           (secret, _hash(session), _iso(_now(now)), user_id))
        if cur.rowcount != 1:
            raise ValueError('Two-factor sign-in is already on.')
    return secret


def cancel(conn: sqlite3.Connection, user_id: int) -> bool:
    """Forget an enrolment that was not confirmed. Never touches two-factor sign-in that is on."""
    with transaction(conn):
        cur = conn.execute('UPDATE users SET totp_secret = NULL, totp_last_step = NULL, totp_pending_session = NULL, '
                           'totp_pending_at = NULL WHERE id = ? AND totp_enabled_at IS NULL AND '
                           'totp_secret IS NOT NULL', (user_id,))
        return cur.rowcount == 1


def _accept_totp(conn: sqlite3.Connection, user_id: int, code: str, t: datetime) -> str | None:
    """'ok' when `code` is a current code not used before (the step is recorded in the same statement), REUSED
    when it is right but its step was already used, else None."""
    row = conn.execute('SELECT totp_secret, totp_last_step FROM users WHERE id = ?', (user_id,)).fetchone()
    if row is None or not row['totp_secret']:
        return None
    step = totp.verify(row['totp_secret'], code, t.timestamp(), after_step=row['totp_last_step'])
    if step is None:
        return REUSED if totp.verify(row['totp_secret'], code, t.timestamp()) is not None else None
    cur = conn.execute('UPDATE users SET totp_last_step = ? WHERE id = ? AND '
                       '(totp_last_step IS NULL OR totp_last_step < ?)', (step, user_id, step))
    return 'ok' if cur.rowcount == 1 else REUSED


def failures(conn: sqlite3.Connection, user_id: int, *, now: datetime | None = None) -> int:
    """Wrong codes in the last CODE_WINDOW."""
    since = _iso(_now(now) - CODE_WINDOW)
    return int(conn.execute('SELECT COUNT(*) FROM code_failures WHERE user_id = ? AND at > ?',
                            (user_id, since)).fetchone()[0])


def codes_locked(conn: sqlite3.Connection, user_id: int, *, now: datetime | None = None) -> bool:
    """True while codes from the app are refused: someone who has the password has been guessing (D-097).
    Recovery codes, which cannot be guessed, still work; so do the admin reset and `hbudget disable-2fa`."""
    return failures(conn, user_id, now=now) >= CODE_FAILURES


def _fresh_codes(conn: sqlite3.Connection, user_id: int, t: datetime) -> list[str]:
    conn.execute('DELETE FROM recovery_codes WHERE user_id = ?', (user_id,))
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = base64.b32encode(secrets.token_bytes(10)).decode('ascii').lower()[:12]
        codes.append('%s-%s-%s' % (raw[:4], raw[4:8], raw[8:]))
        conn.execute('INSERT INTO recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)',
                     (user_id, _hash(_clean_recovery(codes[-1])), _iso(t)))
    return codes


def confirm(conn: sqlite3.Connection, user_id: int, code: str, *, session: str,
            now: datetime | None = None) -> list[str] | None:
    """Finish enrolment with a current code, from the session that started it: turns 2FA on, signs out the user's
    other sessions and returns the recovery codes (shown once; only their hashes are kept). None when the code is
    wrong or this session has nothing pending."""
    t = _now(now)
    with transaction(conn, immediate=True):
        row = conn.execute(_SELECT, (user_id,)).fetchone()
        if row is None or not _pending(row, session, t) or _accept_totp(conn, user_id, code, t) != 'ok':
            return None
        conn.execute('UPDATE users SET totp_enabled_at = ?, totp_pending_session = NULL, totp_pending_at = NULL '
                     'WHERE id = ?', (_iso(t), user_id))
        codes = _fresh_codes(conn, user_id, t)
        delete_user_sessions(conn, user_id, except_token=session)
    return codes


def check(conn: sqlite3.Connection, user_id: int, code: str, *, recovery: bool = True,
          now: datetime | None = None) -> str | None:
    """'totp' or 'recovery' when the code is right for a user with 2FA on; REUSED for a right code whose time
    step was already used (not counted as a guess); else None, and the wrong code is counted. A time step and a
    recovery code are each accepted once. While `codes_locked`, codes from the app are refused unchecked."""
    t = _now(now)
    with transaction(conn, immediate=True):
        row = conn.execute('SELECT totp_enabled_at FROM users WHERE id = ?', (user_id,)).fetchone()
        if row is None or row['totp_enabled_at'] is None:
            return None
        locked = codes_locked(conn, user_id, now=t)
        if not locked:
            got = _accept_totp(conn, user_id, code, t)
            if got == 'ok':
                conn.execute('DELETE FROM code_failures WHERE user_id = ?', (user_id,))
                return 'totp'
            if got == REUSED:
                return REUSED
        cleaned = _clean_recovery(code)
        if recovery and len(cleaned) == 12:
            cur = conn.execute('UPDATE recovery_codes SET used_at = ? WHERE user_id = ? AND code_hash = ? AND '
                               'used_at IS NULL', (_iso(t), user_id, _hash(cleaned)))
            if cur.rowcount == 1:
                return 'recovery'
        if not locked:
            conn.execute('INSERT INTO code_failures (user_id, at) VALUES (?, ?)', (user_id, _iso(t)))
    return None


def check_code(conn: sqlite3.Connection, user_id: int, code: str, *, recovery: bool = True,
               now: datetime | None = None) -> str | None:
    """`check` for callers that only need yes or no: 'totp', 'recovery' or None."""
    how = check(conn, user_id, code, recovery=recovery, now=now)
    return how if how in ('totp', 'recovery') else None


def new_recovery_codes(conn: sqlite3.Connection, user_id: int, *, now: datetime | None = None) -> list[str]:
    """Replace every recovery code (the old ones stop working)."""
    with transaction(conn):
        if conn.execute('SELECT totp_enabled_at FROM users WHERE id = ?', (user_id,)).fetchone()[0] is None:
            raise ValueError('Two-factor sign-in is not on.')
        return _fresh_codes(conn, user_id, _now(now))


def disable(conn: sqlite3.Connection, user_id: int, *, keep_token: str | None = None) -> bool:
    """Turn 2FA off and forget the secret and the codes; the user's sessions are signed out (all but
    `keep_token`). False when it was not on (a pending enrolment is `cancel`'s business)."""
    with transaction(conn, immediate=True):
        cur = conn.execute('UPDATE users SET totp_secret = NULL, totp_enabled_at = NULL, totp_last_step = NULL, '
                           'totp_pending_session = NULL, totp_pending_at = NULL '
                           'WHERE id = ? AND totp_enabled_at IS NOT NULL', (user_id,))
        if cur.rowcount == 1:
            conn.execute('DELETE FROM recovery_codes WHERE user_id = ?', (user_id,))
            conn.execute('DELETE FROM login_challenges WHERE user_id = ?', (user_id,))
            conn.execute('DELETE FROM code_failures WHERE user_id = ?', (user_id,))
            delete_user_sessions(conn, user_id, except_token=keep_token)
        return cur.rowcount == 1


# ---------------------------------------------------------------- between the password and the code
def new_challenge(conn: sqlite3.Connection, user_id: int, *, now: datetime | None = None) -> str:
    """A token for the browser that just gave the right password. Only its hash is stored; it lasts minutes."""
    token = secrets.token_urlsafe(32)
    t = _now(now)
    with transaction(conn):
        conn.execute('DELETE FROM login_challenges WHERE user_id = ? OR expires_at <= ?', (user_id, _iso(t)))
        conn.execute('INSERT INTO login_challenges (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)',
                     (_hash(token), user_id, _iso(t), _iso(t + timedelta(minutes=CHALLENGE_MINUTES))))
    return token


def challenge_user(conn: sqlite3.Connection, token: str | None, *, now: datetime | None = None) -> int | None:
    """The user a live challenge token belongs to (enabled, with 2FA on), else None."""
    if not token:
        return None
    row = conn.execute('SELECT c.user_id, c.expires_at, u.disabled, u.totp_enabled_at FROM login_challenges c '
                       'JOIN users u ON u.id = c.user_id WHERE c.token_hash = ?', (_hash(token),)).fetchone()
    if row is None or row['disabled'] or row['totp_enabled_at'] is None:
        return None
    if datetime.fromisoformat(row['expires_at']) <= _now(now):
        return None
    return int(row['user_id'])


def drop_challenge(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        with transaction(conn):
            conn.execute('DELETE FROM login_challenges WHERE token_hash = ?', (_hash(token),))
