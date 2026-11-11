"""Numbered SQL migrations (ARCHITECTURE 14.1): forward-only, one transaction each, backup before upgrading data."""
from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from .connection import private_umask, secure_files

__all__ = ['Migration', 'NewerDatabase', 'applied', 'available', 'backup_to', 'check_not_newer', 'current_version',
           'migrate']

_NAME = re.compile(r'^(\d{4})_([a-z0-9_]+)\.sql$')


class NewerDatabase(RuntimeError):
    """The database was migrated by a newer release than the one running (a rollback without the old data)."""


class Migration:
    __slots__ = ('name', 'sql', 'version')

    def __init__(self, version: int, name: str, sql: str) -> None:
        self.version, self.name, self.sql = version, name, sql

    def __repr__(self) -> str:
        return 'Migration(%d, %r)' % (self.version, self.name)


def available() -> list[Migration]:
    """Every migration shipped in hbudget/db/migrations, in version order."""
    out = []
    for entry in resources.files('hbudget.db').joinpath('migrations').iterdir():
        m = _NAME.match(entry.name)
        if m:
            out.append(Migration(int(m.group(1)), m.group(2), entry.read_text('utf-8')))
    out.sort(key=lambda x: x.version)
    if [m.version for m in out] != list(range(1, len(out) + 1)):
        raise RuntimeError('migrations must be numbered 0001, 0002, ... without gaps')
    return out


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations '
                 '(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)')


def applied(conn: sqlite3.Connection) -> list[int]:
    _ensure_table(conn)
    return [r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version')]


def current_version(conn: sqlite3.Connection) -> int:
    done = applied(conn)
    return done[-1] if done else 0


def _has_user_data(conn: sqlite3.Connection) -> bool:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name != 'schema_migrations'")]
    for t in tables:
        quoted = '"%s"' % t.replace('"', '""')     # a table name from sqlite_master, never user input
        if conn.execute('SELECT 1 FROM %s LIMIT 1' % quoted).fetchone():  # noqa: S608
            return True
    return False


def backup_to(conn: sqlite3.Connection, target: Path) -> Path:
    """Copy the live database (SQLite backup API) to `target`, owner-readable only."""
    with private_umask():
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = sqlite3.connect(str(target))
        try:
            conn.backup(dest)
        finally:
            dest.close()
    secure_files(target)
    return target


def check_not_newer(conn: sqlite3.Connection) -> None:
    """Refuse a database that a newer release has migrated: this code does not know its tables, and writing to
    them could lose data. Raises NewerDatabase with what to do."""
    known = {m.version for m in available()}
    newer = sorted(v for v in applied(conn) if v not in known)
    if newer:
        raise NewerDatabase(
            'This database was last used by a newer version of the app (it has migration %04d; this version knows '
            'up to %04d), so this version will not start on it. Start the newer version again, or go back to the '
            'copy made before the upgrade: data/backups/hbudget-before-*.db (docs/DEPLOY.md, "Upgrading").'
            % (newer[-1], max(known, default=0)))


def migrate(conn: sqlite3.Connection, db_path: str | Path | None, *, now: datetime | None = None) -> list[int]:
    """Apply pending migrations in order; returns the versions applied.

    When the database already holds data, a copy is written to <data dir>/backups/hbudget-before-<version>-<time>.db
    first. Each migration and its schema_migrations row commit together, or not at all.
    """
    check_not_newer(conn)
    done = set(applied(conn))
    pending = [m for m in available() if m.version not in done]
    if not pending:
        return []
    stamp = (now or datetime.now(UTC)).strftime('%Y%m%dT%H%M%SZ')
    if db_path is not None and str(db_path) != ':memory:' and _has_user_data(conn):
        backup_to(conn, Path(db_path).parent / 'backups' / ('hbudget-before-%04d-%s.db' % (pending[0].version, stamp)))
    out = []
    for m in pending:
        at = (now or datetime.now(UTC)).isoformat(timespec='seconds')
        # the bookkeeping row holds only our own file name and a timestamp, quoted by _sql_text
        bookkeeping = 'INSERT INTO schema_migrations (version, name, applied_at) VALUES (%d, %s, %s);' % (  # noqa: S608
            m.version, _sql_text(m.name), _sql_text(at))
        script = 'BEGIN;\n%s\n%s\nCOMMIT;' % (m.sql, bookkeeping)
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise
        out.append(m.version)
    if db_path is not None and str(db_path) != ':memory:':
        secure_files(db_path)
    return out


def _sql_text(value: str) -> str:
    """A quoted SQL string literal for the migration bookkeeping row (names come from our own files)."""
    return "'%s'" % value.replace("'", "''")
