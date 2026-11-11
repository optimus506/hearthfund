"""Password hashing, tokens, CSRF, security headers and body limits (ARCHITECTURE section 16).

The middlewares here are plain ASGI so they run before FastAPI parses a body: the CSRF check for a multipart
upload happens on the headers alone, and an oversize JSON body is refused before it is read in full.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

SESSION_COOKIE = 'hb_session'
SECURE_SESSION_COOKIE = '__Host-hb_session'
PRE_CSRF_COOKIE = 'hb_csrf_pre'
TWO_FACTOR_COOKIE = 'hb_2fa'          # between the password and the code; sent to /login only (D-095)
CSRF_HEADER = 'x-csrf-token'
BEARER_PATHS = ('/api/v1/', '/mcp')   # routes that take only an access token (routes/api_v1.py, mcp.py)
CSRF_FIELD = 'csrf_token'
CSRF_ERROR = {'error': 'csrf', 'message': 'Missing or expired security token. Reload the page.'}
UNSAFE = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
PRE_SESSION_PATHS = frozenset({'/login', '/login/code', '/setup'})
MAX_FORM_BYTES = 64 * 1024
MAX_JSON_BYTES = 1024 * 1024

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


# ---------------------------------------------------------------- password checks (hashing lives in db.users)

class HashSlots:
    """At most `slots` scrypt computations at once; a caller waits at most `wait` seconds for a slot."""

    def __init__(self, slots: int, wait: float) -> None:
        self._sem = asyncio.Semaphore(slots)
        self._wait = wait

    async def run(self, fn: Callable[[], bool]) -> bool | None:
        """None = no free slot in time (the caller answers 503)."""
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._wait)
        except TimeoutError:
            return None
        try:
            return await asyncio.to_thread(fn)
        finally:
            self._sem.release()


# ---------------------------------------------------------------- tokens

def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


# ---------------------------------------------------------------- ASGI helpers

def header(scope: Scope, name: str) -> str | None:
    key = name.lower().encode()
    for k, v in scope.get('headers', []):
        if k == key:
            return bytes(v).decode('latin-1')
    return None


def cookies(scope: Scope) -> dict[str, str]:
    raw = header(scope, 'cookie')
    if not raw:
        return {}
    c: SimpleCookie = SimpleCookie()
    try:
        c.load(raw)
    except Exception:  # a malformed cookie header is treated as no cookies
        return {}
    return {k: m.value for k, m in c.items()}


async def send_json(send: Send, status: int, body: dict[str, str], extra: Iterable[tuple[bytes, bytes]] = ()) -> None:
    data = json.dumps(body).encode()
    headers = [(b'content-type', b'application/json'), (b'content-length', str(len(data)).encode()),
               (b'cache-control', b'no-store'), *extra]
    await send({'type': 'http.response.start', 'status': status, 'headers': headers})
    await send({'type': 'http.response.body', 'body': data})


async def read_body(receive: Receive, limit: int) -> bytes | None:
    """The whole body, or None when it is larger than `limit`."""
    chunks, size = [], 0
    while True:
        msg = await receive()
        if msg['type'] != 'http.request':
            break
        part = msg.get('body', b'')
        size += len(part)
        if size > limit:
            return None
        chunks.append(part)
        if not msg.get('more_body'):
            break
    return b''.join(chunks)


def replay(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return {'type': 'http.disconnect'}
    return receive


# ---------------------------------------------------------------- CSRF (deny by default)

@dataclass(frozen=True)
class CsrfConfig:
    allowed_origins: frozenset[str]
    session_cookie_names: tuple[str, ...]
    # session cookie token -> the session's CSRF token, or None when there is no such live session
    session_csrf: Callable[[str], str | None]


class CsrfMiddleware:
    """Rejects every POST/PUT/PATCH/DELETE without a valid token.

    - The one exception: the token API (/api/v1/..., /mcp). Those routes authenticate with an
      `Authorization: Bearer` token only and never read cookies, and a browser cannot attach that header to a
      cross-site request (no CORS is offered), so there is no ambient credential to forge with. A request without
      a token reaches them and gets their 401. The Origin check below still applies to them.

    - With a session cookie: the session's synchronizer token, in the X-CSRF-Token header or (urlencoded forms
      only) the csrf_token field.
    - /login and /setup without a session: the double-submit pre-session cookie, compared in constant time.
    - Multipart bodies must use the header; it is checked before the body is read.
    - Origin (or Referer) must be one of the configured origins when present.
    """

    def __init__(self, app: ASGIApp, config: CsrfConfig) -> None:
        self.app = app
        self.cfg = config

    def _origin_ok(self, scope: Scope) -> bool:
        origin = header(scope, 'origin')
        if origin is None:
            ref = header(scope, 'referer')
            if ref is None:
                return True   # same-origin form posts from old browsers; the token still decides
            parts = urlsplit(ref)
            origin = f'{parts.scheme}://{parts.netloc}'
        if origin == 'null':
            return False
        return origin.rstrip('/').lower() in self.cfg.allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or scope['method'] not in UNSAFE:
            await self.app(scope, receive, send)
            return
        if not self._origin_ok(scope):
            await send_json(send, 403, {'error': 'csrf', 'message': 'This request came from another site.'})
            return
        path = scope.get('path', '')
        if path.startswith(BEARER_PATHS):
            await self.app(scope, receive, send)
            return
        jar = cookies(scope)
        session = next((jar[n] for n in self.cfg.session_cookie_names if n in jar), None)
        found = self.cfg.session_csrf(session) if session else None
        expected = [found] if found else []
        if scope.get('path', '') in PRE_SESSION_PATHS and jar.get(PRE_CSRF_COOKIE):
            expected.append(jar[PRE_CSRF_COOKIE])
        if not expected:
            await send_json(send, 403, CSRF_ERROR)
            return
        given = header(scope, CSRF_HEADER)
        ctype = (header(scope, 'content-type') or '').split(';')[0].strip().lower()
        if given is None and ctype == 'application/x-www-form-urlencoded':
            body = await read_body(receive, MAX_FORM_BYTES)
            if body is None:
                await send_json(send, 413, {'error': 'too_large', 'message': 'The form is too large.'})
                return
            given = (parse_qs(body.decode('utf-8', 'replace')).get(CSRF_FIELD) or [''])[0] or None
            receive = replay(body)
        if not any(same(given, e) for e in expected):
            await send_json(send, 403, CSRF_ERROR)
            return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------- body size limits

class BodyLimitMiddleware:
    """413 for a JSON (or other non-multipart) body over 1 MB, and for any body over the upload limit."""

    def __init__(self, app: ASGIApp, upload_limit: int) -> None:
        self.app = app
        # multipart framing adds a little; allow 64 KB on top of the file limit
        self.upload_limit = upload_limit + 64 * 1024

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or scope['method'] not in UNSAFE:
            await self.app(scope, receive, send)
            return
        ctype = (header(scope, 'content-type') or '').split(';')[0].strip().lower()
        limit = self.upload_limit if ctype == 'multipart/form-data' else MAX_JSON_BYTES
        declared = header(scope, 'content-length')
        if declared is not None:
            try:
                if int(declared) > limit:
                    await send_json(send, 413, {'error': 'too_large', 'message': 'The request is too large.'})
                    return
            except ValueError:
                await send_json(send, 400, {'error': 'bad_request', 'message': 'Bad Content-Length.'})
                return
        # read the whole body first (at most the limit), so a chunked body without Content-Length that is too
        # large gets a clean 413 instead of a parse error inside the app
        body = await read_body(receive, limit)
        if body is None:
            await send_json(send, 413, {'error': 'too_large', 'message': 'The request is too large.'})
            return
        await self.app(scope, replay(body), send)


# ---------------------------------------------------------------- headers

class HeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: Callable[[Scope], bool]) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        async def wrapped(msg: Message) -> None:
            if msg['type'] == 'http.response.start':
                hs = [(k, v) for k, v in msg.get('headers', []) if k.lower() not in (b'server',)]
                names = {k.lower() for k, _ in hs}
                add = [(b'content-security-policy', CSP.encode()), (b'x-content-type-options', b'nosniff'),
                       (b'referrer-policy', b'same-origin'), (b'x-frame-options', b'DENY'),
                       (b'permissions-policy', b'camera=(), microphone=(), geolocation=()'),
                       (b'cross-origin-opener-policy', b'same-origin')]
                if b'cache-control' not in names:   # versioned static files set their own (LongCache)
                    add.append((b'cache-control', b'no-cache' if scope.get('path', '').startswith('/static/')
                                else b'no-store'))
                if self.hsts(scope):
                    add.append((b'strict-transport-security', b'max-age=31536000'))
                msg['headers'] = hs + [(k, v) for k, v in add if k not in names]
            await send(msg)
        await self.app(scope, receive, wrapped)
