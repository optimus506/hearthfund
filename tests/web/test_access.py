"""Deny-by-default CSRF on every state-changing route, the role matrix on every household route, and IDOR."""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from hbudget.db import connection, repo
from webhelpers import Client, add_user, login, page_csrf

UNSAFE = {'POST', 'PUT', 'PATCH', 'DELETE'}

# minimum role per household API route; the test fails when a route is added without an entry here
ROLES = {
    ('GET', '/summary'): 'viewer', ('GET', '/home'): 'viewer', ('GET', '/ledger'): 'viewer',
    ('POST', '/rows/{row_id}/status'): 'contributor',
    ('POST', '/rows/{row_id}/note'): 'contributor', ('POST', '/rows/{row_id}/change'): 'editor',
    ('POST', '/rows/{row_id}/remove'): 'editor', ('POST', '/rows/{row_id}/done'): 'editor',
    ('POST', '/rows/{row_id}/undo'): 'editor',
    ('POST', '/rows'): 'editor', ('POST', '/rows/{row_id}/move'): 'editor', ('POST', '/whatif'): 'viewer',
    ('POST', '/rows/bulk'): 'contributor', ('GET', '/checkin'): 'viewer', ('POST', '/checkin'): 'contributor',
    ('GET', '/goals'): 'viewer', ('PUT', '/goals'): 'editor', ('GET', '/milestones'): 'viewer',
    ('POST', '/milestones/seen'): 'contributor', ('PUT', '/celebrate'): 'editor',
    ('GET', '/expected'): 'viewer', ('POST', '/expected'): 'editor', ('PUT', '/expected/{item_id}'): 'editor',
    ('DELETE', '/expected/{item_id}'): 'editor', ('POST', '/expected/{item_id}/status'): 'contributor',
    ('GET', '/series'): 'viewer', ('POST', '/series'): 'editor', ('PUT', '/series/{series_id}'): 'editor',
    ('DELETE', '/series/{series_id}'): 'editor', ('POST', '/series/{series_id}/status'): 'contributor',
    ('POST', '/series/{series_id}/seasonal'): 'editor', ('POST', '/generate'): 'editor',
    ('GET', '/accounts'): 'viewer', ('POST', '/accounts'): 'editor', ('PUT', '/accounts/{account_id}'): 'editor',
    ('DELETE', '/accounts/{account_id}'): 'editor', ('POST', '/payoff'): 'viewer',
    ('GET', '/flow'): 'viewer', ('GET', '/checkup'): 'viewer', ('GET', '/card'): 'viewer',
    ('PUT', '/card/categories'): 'editor',
    ('GET', '/recurring'): 'viewer', ('GET', '/calendar'): 'viewer', ('GET', '/recap'): 'viewer',
    ('PUT', '/recurring/hidden'): 'editor', ('PUT', '/alerts/snooze'): 'contributor',
    ('GET', '/card/stores'): 'viewer', ('GET', '/card/purchases'): 'viewer', ('PUT', '/card/stores'): 'editor',
    ('PUT', '/card/purchases/{txn_id}'): 'editor',
    ('GET', '/rules'): 'viewer', ('POST', '/rules'): 'editor', ('PUT', '/rules/{rule_id}'): 'editor',
    ('DELETE', '/rules/{rule_id}'): 'editor', ('POST', '/rules/order'): 'editor', ('POST', '/rules/test'): 'editor',
    ('GET', '/rules/starter'): 'viewer', ('POST', '/rules/pack'): 'editor',
    ('PUT', '/settings'): 'editor', ('PUT', '/notes'): 'editor', ('PUT', '/caps'): 'editor',
    ('PUT', '/flow-groups'): 'editor',
    ('POST', '/import/billsystem'): 'owner', ('POST', '/import/workbook/preview'): 'owner',
    ('POST', '/import/workbook/commit'): 'owner',
    ('GET', '/export/backup.yaml'): 'editor', ('GET', '/export/backup.json'): 'editor',
    ('GET', '/export/forecast.csv'): 'viewer', ('GET', '/export/forecast.xlsx'): 'viewer',
    ('POST', '/wipe'): 'owner',
    ('GET', '/members'): 'owner', ('POST', '/members'): 'owner', ('PUT', '/members/{user_id}'): 'owner',
    ('DELETE', '/members/{user_id}'): 'owner', ('GET', '/audit'): 'owner',
    ('GET', '/profiles'): 'editor', ('DELETE', '/profiles/{profile_id}'): 'editor',
    ('GET', '/uploads'): 'viewer', ('POST', '/uploads'): 'editor', ('POST', '/uploads/{upload_id}/mapping'): 'editor',
    ('DELETE', '/uploads/{upload_id}'): 'editor',
    ('GET', '/uploads/{upload_id}/removal'): 'editor', ('POST', '/reconcile/preview'): 'editor',
    ('POST', '/reconcile/apply'): 'editor', ('GET', '/reconciliations'): 'viewer',
    ('GET', '/reconciliations/{rec_id}'): 'viewer', ('POST', '/reconciliations/{rec_id}/undo'): 'editor',
    ('GET', '/tokens'): 'owner', ('POST', '/tokens'): 'owner', ('DELETE', '/tokens/{token_id}'): 'owner',
}
ORDER = ['viewer', 'contributor', 'editor', 'owner']
PAGES = {'import': 'owner', 'members': 'owner', 'access': 'owner'}


