"""Access tokens for /api/v1 and /mcp (D-068).

A token is `hbt_` + 40 random URL-safe characters. Only its SHA-256 is stored (the token is long and random, so
a fast hash is enough: there is nothing to guess). `hint` keeps the last four characters so the owner can tell
tokens apart. A token belongs to one household and to the member who made it; it can never do more than that
member's current role (web/routes/api_v1.py applies the cap on every request).
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hbudget.db.connection import transaction

PREFIX = 'hbt_'
SCOPES = ('read', 'write')
MAX_PER_HOUSEHOLD = 50
TOUCH_EVERY = timedelta(minutes=1)   # last_used_at is written at most this often per token


@dataclass(frozen=True)
class Token:
    id: int
    household_id: int
    created_by: int
    creator: str | None
    name: str
    scope: str
    hint: str
    created_at: str
    expires_at: str | None
    last_used_at: str | None
    revoked_at: str | None

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or datetime.fromisoformat(self.expires_at) > now)


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _row(r: sqlite3.Row) -> Token:
    return Token(r['id'], r['household_id'], r['created_by'], r['creator'], r['name'], r['scope'], r['hint'],
                 r['created_at'], r['expires_at'], r['last_used_at'], r['revoked_at'])


_SELECT = ('SELECT t.*, u.username AS creator FROM api_tokens t LEFT JOIN users u ON u.id = t.created_by ')


def create(conn: sqlite3.Connection, hid: int, user_id: int, name: str, scope: str, *,
           days: int | None = None, now: datetime | None = None) -> tuple[Token, str]:
    """A new token; returns it and the secret, which is not stored and cannot be shown again."""
    if scope not in SCOPES:
        raise ValueError('scope must be read or write')
    name = name.strip()
    if not 1 <= len(name) <= 60:
        raise ValueError('Give the token a name of 1 to 60 characters.')
    if days is not None and not 1 <= days <= 3650:
        raise ValueError('The expiry must be 1 to 3650 days.')
    at = _now(now)
    secret = PREFIX + secrets.token_urlsafe(30)
    with transaction(conn, immediate=True):
        n = conn.execute('SELECT COUNT(*) FROM api_tokens WHERE household_id = ? AND revoked_at IS NULL',
                         (hid,)).fetchone()[0]
        if n >= MAX_PER_HOUSEHOLD:
            raise ValueError('This household already has %d tokens; revoke some first.' % MAX_PER_HOUSEHOLD)
        cur = conn.execute(
            'INSERT INTO api_tokens (household_id, created_by, name, scope, token_hash, hint, created_at, expires_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (hid, user_id, name, scope, digest(secret), secret[-4:], at.isoformat(),
             (at + timedelta(days=days)).isoformat() if days else None))
        tid = cur.lastrowid
    tok = get(conn, hid, int(tid or 0))
    assert tok is not None
    return tok, secret


def get(conn: sqlite3.Connection, hid: int, token_id: int) -> Token | None:
    r = conn.execute(_SELECT + 'WHERE t.household_id = ? AND t.id = ?', (hid, token_id)).fetchone()
    return _row(r) if r else None


def list_for(conn: sqlite3.Connection, hid: int) -> list[Token]:
    """Every token of the household, newest first (revoked ones too, so the owner sees what was used)."""
    return [_row(r) for r in conn.execute(_SELECT + 'WHERE t.household_id = ? ORDER BY t.id DESC', (hid,))]


def revoke(conn: sqlite3.Connection, hid: int, token_id: int, *, now: datetime | None = None) -> bool:
    with transaction(conn):
        cur = conn.execute('UPDATE api_tokens SET revoked_at = ? WHERE household_id = ? AND id = ? '
                           'AND revoked_at IS NULL', (_now(now).isoformat(), hid, token_id))
    return cur.rowcount == 1


def lookup(conn: sqlite3.Connection, secret: str, *, now: datetime | None = None) -> Token | None:
    """The active token with this secret, or None (unknown, revoked or expired: callers cannot tell which)."""
    if not secret.startswith(PREFIX) or len(secret) > 200:
        return None
    r = conn.execute(_SELECT + 'WHERE t.token_hash = ?', (digest(secret),)).fetchone()
    tok = _row(r) if r else None
    return tok if tok and tok.active(_now(now)) else None


def touch(conn: sqlite3.Connection, tok: Token, *, now: datetime | None = None) -> None:
    at = _now(now)
    if tok.last_used_at and at - datetime.fromisoformat(tok.last_used_at) < TOUCH_EVERY:
        return
    with transaction(conn):
        conn.execute('UPDATE api_tokens SET last_used_at = ? WHERE id = ?', (at.isoformat(), tok.id))
