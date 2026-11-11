from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hbudget.db import connection, migrate

NOW = datetime(2029, 1, 5, 12, 0, tzinfo=UTC)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_available_is_numbered_without_gaps() -> None:
    ms = migrate.available()
    assert [m.version for m in ms] == list(range(1, len(ms) + 1)) and ms[0].name == 'init'
    assert repr(ms[0]) == "Migration(1, 'init')"


def test_from_empty_and_idempotent(db_file: Path) -> None:
    c = connection.open(db_file)
    assert migrate.current_version(c) == 0
    assert migrate.migrate(c, db_file, now=NOW) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert migrate.current_version(c) == 14 and migrate.applied(c) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert {'users', 'households', 'ledger_rows', 'upload_txns', 'audit_log', 'schema_migrations'} <= _tables(c)
    assert migrate.migrate(c, db_file) == []
    assert not (db_file.parent / 'backups').exists()        # an empty database needs no backup
    assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    for f in db_file.parent.iterdir():
        assert _mode(f) == 0o600, f.name
    c.close()


def test_step_by_step_upgrade_keeps_data_and_backs_up(db_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    every = migrate.available()
    monkeypatch.setattr(migrate, 'available', lambda: every[:1])          # a database made by version 1 only
    c = connection.open(db_file)
    assert migrate.migrate(c, db_file, now=NOW) == [1]
    c.execute("INSERT INTO households (name, as_of, created_at, updated_at) VALUES ('H', '2029-01-01', 'x', 'x')")
    c.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('u', 'h', 'x')")
    c.execute("INSERT INTO ledger_rows (household_id, id, name, kind, amount, due_date, order_date, seq, state) "
              "VALUES (1, 'r1', 'Rent', 'bill', 90000, '2029-01-03', '2029-01-03', 1, 'open')")
    c.execute("INSERT INTO uploads (household_id, id, kind, uploaded_at, status) VALUES (1, 'u1', 'bank', 'x', "
              "'imported')")
    c.execute("INSERT INTO bank_txns (household_id, id, first_upload_id, posted, amount, description) "
              "VALUES (1, 't1', 'u1', '2029-01-02', -5000, 'SHOP')")
    c.execute("INSERT INTO card_txns (household_id, id, first_upload_id, purchased, posted, amount, description, "
              "category) VALUES (1, 'k1', 'u1', '2029-01-01', '2029-01-02', -100, 'CAFE', 'Other')")
    c.execute("INSERT INTO audit_log (household_id, at, user_id, action) VALUES (1, 'x', 1, 'old.entry')")
    c.commit()
    monkeypatch.setattr(migrate, 'available', lambda: every)
    assert migrate.migrate(c, db_file, now=NOW) == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert c.execute('SELECT name, version FROM households').fetchone()[:] == ('H', 0)
    assert c.execute('SELECT username FROM users').fetchone()[0] == 'u'
    assert c.execute('SELECT name, amount FROM ledger_rows').fetchone()[:] == ('Rent', 90000)
    assert c.execute('SELECT id, amount FROM bank_txns').fetchone()[:] == ('t1', -5000)
    assert c.execute('SELECT action, by_name FROM audit_log').fetchone()[:] == ('old.entry', None)
    assert c.execute('SELECT hidden FROM uploads').fetchone()[0] == 0
    assert c.execute('SELECT upload_id, txn_id FROM card_upload_txns').fetchone()[:] == ('u1', 'k1')   # backfilled
    backups = list((db_file.parent / 'backups').iterdir())
    assert [b.name for b in backups] == ['hbudget-before-0002-20290105T120000Z.db']
    assert _mode(backups[0]) == 0o600
    old = sqlite3.connect(str(backups[0]))
    assert old.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 1
    assert old.execute('SELECT name FROM households').fetchone()[0] == 'H'
    old.close()
    c.close()


def test_a_failing_migration_changes_nothing(db_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    every = migrate.available()
    broken = migrate.Migration(len(every) + 1, 'broken', 'CREATE TABLE half (x);\nSELECT nope FROM missing;')
    monkeypatch.setattr(migrate, 'available', lambda: [*every, broken])
    c = connection.open(db_file)
    with pytest.raises(sqlite3.OperationalError):
        migrate.migrate(c, None)
    assert migrate.current_version(c) == len(every)                # the good ones are kept, the broken one is not
    assert 'half' not in _tables(c)
    c.close()


def test_gap_in_numbering_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = tmp_path / 'migrations'
    folder.mkdir()
    (folder / '0001_a.sql').write_text('SELECT 1;')
    (folder / '0003_c.sql').write_text('SELECT 1;')
    (folder / 'README.txt').write_text('ignored')

    class Files:
        def joinpath(self, _name: str) -> Path:
            return folder
    monkeypatch.setattr(migrate.resources, 'files', lambda _pkg: Files())
    with pytest.raises(RuntimeError, match='without gaps'):
        migrate.available()


def test_memory_database_and_private_umask(tmp_path: Path) -> None:
    c = connection.open(':memory:')
    assert migrate.migrate(c, ':memory:') == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    c.close()
    assert connection.db_path(tmp_path) == tmp_path / 'hbudget.db'
    with connection.private_umask():
        (tmp_path / 'secret').write_text('x')
    assert _mode(tmp_path / 'secret') == 0o600


def test_transaction_commits_rolls_back_and_nests(conn: sqlite3.Connection) -> None:
    with connection.transaction(conn):
        conn.execute("INSERT INTO app_settings VALUES ('a', '1')")
        with connection.transaction(conn):                 # joins the outer one
            conn.execute("INSERT INTO app_settings VALUES ('b', '2')")
    with pytest.raises(RuntimeError), connection.transaction(conn, immediate=True):
        conn.execute("INSERT INTO app_settings VALUES ('c', '3')")
        raise RuntimeError('boom')
    assert [r[0] for r in conn.execute('SELECT key FROM app_settings ORDER BY key')] == ['a', 'b']


def test_secure_files_sets_owner_only_modes(tmp_path: Path) -> None:
    db = tmp_path / 'x.db'
    for f in (db, tmp_path / 'x.db-wal', tmp_path / 'x.db-shm'):
        f.write_text('')
        f.chmod(0o644)
    connection.secure_files(db)
    assert [_mode(f) for f in sorted(tmp_path.iterdir())] == [0o600, 0o600, 0o600]
    connection.secure_files(tmp_path / 'missing.db')               # nothing to do, no error


def test_backup_to_copies_the_live_database(conn: sqlite3.Connection, tmp_path: Path) -> None:
    conn.execute("INSERT INTO app_settings VALUES ('k', 'v')")
    conn.commit()
    target = migrate.backup_to(conn, tmp_path / 'deep' / 'copy.db')
    assert _mode(target) == 0o600 and _mode(target.parent) == 0o700
    copy = sqlite3.connect(str(target))
    assert copy.execute("SELECT value FROM app_settings WHERE key = 'k'").fetchone()[0] == 'v'
    copy.close()


def test_applied_does_not_commit_the_callers_transaction(conn: sqlite3.Connection) -> None:
    conn.execute('BEGIN')
    conn.execute("INSERT INTO app_settings VALUES ('pending', '1')")
    assert migrate.applied(conn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    conn.rollback()
    assert conn.execute("SELECT 1 FROM app_settings WHERE key = 'pending'").fetchone() is None


def test_0004_back_fills_the_per_upload_values(db_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hbudget.core.models import BankTxn
    from hbudget.db import repo
    every = migrate.available()
    monkeypatch.setattr(migrate, 'available', lambda: every[:3])         # a database from before 0004
    c = connection.open(db_file)
    migrate.migrate(c, db_file, now=NOW)
    c.execute("INSERT INTO households (name, as_of, created_at, updated_at) VALUES ('H', '2029-01-01', 'x', 'x')")
    c.execute("INSERT INTO uploads (household_id, id, kind, uploaded_at, status) VALUES (1, 'u1', 'bank', 'x', "
              "'waiting')")
    c.execute("INSERT INTO bank_txns (household_id, id, first_upload_id, posted, amount, balance, description, "
              "category) VALUES (1, 't1', 'u1', '2029-01-02', -5000, 95000, 'SHOP', 'Shopping')")
    c.execute("INSERT INTO upload_txns (household_id, upload_id, txn_id, file_index) VALUES (1, 'u1', 't1', 0)")
    c.commit()
    monkeypatch.setattr(migrate, 'available', lambda: every)
    assert migrate.migrate(c, db_file, now=NOW) == [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert repo.upload_txns(c, 1, 'u1') == [BankTxn(id='t1', posted=date(2029, 1, 2), amount=-5000,
                                                    description='SHOP', balance=95000, category='Shopping')]
    c.close()


def test_0009_gives_open_series_rows_the_series_card(db_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from factories import D, budget, row, series
    from hbudget.core.models import Cadence, RowState, Schedule
    from hbudget.db import repo
    every = migrate.available()
    monkeypatch.setattr(migrate, 'available', lambda: every[:8])         # a database from before 0009
    c = connection.open(db_file)
    migrate.migrate(c, db_file, now=NOW)
    hid = repo.create_household(c, 'H', as_of=D(2029, 3, 1))
    s = series('grocery', 'spending', 20000, Schedule(Cadence('weekly'), D(2029, 3, 1)), card='bluecard')
    rows = (row('a', 'Grocery', 25000, D(2029, 3, 8), kind='spending', series_id='grocery', overridden=True),
            row('b', 'Grocery', 20000, D(2029, 3, 15), kind='spending', series_id='grocery', card='bluecard'),
            row('c', 'Grocery', 20000, D(2029, 3, 1), kind='spending', series_id='grocery', state=RowState.CLEARED),
            row('d', 'Gift', 5000, D(2029, 3, 9)))
    keys, cols = repo._TABLES['series']                                  # written as the code before 0011 did
    monkeypatch.setitem(repo._TABLES, 'series', (keys, tuple(x for x in cols if x != 'debt')))
    repo.save_budget(c, hid, replace(budget(series=(s,), rows=rows)), user_id=None, action='test')
    monkeypatch.setitem(repo._TABLES, 'series', (keys, cols))
    c.commit()
    monkeypatch.setattr(migrate, 'available', lambda: every)
    assert migrate.migrate(c, db_file, now=NOW) == [9, 10, 11, 12, 13, 14]
    cards = {r.id: r.card for r in repo.load_budget(c, hid).rows}
    assert cards == {'a': 'bluecard', 'b': 'bluecard', 'c': None, 'd': None}
    c.close()


def test_a_database_from_a_newer_release_is_refused_untouched(db_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rollback that starts an older release on migrated data must not run (docs/STABILITY.md)."""
    c = connection.open(db_file)
    migrate.migrate(c, db_file, now=NOW)
    c.execute("INSERT INTO households (name, currency, as_of, created_at, updated_at) "
              "VALUES ('H', 'USD', '2029-01-01', 'x', 'x')")
    c.commit()
    every = migrate.available()
    monkeypatch.setattr(migrate, 'available', lambda: every[:-2])          # the older release knows two fewer
    with pytest.raises(migrate.NewerDatabase) as e:
        migrate.migrate(c, db_file, now=NOW)
    assert 'newer version of the app (it has migration %04d; this version knows up to %04d)' % (
        every[-1].version, every[-3].version) in str(e.value)
    assert 'hbudget-before-' in str(e.value)
    assert migrate.applied(c) == [m.version for m in every]                # nothing was changed
    assert not (db_file.parent / 'backups').exists()
    c.close()


def test_the_app_refuses_to_start_on_a_newer_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The uvicorn --factory path (`make dev`) stops too, not only `hbudget serve`."""
    from hbudget.web.app import create_app
    from hbudget.web.config import Settings
    c = connection.open(tmp_path / 'hbudget.db')
    migrate.migrate(c, tmp_path / 'hbudget.db', now=NOW)
    c.close()
    every = migrate.available()
    monkeypatch.setattr(migrate, 'available', lambda: every[:-1])
    with pytest.raises(migrate.NewerDatabase, match='newer version of the app'):
        create_app(Settings(data_dir=tmp_path))