def api_routes(app):  # type: ignore[no-untyped-def]
    """Every APIRoute, also inside included routers (newer FastAPI wraps them)."""
    stack = list(app.routes)
    while stack:
        r = stack.pop(0)
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, 'original_router'):
            stack.extend(r.original_router.routes)


def household_routes(app):  # type: ignore[no-untyped-def]
    prefix = '/api/h/{hid}'
    for r in api_routes(app):
        if r.path.startswith(prefix):
            for method in r.methods:
                yield method, r.path[len(prefix):]


def fill(path: str) -> str:
    return re.sub(r'\{(\w+)\}', lambda m: 'x1' if m.group(1) != 'user_id' else '999', path)


def test_every_household_route_has_a_role(app):
    routes = set(household_routes(app))
    missing = routes - set(ROLES)
    assert not missing, 'add these routes to ROLES: %s' % sorted(missing)
    assert not set(ROLES) - routes, 'stale entries: %s' % sorted(set(ROLES) - routes)


def test_every_state_changing_route_needs_the_csrf_token(admin, app):
    paths = [(m, p) for m, p in household_routes(app) if m in UNSAFE]
    for r in api_routes(app):
        if not r.path.startswith('/api/h/'):
            paths += [(m, r.path) for m in r.methods if m in UNSAFE]
    assert len(paths) > 40
    for method, path in paths:
        url = fill(path) if path.startswith(('/login', '/setup', '/logout', '/account', '/api/admin')) else (
            '/api/h/%d' % admin.hid + fill(path))
        r = admin.request(method, url, json={})
        assert r.status_code == 403, (method, url, r.status_code)
        assert r.json()['error'] == 'csrf'
        # a wrong token or a foreign origin is refused too
        r = admin.request(method, url, json={}, headers={'X-CSRF-Token': 'nope'})
        assert r.status_code == 403, (method, url)
        r = admin.request(method, url, json={}, headers={'X-CSRF-Token': admin.csrf, 'Origin': 'http://evil.example'})
        assert r.status_code == 403, (method, url)


def test_multipart_is_refused_before_the_body_is_read(admin):
    big = b'x' * (2 * 1024 * 1024)
    r = admin.post('/api/h/%d/uploads' % admin.hid, files={'file': ('a.csv', big, 'text/csv')})
    assert r.status_code == 403 and r.json()['error'] == 'csrf'


def test_null_origin_is_refused(admin):
    r = admin.request('PUT', '/api/h/%d/notes' % admin.hid, json={'notes': 'x'},
                      headers={'X-CSRF-Token': admin.csrf, 'Origin': 'null'})
    assert r.status_code == 403


@pytest.fixture
def people(admin, app, tmp_path):  # type: ignore[no-untyped-def]
    out = {'owner': admin}
    for role in ('editor', 'contributor', 'viewer'):
        add_user(tmp_path, admin.hid, role + '1', role)
        c = login(app, role + '1')
        c.hid = admin.hid
        c.csrf = page_csrf(c, '/h/%d/bills' % admin.hid)
        out[role] = c
    add_user(tmp_path, admin.hid, 'stranger', None)
    s = login(app, 'stranger')
    s.hid = admin.hid
    s.csrf = page_csrf(s, '/account')
    out['stranger'] = s
    return out


