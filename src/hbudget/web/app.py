"""The app factory (ARCHITECTURE section 15): start-up checks, middlewares, routes, error handling."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from hbudget import __version__
from hbudget.core.reconcile import ReconcileError
from hbudget.db import connection, migrate, repo, users
from hbudget.io import ImportProblem

from .config import Settings
from .deps import ApiError, NeedLogin, NeedTwoFactor, session_token
from .security import (
    SECURE_SESSION_COOKIE,
    SESSION_COOKIE,
    BodyLimitMiddleware,
    CsrfConfig,
    CsrfMiddleware,
    HashSlots,
    HeadersMiddleware,
)

log = logging.getLogger('hbudget')
HERE = Path(__file__).parent
SETUP_TOKEN_FILE = 'setup-token'  # noqa: S105 - a file name, not a secret
SETUP_HASH_KEY = 'setup_token_hash'


def _prepare_data_dir(settings: Settings) -> None:
    os.umask(0o077)   # the database, its WAL files, the secret key and backups: owner only
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = settings.data_dir / 'secret_key'
    if not settings.secret_key and not key.exists():
        key.write_text(secrets.token_urlsafe(48))
        key.chmod(0o600)


def secret_key(settings: Settings) -> str:
    """HBUDGET_SECRET_KEY, else the key file in the data folder (written on first start)."""
    return settings.secret_key or (settings.data_dir / 'secret_key').read_text().strip()


def _first_run(conn: sqlite3.Connection, settings: Settings) -> None:
    """No users yet: create the admin from HBUDGET_ADMIN_* if given, else write a one-time setup token (D-035)."""
    token_file = settings.data_dir / SETUP_TOKEN_FILE
    if users.user_count(conn):
        if settings.admin_username or settings.admin_password:
            log.warning('HBUDGET_ADMIN_USERNAME / HBUDGET_ADMIN_PASSWORD are ignored: users already exist. '
                        'Remove them from the environment.')
        token_file.unlink(missing_ok=True)
        repo.delete_setting(conn, SETUP_HASH_KEY)
        return
    if settings.admin_username and settings.admin_password:
        _, hid = users.create_first_admin(conn, settings.admin_username, settings.admin_password,
                                          'Demo household' if settings.demo else 'Household')
        if settings.demo:
            from hbudget import demo
            demo.install(conn, hid, today=date.today(), user_id=None)
        log.info('First run: created the admin account from the environment. Remove HBUDGET_ADMIN_PASSWORD now.')
        token_file.unlink(missing_ok=True)
        return
    token = secrets.token_urlsafe(32)
    token_file.write_text(token + '\n')
    token_file.chmod(0o600)
    repo.set_setting(conn, SETUP_HASH_KEY, hashlib.sha256(token.encode()).hexdigest())
    # the token itself is never logged
    log.warning('First run: open /setup and enter the setup token from %s', token_file)


def _start(settings: Settings) -> None:
    _prepare_data_dir(settings)
    conn = connection.connect(settings.db_path)
    try:
        applied = migrate.migrate(conn, settings.db_path)
        if applied:
            log.info('database migrated: %s', ', '.join(str(v) for v in applied))
        users.purge(conn)
        for h in repo.list_households(conn):      # postings stored twice before #87 was fixed; quick when none are
            bank, card = repo.merge_duplicate_txns(conn, h.id)
            if bank or card:
                log.info('household %d: merged %d bank and %d card postings stored twice', h.id, bank, card)
        _first_run(conn, settings)
    finally:
        conn.close()


def _housekeeping(settings: Settings, stop: threading.Event) -> None:
    while not stop.wait(24 * 3600):
        try:
            conn = connection.connect(settings.db_path)
            try:
                users.purge(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            log.exception('daily clean-up failed')


PLAID_FIRST_WAIT = 600       # seconds after start before the daily Plaid sync first looks
PLAID_LOOK_EVERY = 3600      # then it looks this often; each bank is synced at most once a day (plaid.AUTO_EVERY)


def _plaid_daily(app: FastAPI, stop: threading.Event) -> None:
    """The optional daily Plaid sync (docs/PLAID.md): stores what changed at the bank, for households that turned
    it on. Hourly looks so a restart never skips a day; plaid.auto_sync decides which banks are due."""
    from .routes import plaid

    wait = PLAID_FIRST_WAIT
    while not stop.wait(wait):
        wait = PLAID_LOOK_EVERY
        try:
            plaid.auto_sync(app)
        except Exception:
            log.exception('daily bank sync failed')


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith(('/api/', '/mcp')) or 'application/json' in request.headers.get('accept', '')


_HOUSEHOLD_PATH = re.compile(r'^/h/(\d{1,18})(?:/|$)')


def _home_link(request: Request) -> tuple[str, str]:
    """Where an error page sends the user back to: the household in the URL when the signed-in user is a member of
    it, else /. Nothing is said about households the user cannot see."""
    m = _HOUSEHOLD_PATH.match(request.url.path)
    token = session_token(request)
    if not m or not token:
        return '/', 'Back'
    settings: Settings = request.app.state.settings
    try:
        conn = connection.connect(settings.db_path)
        try:
            sess = users.get_session(conn, token, idle_hours=settings.session_hours)
            user = users.get_user(conn, sess.user_id) if sess else None
            hid = int(m.group(1))
            if user is None or user.disabled or repo.get_role(conn, user.id, hid) is None:
                return '/', 'Back'
            h = repo.get_household(conn, hid)
        finally:
            conn.close()
    except sqlite3.Error:
        return '/', 'Back'
    return ('/h/%d' % hid, 'Back to ' + h.name) if h else ('/', 'Back')



def static_tag() -> str:
    """The app version and a hash of every static file: changes whenever a file does."""
    h = hashlib.sha256()
    for f in sorted((HERE / 'static').rglob('*')):
        if f.is_file():
            h.update(f.relative_to(HERE).as_posix().encode() + b'\0' + f.read_bytes())
    return 'v%s-%s' % (__version__, h.hexdigest()[:10])


class LongCache(StaticFiles):
    """Static files under a versioned path: the content never changes there, so browsers keep it."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        r = super().file_response(*args, **kwargs)
        r.headers['cache-control'] = 'public, max-age=31536000, immutable'
        return r

