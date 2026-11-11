"""The only code that locates the private regression files (ARCHITECTURE 20.7).

Private tests call private_file(); when the files are not there the test is skipped with a reason that names the
variable or the file pattern, never a value. report() formats a failure without showing values unless
PRIVATE_SHOW=1 is set.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ENV = 'BUDGET_PRIVATE_DIR'
PREFIX = 'private regression test did not run: '


def private_dir() -> Path:
    raw = os.environ.get(ENV, '').strip()
    if not raw:
        pytest.skip(PREFIX + '%s is not set' % ENV)
    path = Path(os.path.expanduser(raw))
    if not path.is_dir():
        pytest.skip(PREFIX + '%s is not a folder' % ENV)
    return path


def private_file(name: str) -> Path:
    """A file in BUDGET_PRIVATE_DIR. `name` may be a glob pattern such as "*.ods"; it must match exactly one file."""
    folder = private_dir()
    if any(c in name for c in '*?['):
        hits = sorted(p for p in folder.glob(name) if p.is_file())
        if not hits:
            pytest.skip(PREFIX + 'no %s file in %s' % (name, ENV))
        if len(hits) > 1:
            pytest.skip(PREFIX + 'more than one %s file in %s; name it in private_tests/files.yaml' % (name, ENV))
        return hits[0]
    path = folder / name
    if not path.is_file():
        pytest.skip(PREFIX + 'a configured file is missing from %s' % ENV)
    return path


def report(field: str, ok: bool, *, got: object = None, want: object = None) -> str:
    """'' when ok, else "<field>: mismatch" (plus the values only when PRIVATE_SHOW=1)."""
    if ok:
        return ''
    if os.environ.get('PRIVATE_SHOW') == '1':
        return '%s: mismatch (got %r, want %r)' % (field, got, want)
    return '%s: mismatch' % field


def check(field: str, got: object, want: object) -> None:
    """Assert got == want; the failure message never shows the values (unless PRIVATE_SHOW=1)."""
    message = report(field, got == want, got=got, want=want)
    if message:
        raise AssertionError(message) from None