def test_role_matrix(people):
    for (method, path), need in sorted(ROLES.items()):
        for role in ORDER:
            r = people[role].api(method, fill(path), json={})
            if ORDER.index(role) < ORDER.index(need):
                assert r.status_code == 403, (method, path, role, r.status_code, r.text[:200])
            else:
                assert r.status_code not in (401, 403), (method, path, role, r.status_code, r.text[:200])
        r = people['stranger'].api(method, fill(path), json={})
        assert r.status_code == 404, (method, path, 'stranger', r.status_code)


def test_pages_by_role(people, admin):
    from hbudget.web.routes.pages import TABS
    for tab, _ in TABS:
        for role in ORDER:
            r = people[role].get('/h/%d/%s' % (admin.hid, tab))
            need = PAGES.get(tab, 'viewer')
            want = 200 if ORDER.index(role) >= ORDER.index(need) else 403
            assert r.status_code == want, (tab, role, r.status_code)
        assert people['stranger'].get('/h/%d/%s' % (admin.hid, tab)).status_code == 404
    assert people['viewer'].get('/admin').status_code == 404
    assert people['owner'].get('/admin').status_code == 200


def test_contributor_can_mark_but_not_edit(people, admin):
    o = people['owner']
    sid = o.api('POST', '/series', {'name': 'Electric', 'kind': 'bill', 'amount': 15000,
                                    'schedule': {'cadence': 'monthly', 'start': '2029-03-01', 'day': 6}}).json()['id']
    rows = o.api('GET', '/ledger').json()['rows']
    rid = rows[0]['id']
    c = people['contributor']
    assert c.api('POST', '/rows/%s/status' % rid, {'status': 'manual'}).status_code == 200
    assert c.api('POST', '/series/%s/status' % sid, {'status': 'autopay'}).status_code == 200
    assert c.api('POST', '/rows/%s/change' % rid, {'amount': 1}).status_code == 403
    eid = o.api('POST', '/expected', {'label': 'Refund', 'amount': 2000, 'direction': 'in',
                                      'date': '2029-03-08'}).json()['id']
    assert c.api('POST', '/expected/%s/status' % eid, {'status': 'done'}).status_code == 200
    assert c.api('POST', '/expected/%s/status' % eid, {'status': 'done', 'amount': 1}).status_code == 400
    assert c.api('PUT', '/expected/%s' % eid, {'label': 'X', 'amount': 1, 'direction': 'in',
                                                'date': '2029-03-08'}).status_code == 403
    assert people['viewer'].api('POST', '/rows/%s/status' % rid, {'status': 'none'}).status_code == 403


def test_other_households_ids_are_not_found(admin, app, tmp_path):
    a = admin
    sid = a.api('POST', '/series', {'name': 'Electric', 'kind': 'bill', 'amount': 15000,
                                    'schedule': {'cadence': 'monthly', 'start': '2029-03-01', 'day': 6}}).json()['id']
    rid = a.api('GET', '/ledger').json()['rows'][0]['id']
    aid = a.api('POST', '/accounts', {'name': 'Card A', 'kind': 'credit_card', 'balance': 100000}).json()['id']
    rule = a.api('POST', '/rules', {'name': 'Electric', 'kind': 'bill', 'contains': ['ELECTRIC'],
                                    'targets': [sid]}).json()['id']
    conn = connection.connect(tmp_path / 'hbudget.db')
    hid2 = repo.create_household(conn, 'Second')
    from hbudget.db import users
    repo.add_member(conn, hid2, users.get_user_by_name(conn, 'admin').id, 'owner')
    conn.close()
    b = Client(app)
    b.cookies = a.cookies
    b.csrf, b.hid = a.csrf, hid2
    for method, path, body in [('POST', '/rows/%s/status' % rid, {'status': 'manual'}),
                               ('POST', '/rows/%s/change' % rid, {'amount': 1}),
                               ('PUT', '/series/%s' % sid, {'name': 'X', 'kind': 'bill', 'amount': 1,
                                                            'schedule': {'cadence': 'once', 'start': '2029-03-01'}}),
                               ('DELETE', '/accounts/%s' % aid, None), ('DELETE', '/rules/%s' % rule, None),
                               ('POST', '/series/%s/seasonal' % sid, {'rules': [{'months': '1-12', 'amount': 1}]})]:
        r = b.api(method, path, body)
        assert r.status_code == 404, (method, path, r.status_code, r.text[:200])
    # and the first household is unchanged
    assert a.api('GET', '/ledger').json()['rows'][0]['amount'] == 15000
    # a household id that does not exist looks the same as one you are not in
    a2 = Client(app)
    a2.cookies, a2.csrf, a2.hid = a.cookies, a.csrf, 9999
    assert a2.api('GET', '/summary').status_code == 404


