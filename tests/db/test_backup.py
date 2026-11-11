"""`hbudget backup`: online copy, integrity check, retention (db/backup.py)."""
from __future__ import annotations

import re
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hbudget import cli
from hbudget.db import backup, connection, migrate

T0 = datetime(2029, 11, 3, 2, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / 'hbudget.db'
    conn = connection.connect(path)
    migrate.migrate(conn, path)
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('backup-test', 'kept')")
    conn.commit()
    conn.close()
    return path


def value_in(path: Path) -> str:
    c = sqlite3.connect(path)
    try:
        return str(c.execute("SELECT value FROM app_settings WHERE key = 'backup-test'").fetchone()[0])
    finally:
        c.close()


def test_backup_copies_checks_and_is_private(db, tmp_path):
    res = backup.run(db, tmp_path / 'backups', now=T0)
    assert res.path.name == 'hbudget-20291103T020000Z.db' and res.size > 0 and res.removed == []
    assert value_in(res.path) == 'kept'
    assert stat.S_IMODE(res.path.stat().st_mode) == 0o600
    assert [p.name for p in res.path.parent.iterdir()] == [res.path.name]   # no partial file left behind


def test_backup_while_another_connection_writes(db, tmp_path):
    live = connection.connect(db)     # WAL mode, as the running app has it
    live.execute("UPDATE app_settings SET value = 'committed' WHERE key = 'backup-test'")
    live.commit()
    live.execute('BEGIN IMMEDIATE')   # a write in progress is not in the copy
    live.execute("UPDATE app_settings SET value = 'uncommitted' WHERE key = 'backup-test'")
    try:
        res = backup.run(db, tmp_path / 'backups', now=T0)
    finally:
        live.rollback()
        live.close()
    assert value_in(res.path) == 'committed'


def test_retention_keeps_the_newest_of_each_kind(db, tmp_path):
    folder = tmp_path / 'backups'
    folder.mkdir()
    for v, day in ((2, 1), (3, 2), (4, 3)):
        (folder / ('hbudget-before-%04d-202910%02dT000000Z.db' % (v, day))).write_bytes(b'x')
    (folder / 'notes.txt').write_text('mine')
    names = []
    for i in range(4):
        names.append(backup.run(db, folder, keep=2, keep_upgrade=1, now=T0 + timedelta(days=i)).path.name)
    left = sorted(p.name for p in folder.iterdir())
    assert left == sorted([*names[-2:], 'hbudget-before-0004-20291003T000000Z.db', 'notes.txt'])


def test_bad_copy_is_not_kept(db, tmp_path, monkeypatch):
    def broken(conn, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'not a database' * 100)
        return target
    monkeypatch.setattr(backup, 'backup_to', broken)
    folder = tmp_path / 'backups'
    with pytest.raises(backup.BackupError, match='integrity'):
        backup.run(db, folder, now=T0)
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize(('kw', 'msg'), [({'keep': 0}, '--keep'), ({'keep_upgrade': -1}, '--keep')])
def test_bad_arguments(db, tmp_path, kw, msg):
    with pytest.raises(backup.BackupError, match=msg):
        backup.run(db, tmp_path / 'b', **kw)


def test_missing_database(tmp_path):
    with pytest.raises(backup.BackupError, match='no database'):
        backup.run(tmp_path / 'nope.db', tmp_path / 'b')


def test_cli(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HBUDGET_DATA_DIR', str(tmp_path))
    assert cli.main(['backup', '--keep', '3']) == 0
    out = capsys.readouterr().out
    assert 'integrity ok' in out and str(tmp_path / 'backups') in out
    # scripts/upgrade.sh reads the file name from this line: keep its shape
    assert re.search(r'^backup: /\S*/backups/hbudget-[0-9TZ]+\.db \(\d+ bytes, integrity ok\)$', out, re.M), out
    monkeypatch.setenv('HBUDGET_DATA_DIR', str(tmp_path / 'empty'))
    assert cli.main(['backup']) == 2
    assert capsys.readouterr().err.startswith('error: no database')
