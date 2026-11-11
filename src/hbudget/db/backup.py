"""Scheduled backups: `hbudget backup` (docs/DEPLOY.md, "Backups").

A backup is a copy of the live database made with SQLite's online backup API, so the app can keep running.
It is written under a temporary name, checked with `PRAGMA integrity_check` and only then renamed to
`hbudget-<UTC time>.db`, so a failed or interrupted run never leaves a file that looks like a good backup.
Retention keeps the newest scheduled backups and, separately, the newest pre-upgrade copies that
`migrate.migrate` writes (`hbudget-before-<version>-<time>.db`). Nothing else in the folder is touched.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from hbudget.db.migrate import backup_to

SCHEDULED = re.compile(r'^hbudget-\d{8}T\d{6}Z\.db$')
BEFORE_UPGRADE = re.compile(r'^hbudget-before-\d{4}-\d{8}T\d{6}Z\.db$')


class BackupError(RuntimeError):
    """The backup could not be made or did not pass the integrity check."""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size: int
    removed: list[Path] = field(default_factory=list)


def _newest_first(folder: Path, pattern: re.Pattern[str]) -> list[Path]:
    # the UTC timestamp in the name sorts in time order; the name is a tie-breaker that never changes
    return sorted((p for p in folder.iterdir() if p.is_file() and pattern.match(p.name)),
                  key=lambda p: p.name[-20:] + p.name, reverse=True)


def prune(folder: Path, *, keep: int, keep_upgrade: int) -> list[Path]:
    """Delete scheduled backups beyond the newest `keep` and pre-upgrade copies beyond the newest `keep_upgrade`."""
    removed = []
    for pattern, n in ((SCHEDULED, keep), (BEFORE_UPGRADE, keep_upgrade)):
        for old in _newest_first(folder, pattern)[n:]:
            old.unlink()
            removed.append(old)
    return removed


def run(db_path: Path, folder: Path, *, keep: int = 14, keep_upgrade: int = 5,
        now: datetime | None = None) -> BackupResult:
    """Back up `db_path` into `folder`, verify the copy, then apply retention. Raises BackupError."""
    if keep < 1 or keep_upgrade < 0:
        raise BackupError('--keep must be at least 1 and --keep-upgrade at least 0')
    if not db_path.is_file():
        raise BackupError('no database at %s' % db_path)
    stamp = (now or datetime.now(UTC)).strftime('%Y%m%dT%H%M%SZ')
    final = folder / ('hbudget-%s.db' % stamp)
    if final.exists():
        raise BackupError('%s already exists' % final.name)
    partial = folder / ('.hbudget-%s.db.partial' % stamp)
    src = sqlite3.connect('file:%s?mode=ro' % db_path.resolve().as_posix(), uri=True)
    try:
        backup_to(src, partial)
    except sqlite3.Error as e:
        partial.unlink(missing_ok=True)
        raise BackupError('could not copy the database: %s' % e) from None
    finally:
        src.close()
    try:
        check = sqlite3.connect(partial)
        try:
            # one self-contained file (no -wal/-shm beside it), easy to copy elsewhere
            check.execute('PRAGMA journal_mode=DELETE')
            verdict = [r[0] for r in check.execute('PRAGMA integrity_check')]
        finally:
            check.close()
    except sqlite3.Error as e:
        verdict = [str(e)]
    if verdict != ['ok']:
        for f in (partial, partial.with_name(partial.name + '-wal'), partial.with_name(partial.name + '-shm')):
            f.unlink(missing_ok=True)
        raise BackupError('the copy failed the integrity check: %s' % '; '.join(verdict[:3]))
    partial.rename(final)
    return BackupResult(final, final.stat().st_size, prune(folder, keep=keep, keep_upgrade=keep_upgrade))
