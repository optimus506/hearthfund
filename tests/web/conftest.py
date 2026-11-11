"""Fixtures for the web tests: a fresh data dir per test, an app, and a signed-in admin (helpers: webhelpers.py)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hbudget.db import connection, repo, users
from hbudget.web.app import create_app
from webhelpers import TODAY, Client, make_settings, page_csrf, set_start, setup_admin


@pytest.fixture
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    a = create_app(make_settings(tmp_path))
    a.state.clock = lambda: TODAY
    yield a
    a.state.stop.set()


@pytest.fixture
def admin(app, tmp_path: Path) -> Iterator[Client]:  # type: ignore[no-untyped-def]
    with Client(app, base_url='http://testserver') as c:
        setup_admin(c, tmp_path)
        conn = connection.connect(tmp_path / 'hbudget.db')
        c.hid = repo.households_for_user(conn, users.get_user_by_name(conn, 'admin').id)[0][0].id  # type: ignore[union-attr]
        set_start(conn, c.hid)
        conn.close()
        c.csrf = page_csrf(c, '/h/%d/bills' % c.hid)
        yield c