ADMIN_ROUTES = {('GET', '/overview'), ('GET', '/system'), ('POST', '/users'), ('POST', '/users/{user_id}/disabled'),
                ('POST', '/users/{user_id}/admin'), ('POST', '/users/{user_id}/password'),
                ('POST', '/users/{user_id}/2fa/reset'), ('POST', '/households'),
                ('POST', '/households/{hid}/owner')}
PUBLIC = {('GET', '/login'), ('POST', '/login'), ('GET', '/setup'), ('POST', '/setup'), ('POST', '/logout'),
          ('GET', '/healthz'), ('GET', '/'), ('GET', '/offline'), ('GET', '/sw.js'),
          ('GET', '/login/code'), ('POST', '/login/code')}      # the code step: no session yet (D-095)
SIGNED_IN = {('GET', '/logout'), ('GET', '/account'), ('POST', '/account/password'), ('GET', '/switch'),
             ('GET', '/admin'), ('GET', '/h/{hid}'), ('GET', '/account/2fa'), ('POST', '/account/2fa/start'),
             ('POST', '/account/2fa/cancel'), ('POST', '/account/2fa/confirm'), ('POST', '/account/2fa/codes'),
             ('POST', '/account/2fa/off')}


def test_every_route_is_in_a_known_table(app):
    """Guard: a new route must be added to ROLES, ADMIN_ROUTES, PUBLIC, SIGNED_IN or the page tabs."""
    from hbudget.web.routes.pages import TABS
    pages = {('GET', '/h/{hid}/' + t) for t, _ in TABS}
    known_hh = {('/api/h/{hid}' + p, m) for m, p in ROLES}
    unknown = []
    for r in api_routes(app):
        for m in r.methods:
            if m == 'HEAD':
                continue
            if r.path.startswith('/api/h/{hid}'):
                ok = (r.path, m) in known_hh
            elif r.path.startswith('/api/v1/') or r.path == '/mcp':
                ok = True     # access-token routes: tests/web/test_api_v1.py covers each one
            elif r.path.startswith('/api/admin'):
                ok = (m, r.path[len('/api/admin'):]) in ADMIN_ROUTES
            else:
                ok = (m, r.path) in PUBLIC | SIGNED_IN | pages
            if not ok:
                unknown.append((m, r.path))
    assert not unknown, unknown
    admin_found = {(m, r.path[len('/api/admin'):]) for r in api_routes(app) if r.path.startswith('/api/admin')
                   for m in r.methods}
    assert admin_found == ADMIN_ROUTES


def test_admin_api_is_for_admins_only(people, app):
    for method, path in sorted(ADMIN_ROUTES):
        url = '/api/admin' + fill(path).replace('x1', '1')
        for role in ('editor', 'contributor', 'viewer', 'stranger'):
            c = people[role]
            r = c.request(method, url, json={}, headers={'X-CSRF-Token': c.csrf})
            assert r.status_code == 404, (method, path, role, r.status_code)
        anon = Client(app)
        if method == 'GET':
            assert anon.get(url).status_code == 401, (method, path)
        else:
            assert anon.request(method, url, json={}).status_code == 403   # no CSRF token without a session
        # the admin (also the household owner) gets past the guard
        o = people['owner']
        r = o.request(method, url, json={}, headers={'X-CSRF-Token': o.csrf})
        assert r.status_code not in (401, 403), (method, path, r.status_code)
    # signed-in pages for an anonymous visitor redirect to /login
    anon = Client(app)
    for path in ('/account', '/account/2fa', '/admin', '/logout', '/h/1'):
        assert anon.get(path, follow_redirects=False).headers['location'] == '/login'
