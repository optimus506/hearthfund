"""The offline page and the service worker (D-089): no sign-in needed, and the worker caches no budget data."""
from __future__ import annotations

from test_api_v1 import anon  # noqa: F401 - a fixture


def test_offline_page_and_service_worker(anon):  # noqa: F811 - the anon fixture
    page = anon.get('/offline')
    assert page.status_code == 200 and "You're offline" in page.text and '/css/app.css' in page.text
    sw = anon.get('/sw.js')
    assert sw.status_code == 200 and sw.headers['content-type'].startswith('text/javascript')
    assert sw.headers['service-worker-allowed'] == '/' and sw.headers['cache-control'] == 'no-cache'
    body = sw.text
    tag = body.split("const CACHE = 'hbudget-", 1)[1].split("'", 1)[0]
    assert "'/offline'" in body and '/static/%s/css/app.css' % tag in body
    assert anon.get('/static/%s/css/app.css' % tag).status_code == 200     # what it caches exists
    assert 'cache.put' not in body and '.put(' not in body                 # it never stores a response it fetched
