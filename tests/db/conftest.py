from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from hbudget.db import connection, migrate


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    return tmp_path / 'data' / 'hbudget.db'


@pytest.fixture
def conn(db_file: Path) -> Iterator[sqlite3.Connection]:
    c = connection.open(db_file)
    migrate.migrate(c, db_file)
    yield c
    c.close()
