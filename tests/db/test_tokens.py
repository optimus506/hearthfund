"""db.tokens: access tokens are stored hashed, shown once, expire, revoke, and name the audit entries they make."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hbudget.db import connection, migrate, repo, tokens, users

T0 = datetime(2029, 3, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path):  # type: ignore[no-untyped-def]
    conn = connection.connect(tmp_path / 'hbudget.db')
    migrate.migrate(conn, tmp_path / 'hbudget.db')
    uid = users.create_user(conn, 'owner1', 'correct horse battery')
    hid = repo.create_household(conn, 'Test household')
    repo.add_member(conn, hid, uid, 'owner')
    yield conn, hid, uid
    conn.close()


def test_create_stores_only_a_hash(db):
    conn, hid, uid = db
    tok, secret = tokens.create(conn, hid, uid, ' Laptop agent ', 'read', now=T0)
    assert secret.startswith('hbt_') and len(secret) >= 40
    assert tok.name == 'Laptop agent' and tok.scope == 'read' and tok.hint == secret[-4:] and tok.creator == 'owner1'
    stored = conn.execute('SELECT token_hash FROM api_tokens').fetchone()[0]
    assert stored == tokens.digest(secret) and secret not in stored
    assert tokens.lookup(conn, secret, now=T0) == tok
    assert tokens.lookup(conn, secret + 'x', now=T0) is None
    assert tokens.lookup(conn, 'not-a-token', now=T0) is None


def test_expiry_and_revoke(db):
    conn, hid, uid = db
    tok, secret = tokens.create(conn, hid, uid, 'Short', 'write', days=1, now=T0)
    assert tokens.lookup(conn, secret, now=T0 + timedelta(hours=23)) is not None
    assert tokens.lookup(conn, secret, now=T0 + timedelta(days=1)) is None
    tok2, secret2 = tokens.create(conn, hid, uid, 'Other', 'write', now=T0)
    assert tokens.revoke(conn, hid, tok2.id, now=T0) and not tokens.revoke(conn, hid, tok2.id, now=T0)
    assert tokens.lookup(conn, secret2, now=T0) is None
    assert not tokens.revoke(conn, hid + 1, tok.id)            # another household's id: nothing happens
    assert [t.name for t in tokens.list_for(conn, hid)] == ['Other', 'Short']


@pytest.mark.parametrize(('kw', 'msg'), [({'name': ''}, 'name'), ({'name': 'x' * 61}, 'name'),
                                          ({'scope': 'admin'}, 'scope'), ({'days': 0}, 'expiry')])
def test_bad_input(db, kw, msg):
    conn, hid, uid = db
    args = {'name': 'Fine', 'scope': 'read', **kw}
    with pytest.raises(ValueError, match=msg):
        tokens.create(conn, hid, uid, args.pop('name'), args.pop('scope'), **args)


def test_limit_per_household(db, monkeypatch):
    conn, hid, uid = db
    monkeypatch.setattr(tokens, 'MAX_PER_HOUSEHOLD', 2)
    tokens.create(conn, hid, uid, 'a', 'read')
    b, _ = tokens.create(conn, hid, uid, 'b', 'read')
    with pytest.raises(ValueError, match='revoke'):
        tokens.create(conn, hid, uid, 'c', 'read')
    tokens.revoke(conn, hid, b.id)
    tokens.create(conn, hid, uid, 'c', 'read')


def test_touch_writes_at_most_once_a_minute(db):
    conn, hid, uid = db
    tok, secret = tokens.create(conn, hid, uid, 'a', 'read', now=T0)
    tokens.touch(conn, tok, now=T0)
    tok = tokens.lookup(conn, secret, now=T0)
    assert tok and tok.last_used_at == T0.isoformat()
    tokens.touch(conn, tok, now=T0 + timedelta(seconds=30))
    assert tokens.get(conn, hid, tok.id).last_used_at == T0.isoformat()  # type: ignore[union-attr]
    tokens.touch(conn, tok, now=T0 + timedelta(minutes=2))
    assert tokens.get(conn, hid, tok.id).last_used_at == (T0 + timedelta(minutes=2)).isoformat()  # type: ignore[union-attr]


def test_audit_entries_name_the_token(db):
    conn, hid, uid = db
    repo.audit(conn, household_id=hid, user_id=uid, action='plain')
    with repo.audit_via('token: Laptop agent'):
        repo.audit(conn, household_id=hid, user_id=uid, action='by.token', detail={'n': 1})
    repo.audit(conn, household_id=hid, user_id=uid, action='after')
    got = {a['action']: a['detail'] for a in repo.list_audit(conn, hid)}
    assert got['by.token'] == {'n': 1, 'via': 'token: Laptop agent'}
    assert 'via' not in got['plain'] and 'via' not in got['after']
