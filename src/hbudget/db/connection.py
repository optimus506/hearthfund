"""SQLite connections: pragmas, private file permissions, and nestable transactions (ARCHITECTURE 14)."""
from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

__all__ = ['DB_NAME', 'connect', 'db_path', 'open', 'private_umask', 'secure_files', 'transaction']

DB_NAME = 'hbudget.db'


def db_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / DB_NAME


@contextlib.contextmanager
def private_umask() -> Iterator[None]:
    """Files created inside are readable by the owner only (umask 077)."""
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def secure_files(path: str | Path) -> None:
    """Mode 600 for the database and its WAL / SHM files (when they exist)."""
    p = Path(path)
    for f in (p, p.with_name(p.name + '-wal'), p.with_name(p.name + '-shm')):
        if f.exists():
            f.chmod(0o600)


def connect(path: str | Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    """Open (creating if needed) the database with WAL, foreign keys and a busy timeout."""
    target = str(path)
    if target != ':memory:':
        Path(target).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with private_umask():
        conn = sqlite3.connect(target, check_same_thread=check_same_thread, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 5000')
        if target != ':memory:':
            conn.execute('PRAGMA journal_mode = WAL')
    if target != ':memory:':
        secure_files(target)
    return conn


open = connect


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """A transaction that commits on success and rolls back on error. Nested use joins the outer transaction.

    `immediate=True` takes the write lock at the start (BEGIN IMMEDIATE), for check-then-write sequences such as
    the first-run admin creation.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
