"""Helpers for the web tests (tests/web): a test client, settings, the first-run setup through the real /setup form.

Imported by name (`from webhelpers import ...`); tests/web/conftest.py holds only the fixtures. A module name of
its own avoids the clash between the several conftest.py files when test folders are run in another order."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from hbudget.db import connection, repo, users
from hbudget.web.config import Settings

TODAY = date(2029, 3, 1)
PASSWORD = 'correct horse battery'


def make_settings(tmp: Path, **kw: object) -> Settings:
    base: dict[str, object] = {'data_dir': tmp, 'allowed_hosts': ('testserver', '127.0.0.1', 'localhost'),
                               'cookie_secure': 'false', 'time_limit_seconds': 5.0}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, 'no csrf token in page'
    return m.group(1)


def page_csrf(client: TestClient, path: str) -> str:
    """The session CSRF token as a page ships it in its page data."""
    r = client.get(path)
    assert r.status_code == 200, r.text[:200]
    m = re.search(r'"csrf":\s*"([^"]+)"', r.text)
    assert m, 'no csrf in page data'
    return m.group(1)


class Client(TestClient):
    csrf: str = ''
    hid: int = 0

    def api(self, method: str, path: str, json: object = None, **kw: object) -> object:
        headers = {'X-CSRF-Token': self.csrf, 'Origin': 'http://testserver'}
        return self.request(method, '/api/h/%d%s' % (self.hid, path), json=json, headers=headers, **kw)


def setup_admin(client: TestClient, tmp: Path, username: str = 'admin', household: str = 'Test household') -> None:
    r = client.get('/setup')
    assert r.status_code == 200
    token = (tmp / 'setup-token').read_text().strip()
    r = client.post('/setup', data={'csrf_token': csrf_from(r.text), 'token': token, 'username': username,
                                    'password': PASSWORD, 'household': household, 'start': 'empty'},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:300]


def set_start(conn, hid: int, balance: int = 100000) -> None:  # type: ignore[no-untyped-def]
    """Fake households start on the test's fixed date, not on the real day they were created."""
    from dataclasses import replace
    b = repo.load_budget(conn, hid)
    repo.save_budget(conn, hid, replace(b, as_of=TODAY, start_balance=balance), user_id=None, action='test.setup')


def add_user(tmp: Path, hid: int, username: str, role: str | None) -> None:
    conn = connection.connect(tmp / 'hbudget.db')
    try:
        uid = users.create_user(conn, username, PASSWORD)
        if role:
            repo.add_member(conn, hid, uid, role)
    finally:
        conn.close()


def login(app, username: str, password: str = PASSWORD) -> Client:  # type: ignore[no-untyped-def]
    c = Client(app, base_url='http://testserver')
    r = c.get('/login')
    r = c.post('/login', data={'csrf_token': csrf_from(r.text), 'username': username, 'password': password},
               follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return c
