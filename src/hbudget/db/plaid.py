"""Storage for the optional Plaid connection (migration 0010, docs/PLAID.md). No network here."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .connection import transaction

ROLES = ('bank', 'card', 'ignore')
AUTO_SYNC = ('off', 'daily')


# ---------------------------------------------------------------- sealing the access token

def token_key(secret_key: str) -> bytes:
    """The AES-256 key for access tokens, derived from the server's secret key (HMAC-SHA256 as the KDF)."""
    return hmac.new(secret_key.encode('utf-8'), b'hbudget plaid access token v1', hashlib.sha256).digest()


def seal(key: bytes, plaintext: str, aad: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode('utf-8'), aad.encode('utf-8'))
    return 'v1:' + base64.b64encode(nonce + ct).decode('ascii')


def unseal(key: bytes, sealed: str, aad: str) -> str:
    """Raises ValueError when the token was sealed with another key (a new secret key) or was changed."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not sealed.startswith('v1:'):
        raise ValueError('unknown token format')
    raw = base64.b64decode(sealed[3:])
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], aad.encode('utf-8')).decode('utf-8')
    except InvalidTag:
        raise ValueError('the bank connection cannot be read with this server\'s secret key') from None


def aad(hid: int, item_id: str) -> str:
    return '%d:%s' % (hid, item_id)


# ---------------------------------------------------------------- values

@dataclass(frozen=True, slots=True)
class Item:
    item_id: str
    institution: str
    token_sealed: str
    cursor: str | None
    linked_at: str
    linked_by: str | None
    last_sync_at: str | None
    last_error: str


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    item_id: str
    name: str
    mask: str
    type: str
    subtype: str
    role: str
    current: int | None


@dataclass(frozen=True, slots=True)
class StoredTxn:
    plaid_id: str
    account_id: str
    txn_id: str | None
    posted: date
    authorized: date | None
    amount: int
    description: str


def _now(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat(timespec='seconds')


# ---------------------------------------------------------------- household switch

def enabled(conn: sqlite3.Connection, hid: int) -> bool:
    return conn.execute('SELECT 1 FROM plaid_households WHERE household_id = ?', (hid,)).fetchone() is not None


def enable(conn: sqlite3.Connection, hid: int, by: str | None) -> None:
    with transaction(conn):
        conn.execute('INSERT INTO plaid_households (household_id, enabled_at, enabled_by) VALUES (?, ?, ?) '
                     'ON CONFLICT (household_id) DO NOTHING', (hid, _now(), by))


def disable(conn: sqlite3.Connection, hid: int) -> None:
    """Forget everything Plaid-related for the household. The postings already stored in the history stay."""
    with transaction(conn):
        for table in ('plaid_txns', 'plaid_accounts', 'plaid_items', 'plaid_households'):
            conn.execute('DELETE FROM %s WHERE household_id = ?' % table, (hid,))  # noqa: S608 - constant names


def auto_sync(conn: sqlite3.Connection, hid: int) -> str:
    r = conn.execute('SELECT auto_sync FROM plaid_households WHERE household_id = ?', (hid,)).fetchone()
    return str(r[0]) if r else 'off'


def set_auto_sync(conn: sqlite3.Connection, hid: int, value: str) -> None:
    if value not in AUTO_SYNC:
        raise ValueError(value)
    with transaction(conn):
        conn.execute('UPDATE plaid_households SET auto_sync = ? WHERE household_id = ?', (value, hid))


def auto_households(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT household_id FROM plaid_households WHERE auto_sync = 'daily' "
                                       'ORDER BY household_id')]


def claim(conn: sqlite3.Connection, hid: int, item_id: str, older_than: datetime, now: datetime | None = None) -> bool:
    """Mark the item as syncing now unless it synced after `older_than`: True = this caller syncs it. One atomic
    update, so a restart or a second server process never syncs the same bank twice in a day."""
    with transaction(conn):
        return conn.execute('UPDATE plaid_items SET last_sync_at = ? WHERE household_id = ? AND item_id = ? AND '
                            '(last_sync_at IS NULL OR last_sync_at < ?)',
                            (_now(now), hid, item_id, _now(older_than))).rowcount == 1


def forget_history(conn: sqlite3.Connection, hid: int) -> None:
    """For a wipe: the next sync starts over from the history Plaid keeps; links and roles stay."""
    with transaction(conn):
        conn.execute('DELETE FROM plaid_txns WHERE household_id = ?', (hid,))
        conn.execute('UPDATE plaid_items SET cursor = NULL WHERE household_id = ?', (hid,))


# ---------------------------------------------------------------- items and accounts

def _item(r: sqlite3.Row) -> Item:
    return Item(r['item_id'], r['institution'], r['token_sealed'], r['cursor'], r['linked_at'], r['linked_by'],
                r['last_sync_at'], r['last_error'])


def items(conn: sqlite3.Connection, hid: int) -> list[Item]:
    rows = conn.execute('SELECT * FROM plaid_items WHERE household_id = ? ORDER BY linked_at, item_id', (hid,))
    return [_item(r) for r in rows]


def get_item(conn: sqlite3.Connection, hid: int, item_id: str) -> Item | None:
    r = conn.execute('SELECT * FROM plaid_items WHERE household_id = ? AND item_id = ?', (hid, item_id)).fetchone()
    return _item(r) if r else None


def add_item(conn: sqlite3.Connection, hid: int, item_id: str, institution: str, token_sealed: str,
             by: str | None) -> None:
    with transaction(conn):
        conn.execute('INSERT INTO plaid_items (household_id, item_id, institution, token_sealed, linked_at, linked_by) '
                     'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (household_id, item_id) DO UPDATE SET '
                     'token_sealed = excluded.token_sealed, institution = excluded.institution, last_error = \'\'',
                     (hid, item_id, institution[:80], token_sealed, _now(), by))


def remove_item(conn: sqlite3.Connection, hid: int, item_id: str) -> None:
    with transaction(conn):
        ids = [r[0] for r in conn.execute('SELECT account_id FROM plaid_accounts WHERE household_id = ? AND '
                                          'item_id = ?', (hid, item_id))]
        conn.executemany('DELETE FROM plaid_txns WHERE household_id = ? AND account_id = ?', [(hid, a) for a in ids])
        conn.execute('DELETE FROM plaid_items WHERE household_id = ? AND item_id = ?', (hid, item_id))


def save_sync(conn: sqlite3.Connection, hid: int, item_id: str, cursor: str | None, error: str = '',
              now: datetime | None = None) -> None:
    with transaction(conn):
        conn.execute('UPDATE plaid_items SET cursor = COALESCE(?, cursor), last_sync_at = ?, last_error = ? '
                     'WHERE household_id = ? AND item_id = ?', (cursor, _now(now), error[:300], hid, item_id))


def accounts(conn: sqlite3.Connection, hid: int) -> list[Account]:
    rows = conn.execute('SELECT * FROM plaid_accounts WHERE household_id = ? ORDER BY item_id, name, account_id',
                        (hid,))
    return [Account(r['account_id'], r['item_id'], r['name'], r['mask'], r['type'], r['subtype'], r['role'],
                    r['current']) for r in rows]


def upsert_accounts(conn: sqlite3.Connection, hid: int, item_id: str,
                    found: Iterable[tuple[str, str, str, str, str, int | None]], roles: dict[str, str]) -> None:
    """(account_id, name, mask, type, subtype, current). A new account takes its role from `roles`; a known one
    keeps the role the household chose."""
    with transaction(conn):
        for account_id, name, mask, typ, subtype, current in found:
            conn.execute('INSERT INTO plaid_accounts (household_id, account_id, item_id, name, mask, type, subtype, '
                         'role, current) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (household_id, account_id) '
                         'DO UPDATE SET name = excluded.name, mask = excluded.mask, current = excluded.current',
                         (hid, account_id, item_id, name, mask, typ, subtype, roles.get(account_id, 'ignore'),
                          current))


def set_role(conn: sqlite3.Connection, hid: int, account_id: str, role: str) -> bool:
    """One reconciled (bank) account per household: choosing another moves the role. False = no such account."""
    if role not in ROLES:
        raise ValueError(role)
    with transaction(conn):
        if conn.execute('SELECT 1 FROM plaid_accounts WHERE household_id = ? AND account_id = ?',
                        (hid, account_id)).fetchone() is None:
            return False
        if role == 'bank':
            conn.execute("UPDATE plaid_accounts SET role = 'ignore' WHERE household_id = ? AND role = 'bank'", (hid,))
        conn.execute('UPDATE plaid_accounts SET role = ? WHERE household_id = ? AND account_id = ?',
                     (role, hid, account_id))
    return True


# ---------------------------------------------------------------- transactions

def store_txns(conn: sqlite3.Connection, hid: int,
               txns: Iterable[tuple[str, str, date, date | None, int, str]]) -> list[str]:
    """(plaid_id, account_id, posted, authorized, amount, description) of posted transactions; returns the ids
    that were new (a repeat of a known id changes nothing)."""
    new = []
    for plaid_id, account_id, posted, authorized, amount, desc in txns:
        cur = conn.execute('INSERT INTO plaid_txns (household_id, plaid_id, account_id, posted, authorized, amount, '
                           'description) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (household_id, plaid_id) DO NOTHING',
                           (hid, plaid_id, account_id, posted.isoformat(),
                            authorized.isoformat() if authorized else None, amount, desc))
        if cur.rowcount:
            new.append(plaid_id)
    return new


def mark_removed(conn: sqlite3.Connection, hid: int, plaid_ids: Sequence[str]) -> list[str]:
    """Plaid withdrew these postings; returns the stored ids they had (txn_id) so the caller can say so."""
    out = []
    for pid in plaid_ids:
        r = conn.execute('SELECT txn_id FROM plaid_txns WHERE household_id = ? AND plaid_id = ? AND removed = 0',
                         (hid, pid)).fetchone()
        if r is not None:
            conn.execute('UPDATE plaid_txns SET removed = 1 WHERE household_id = ? AND plaid_id = ?', (hid, pid))
            out.append(r[0] or '')
    return out


def known(conn: sqlite3.Connection, hid: int, plaid_ids: Sequence[str]) -> int:
    return sum(1 for pid in plaid_ids if conn.execute(
        'SELECT 1 FROM plaid_txns WHERE household_id = ? AND plaid_id = ? AND removed = 0', (hid, pid)).fetchone())


def account_txns(conn: sqlite3.Connection, hid: int, account_id: str, since: date | None = None,
                 only: Sequence[str] | None = None, unassigned: bool = False) -> list[StoredTxn]:
    """An account's stored postings (not withdrawn), oldest first. `unassigned`: only those no upload has taken
    yet (no txn_id), which is what the next card upload brings in."""
    rows = conn.execute('SELECT * FROM plaid_txns WHERE household_id = ? AND account_id = ? AND removed = 0 '
                        'AND (? IS NULL OR posted >= ?) AND (? = 0 OR txn_id IS NULL) ORDER BY posted, plaid_id',
                        (hid, account_id, since and since.isoformat(), since and since.isoformat(),
                         int(unassigned))).fetchall()
    want = set(only) if only is not None else None
    return [StoredTxn(r['plaid_id'], r['account_id'], r['txn_id'], date.fromisoformat(r['posted']),
                      date.fromisoformat(r['authorized']) if r['authorized'] else None, r['amount'], r['description'])
            for r in rows if want is None or r['plaid_id'] in want]


def assign_ids(conn: sqlite3.Connection, hid: int, table: str, txns: Sequence[StoredTxn]) -> dict[str, str]:
    """The id each Plaid posting is stored under in `table` (bank_txns or card_txns). A posting the household
    already has from a file upload (same date and amount, not a Plaid id, not claimed by another Plaid posting)
    keeps that id, so nothing is counted twice; the rest become 'plaid:<id>'. Saved, so it never changes."""
    if table not in ('bank_txns', 'card_txns'):
        raise ValueError(table)
    out: dict[str, str] = {}
    claimed = {r[0] for r in conn.execute('SELECT txn_id FROM plaid_txns WHERE household_id = ? AND txn_id IS NOT '
                                          'NULL', (hid,))}
    for t in txns:
        if t.txn_id:
            out[t.plaid_id] = t.txn_id
            continue
        cands = [r[0] for r in conn.execute(
            "SELECT id FROM %s WHERE household_id = ? AND posted = ? AND amount = ? AND id NOT LIKE 'plaid:%%' "  # noqa: S608
            'ORDER BY id' % table, (hid, t.posted.isoformat(), t.amount))]
        tid = next((c for c in cands if c not in claimed), 'plaid:' + t.plaid_id)
        claimed.add(tid)
        out[t.plaid_id] = tid
        conn.execute('UPDATE plaid_txns SET txn_id = ? WHERE household_id = ? AND plaid_id = ?', (tid, hid, t.plaid_id))
    return out
