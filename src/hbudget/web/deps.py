"""FastAPI dependencies: the database connection, the signed-in user, and the household role guard."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from hbudget.db import connection, repo, users

from .config import Settings
from .security import SECURE_SESSION_COOKIE, SESSION_COOKIE

ROLE_ORDER = {'viewer': 0, 'contributor': 1, 'editor': 2, 'owner': 3}


class ApiError(HTTPException):
    """An error with a machine-readable code: answered as {"error": code, "message": text}."""

    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code=status, detail=message, headers=headers)
        self.code = code


class NeedLogin(Exception):
    """No valid session: pages redirect to /login (or /setup), the API answers 401."""


class NeedTwoFactor(Exception):
    """HBUDGET_REQUIRE_2FA is on and this user has not set it up: pages redirect to /account/2fa, the API
    answers 403. Only the set-up pages and signing out are open."""


TWO_FACTOR_OPEN = ('/account/2fa', '/logout')


def settings_of(request: Request) -> Settings:
    s: Settings = request.app.state.settings
    return s


def today_of(request: Request) -> date:
    clock: Callable[[], date] = request.app.state.clock
    return clock()


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connection.connect(settings_of(request).db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def session_token(request: Request) -> str | None:
    return request.cookies.get(SECURE_SESSION_COOKIE) or request.cookies.get(SESSION_COOKIE)


@dataclass(frozen=True)
class Auth:
    user: users.User
    token: str
    csrf: str


def current_auth(request: Request, conn: Conn) -> Auth:
    """The signed-in user, re-read on every request (a disabled user is signed out at once)."""
    token = session_token(request)
    if not token:
        raise NeedLogin
    sess = users.get_session(conn, token, idle_hours=settings_of(request).session_hours)
    if sess is None:
        raise NeedLogin
    user = users.get_user(conn, sess.user_id)
    if user is None or user.disabled:
        raise NeedLogin
    if settings_of(request).require_2fa and not user.two_factor and not request.url.path.startswith(TWO_FACTOR_OPEN):
        raise NeedTwoFactor
    return Auth(user, token, sess.csrf_token)


CurrentAuth = Annotated[Auth, Depends(current_auth)]


@dataclass(frozen=True)
class Member:
    auth: Auth
    hid: int
    role: str
    household: repo.Household
    conn: sqlite3.Connection
    today: date

    @property
    def user_id(self) -> int:
        return self.auth.user.id


def member(min_role: str) -> Callable[..., Member]:
    """Dependency: the caller's membership of household {hid}. Not a member (or no such household): 404 with no
    hint that it exists. A weaker role than `min_role`: 403."""
    need = ROLE_ORDER[min_role]

    def dep(hid: int, request: Request, auth: CurrentAuth, conn: Conn) -> Member:
        role = repo.get_role(conn, auth.user.id, hid)
        h = repo.get_household(conn, hid) if role else None
        if role is None or h is None:
            raise ApiError(404, 'not_found', 'Not found.')
        if ROLE_ORDER[role] < need:
            raise ApiError(403, 'forbidden', 'Your role in this household cannot do that.')
        return Member(auth, hid, role, h, conn, effective_today(conn, hid, today_of(request)))
    return dep


Viewer = Annotated[Member, Depends(member('viewer'))]
Contributor = Annotated[Member, Depends(member('contributor'))]
Editor = Annotated[Member, Depends(member('editor'))]
Owner = Annotated[Member, Depends(member('owner'))]


def effective_today(conn: sqlite3.Connection, hid: int, today: date) -> date:
    """The later of the real date and the budget's as-of date. A budget that starts in the future (a plan made
    ahead, or a test fixture) is then shown from its own start: the Bills window, "due within 7 days" and the payoff
    plan all begin there."""
    row = conn.execute('SELECT as_of FROM households WHERE id = ?', (hid,)).fetchone()
    as_of = date.fromisoformat(row[0]) if row and row[0] else today
    return max(today, as_of)


def require_admin(auth: CurrentAuth) -> Auth:
    if not auth.user.is_admin:
        raise ApiError(404, 'not_found', 'Not found.')
    return auth


Admin = Annotated[Auth, Depends(require_admin)]


def client_ip(request: Request) -> str:
    """The peer address (uvicorn has already applied X-Forwarded-For when it came from a trusted proxy)."""
    return request.client.host if request.client else 'unknown'


def is_https(request: Request) -> bool:
    return request.url.scheme == 'https'


def secure_cookies(request: Request) -> bool:
    mode = settings_of(request).cookie_secure
    return mode == 'true' or (mode == 'auto' and is_https(request))


def page_context(request: Request, conn: sqlite3.Connection, auth: Auth | None, **extra: Any) -> dict[str, Any]:
    hh = repo.households_for_user(conn, auth.user.id) if auth else []
    ctx: dict[str, Any] = {'request': request, 'user': auth.user if auth else None, 'csrf': auth.csrf if auth else '',
                           'households': [h for h, _ in hh], 'version': request.app.state.version}
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------- a code for changes that open a way in (D-095)
CODE_HEADER = 'x-2fa-code'
_CODE_LOCK = threading.Lock()


def second_factor(request: Request, conn: sqlite3.Connection, user: users.User) -> None:
    """A user with two-factor sign-in gives a current code (header X-2FA-Code) for a change that would outlive
    a borrowed session: an access token, a new user or member, an admin flag, a reset of someone's password or
    two-factor sign-in. Wrong codes count towards the sign-in back-off. One check at a time, so a burst of
    parallel guesses cannot get past the back-off before the failures are recorded."""
    from hbudget.db import twofactor  # imported here: db.twofactor is not needed by most requests
    if not user.two_factor:
        return
    code = request.headers.get(CODE_HEADER, '').strip()
    if not code:
        raise ApiError(403, 'two_factor', 'Enter a code from your authenticator app.')
    ip = client_ip(request)
    with _CODE_LOCK:
        wait = users.too_many_attempts(conn, user.username, ip)
        if wait:
            raise ApiError(429, 'too_many', 'Too many attempts. Try again in %d seconds.' % wait,
                           headers={'Retry-After': str(wait)})
        how = twofactor.check(conn, user.id, code, recovery=False)
        if how == twofactor.REUSED:
            raise ApiError(403, 'two_factor', 'That code was just used. Wait for the next one the app shows.')
        users.record_attempt(conn, user.username, ip, how is not None)
    if how is None:
        if twofactor.codes_locked(conn, user.id):
            if twofactor.failures(conn, user.id) == twofactor.CODE_FAILURES:
                repo.audit(conn, household_id=None, user_id=user.id, action='2fa.locked')
            raise ApiError(429, 'too_many', 'Too many wrong codes today: codes from the app are refused for now. '
                           'Sign in again with a recovery code, or ask the admin.')
        raise ApiError(403, 'two_factor', 'That code is not right. Enter the code the app shows now.')