# The service worker keeps only the offline page and its stylesheet and icon. It never stores a page, an API answer
# or anything else from the budget: those are only fetched from the network (D-089).
SERVICE_WORKER = """'use strict';
const CACHE = '%(cache)s';
const OFFLINE = ['/offline', '%(static)s/css/app.css', '%(static)s/icon.svg'];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(OFFLINE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const r = e.request;
  if (r.method !== 'GET') return;
  if (r.mode === 'navigate') {
    e.respondWith(fetch(r).catch(() => caches.match('/offline')));
    return;
  }
  if (OFFLINE.includes(new URL(r.url).pathname)) {
    e.respondWith(fetch(r).catch(() => caches.match(r)));
  }
});
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.check()
    for warning in settings.warnings():
        log.warning(warning)
    _start(settings)

    app = FastAPI(title='Hearthfund', version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  debug=settings.dev)
    app.state.settings = settings
    app.state.version = __version__
    app.state.clock = date.today
    app.state.started = datetime.now(UTC)
    app.state.hash_slots = HashSlots(settings.scrypt_slots, settings.scrypt_wait_seconds)
    app.state.cache = {}          # short-lived parsed files between two calls (workbook mapping, upload mapping)
    app.state.cache_lock = threading.Lock()
    app.state.templates = Jinja2Templates(directory=str(HERE / 'templates'))
    stop = threading.Event()
    app.state.stop = stop
    threading.Thread(target=_housekeeping, args=(settings, stop), daemon=True, name='hbudget-housekeeping').start()

    from .routes import api_access, api_admin, api_household, api_reconcile, api_v1, auth, mcp, pages
    app.state.api_rate = api_v1.RateLimiter(*api_v1.RATE_LIMIT)
    for r in (auth.router, pages.router, api_household.router, api_reconcile.router, api_admin.router,
              api_access.router, api_v1.router, mcp.router):
        app.include_router(r)
    # the optional Plaid connection (docs/PLAID.md): off by default, and then nothing of it is loaded
    app.state.templates.env.globals['plaid_on'] = settings.plaid != 'off'
    if settings.plaid != 'off':
        from hbudget.db.plaid import token_key
        from hbudget.io.plaid import PlaidClient

        from .routes import plaid
        assert settings.plaid_client_id and settings.plaid_secret    # Settings.check
        app.state.plaid_client = PlaidClient(settings.plaid, settings.plaid_client_id, settings.plaid_secret)
        app.state.plaid_key = token_key(secret_key(settings))
        app.include_router(plaid.router)
        app.include_router(plaid.api)
        threading.Thread(target=_plaid_daily, args=(app, stop), daemon=True, name='hbudget-plaid-daily').start()
    # pages link the versioned path, cached for a year; a new release or a changed file gives a new path (D-073)
    app.state.static_tag = static_tag()
    app.state.templates.env.globals['static'] = '/static/' + app.state.static_tag
    app.mount('/static/' + app.state.static_tag, LongCache(directory=str(HERE / 'static')), name='static_v')
    app.mount('/static', StaticFiles(directory=str(HERE / 'static')), name='static')

    @app.get('/offline', include_in_schema=False)
    def offline(request: Request) -> Response:
        """The page the service worker shows when the server cannot be reached; it has no budget data."""
        r: Response = app.state.templates.TemplateResponse(request, 'offline.html', {})
        return r

    @app.get('/sw.js', include_in_schema=False)
    def service_worker() -> Response:
        """The service worker (root scope): it only shows /offline when a page cannot load (D-089)."""
        body = SERVICE_WORKER % {'cache': 'hbudget-' + app.state.static_tag,
                                 'static': '/static/' + app.state.static_tag}
        return Response(body, media_type='text/javascript',
                        headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': '/'})

    @app.get('/healthz', include_in_schema=False)
    def healthz() -> dict[str, str]:
        conn = connection.connect(settings.db_path)
        try:
            conn.execute('SELECT 1').fetchone()
        finally:
            conn.close()
        return {'status': 'ok'}

    @app.exception_handler(NeedLogin)
    async def need_login(request: Request, exc: NeedLogin) -> Response:
        if _wants_json(request):
            return JSONResponse({'error': 'auth', 'message': 'Sign in first.'}, status_code=401)
        return RedirectResponse('/login', status_code=303)

    @app.exception_handler(NeedTwoFactor)
    async def need_two_factor(request: Request, exc: NeedTwoFactor) -> Response:
        if _wants_json(request):
            return JSONResponse({'error': 'two_factor_required',
                                 'message': 'Turn on two-factor sign-in first (Account).'}, status_code=403)
        return RedirectResponse('/account/2fa', status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        code = getattr(exc, 'code', None) or {400: 'bad_request', 401: 'auth', 403: 'forbidden', 404: 'not_found',
                                              405: 'method', 409: 'stale', 413: 'too_large', 415: 'type',
                                              429: 'too_many', 503: 'busy'}.get(exc.status_code, 'error')
        message = exc.detail if isinstance(exc.detail, str) else 'Request failed.'
        if exc.status_code == 404 and not isinstance(exc, ApiError):
            message = 'Not found.'
        if _wants_json(request):
            return JSONResponse({'error': code, 'message': message}, status_code=exc.status_code,
                                headers=getattr(exc, 'headers', None))
        t: Jinja2Templates = request.app.state.templates
        home, home_label = _home_link(request)
        return t.TemplateResponse(request, 'error.html', {'title': 'Sorry', 'message': message, 'home': home,
                                                          'home_label': home_label, 'version': __version__},
                                  status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> Response:
        first = exc.errors()[0] if exc.errors() else {}
        where = '.'.join(str(x) for x in first.get('loc', ())[1:]) or 'request'
        return JSONResponse({'error': 'validation', 'message': f'{where}: {first.get("msg", "invalid")}'},
                            status_code=400)

    @app.exception_handler(ImportProblem)
    async def import_problem(request: Request, exc: ImportProblem) -> Response:
        return JSONResponse({'error': 'import', 'message': str(exc)}, status_code=400)

    @app.exception_handler(repo.Conflict)
    async def conflict(request: Request, exc: repo.Conflict) -> Response:
        return JSONResponse({'error': 'stale', 'message': 'Someone else saved changes at the same time. The page '
                             'will reload.'}, status_code=409)

    @app.exception_handler(repo.LastOwner)
    async def last_owner(request: Request, exc: repo.LastOwner) -> Response:
        return JSONResponse({'error': 'last_owner', 'message': 'A household needs at least one owner.'},
                            status_code=400)

    @app.exception_handler(ReconcileError)
    async def reconcile_error(request: Request, exc: ReconcileError) -> Response:
        return JSONResponse({'error': exc.code, 'message': exc.message}, status_code=400)

    if not settings.dev:
        @app.exception_handler(Exception)
        async def crash(request: Request, exc: Exception) -> Response:
            log.error('unhandled error on %s %s: %s', request.method, request.url.path, type(exc).__name__)
            return JSONResponse({'error': 'server', 'message': 'Something went wrong on the server.'}, status_code=500)

    def session_csrf(token: str) -> str | None:
        conn = connection.connect(settings.db_path)
        try:
            s = users.get_session(conn, token, idle_hours=settings.session_hours)
            return s.csrf_token if s else None
        finally:
            conn.close()

    app.add_middleware(CsrfMiddleware, config=CsrfConfig(
        allowed_origins=settings.allowed_origins(), session_cookie_names=(SECURE_SESSION_COOKIE, SESSION_COOKIE),
        session_csrf=session_csrf))
    app.add_middleware(BodyLimitMiddleware, upload_limit=settings.max_upload_bytes)
    hsts = settings.cookie_secure == 'true'
    app.add_middleware(HeadersMiddleware, hsts=lambda scope: hsts or scope.get('scheme') == 'https')
    if '*' not in settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    return app


def cache_put(app: Any, key: str, value: Any, owner: tuple[int, int], ttl: float = 900) -> None:
    with app.state.cache_lock:
        now = time.monotonic()
        for k in [k for k, v in app.state.cache.items() if v[0] < now]:
            del app.state.cache[k]
        app.state.cache[key] = (now + ttl, owner, value)


def cache_get(app: Any, key: str, owner: tuple[int, int]) -> Any:
    with app.state.cache_lock:
        hit = app.state.cache.get(key)
        if not hit or hit[0] < time.monotonic() or hit[1] != owner:
            return None
        return hit[2]
