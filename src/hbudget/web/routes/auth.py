"""Sign-in, sign-out, first-run setup and the account page (ARCHITECTURE section 16)."""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from hbudget import demo
from hbudget.core import qr, totp
from hbudget.db import repo, twofactor, users
from hbudget.web.app import SETUP_HASH_KEY, SETUP_TOKEN_FILE
from hbudget.web.deps import (
    ApiError,
    Auth,
    Conn,
    CurrentAuth,
    client_ip,
    page_context,
    secure_cookies,
    session_token,
    settings_of,
    today_of,
)
from hbudget.web.security import (
    PRE_CSRF_COOKIE,
    SECURE_SESSION_COOKIE,
    SESSION_COOKIE,
    TWO_FACTOR_COOKIE,
    new_token,
)

router = APIRouter()


def _render(request: Request, name: str, ctx: dict[str, Any], status: int = 200) -> HTMLResponse:
    t = request.app.state.templates
    ctx.setdefault('version', request.app.state.version)
    return t.TemplateResponse(request, name, ctx, status_code=status)  # type: ignore[no-any-return]


def _pre_csrf(request: Request, response: Response, token: str) -> None:
    response.set_cookie(PRE_CSRF_COOKIE, token, httponly=True, samesite='strict', secure=secure_cookies(request),
                        path='/', max_age=3600)


def _pre_token(request: Request) -> str:
    return request.cookies.get(PRE_CSRF_COOKIE) or new_token()


def _sign_in(request: Request, conn: sqlite3.Connection, user_id: int, response: Response) -> None:
    s = settings_of(request)
    token, _ = users.create_session(conn, user_id, user_agent=request.headers.get('user-agent', ''),
                                    idle_hours=s.session_hours)
    secure = secure_cookies(request)
    name = SECURE_SESSION_COOKIE if secure else SESSION_COOKIE
    response.set_cookie(name, token, httponly=True, samesite='lax', secure=secure, path='/',
                        max_age=s.session_max_days * 86400)
    response.delete_cookie(PRE_CSRF_COOKIE, path='/')


# ---------------------------------------------------------------- login / logout

@router.get('/login', response_class=HTMLResponse)
def login_page(request: Request, conn: Conn) -> Response:
    if not users.user_count(conn):
        return RedirectResponse('/setup', status_code=303)
    tok = _pre_token(request)
    resp = _render(request, 'login.html', {'title': 'Sign in', 'csrf': tok})
    _pre_csrf(request, resp, tok)
    return resp


@router.post('/login')
async def login(request: Request, conn: Conn, username: Annotated[str, Form(max_length=64)] = '',
                password: Annotated[str, Form(max_length=256)] = '') -> Response:
    ip = client_ip(request)
    name = username.strip()
    tok = _pre_token(request)

    def again(error: str, status: int, headers: dict[str, str] | None = None) -> Response:
        resp = _render(request, 'login.html', {'title': 'Sign in', 'csrf': tok, 'error': error, 'username': name},
                       status)
        for k, v in (headers or {}).items():
            resp.headers[k] = v
        return resp

    slots = request.app.state.hash_slots
    result: list[int | None] = []
    held: list[int] = []
    two_factor: list[bool] = []

    def check() -> bool:
        # check the back-off and record the attempt inside the hashing slot, so a parallel burst cannot pass the
        # check before the earlier failures are recorded
        wait = users.too_many_attempts(conn, name, ip)
        if wait:
            held.append(wait)
            return True
        uid = users.verify_login(conn, name, password)
        user = users.get_user(conn, uid) if uid is not None else None
        two_factor.append(bool(user and user.two_factor))
        # with 2FA the password alone is not a sign-in: a success here must not clear the back-off, or knowing
        # the password would buy unlimited guesses at the code
        if not two_factor[0]:
            users.record_attempt(conn, name, ip, uid is not None)
        result.append(uid)
        return True
    if await slots.run(check) is None:
        return again('The server is busy. Try again in a moment.', 503, {'Retry-After': '2'})
    if held:
        return again('Too many attempts. Try again in %d seconds.' % held[0], 429, {'Retry-After': str(held[0])})
    uid = result[0]
    if uid is None:
        repo.audit(conn, household_id=None, user_id=None, action='login.failed')
        return again('That username and password do not match.', 401)
    if two_factor[0]:
        # recorded so that "the password was right but no code followed" can be seen in the activity log
        repo.audit(conn, household_id=None, user_id=uid, action='login.password.ok')
        resp = RedirectResponse('/login/code', status_code=303)
        resp.set_cookie(TWO_FACTOR_COOKIE, twofactor.new_challenge(conn, uid), httponly=True, samesite='strict',
                        secure=secure_cookies(request), path='/login', max_age=twofactor.CHALLENGE_MINUTES * 60)
        _pre_csrf(request, resp, tok)
        return resp
    repo.audit(conn, household_id=None, user_id=uid, action='login.ok')
    resp = RedirectResponse('/', status_code=303)
    _sign_in(request, conn, uid, resp)
    return resp


@router.get('/login/code', response_class=HTMLResponse)
def login_code_page(request: Request, conn: Conn) -> Response:
    if twofactor.challenge_user(conn, request.cookies.get(TWO_FACTOR_COOKIE)) is None:
        return RedirectResponse('/login', status_code=303)
    tok = _pre_token(request)
    resp = _render(request, 'login_code.html', {'title': 'Enter your code', 'csrf': tok})
    _pre_csrf(request, resp, tok)
    return resp


@router.post('/login/code')
async def login_code(request: Request, conn: Conn, code: Annotated[str, Form(max_length=40)] = '') -> Response:
    """The second step (D-095): the challenge cookie says whose password was right; the code finishes the sign-in.
    Wrong codes count towards the same back-off as wrong passwords, per user and per address."""
    challenge = request.cookies.get(TWO_FACTOR_COOKIE)
    uid = twofactor.challenge_user(conn, challenge)
    user = users.get_user(conn, uid) if uid is not None else None
    if user is None:
        return RedirectResponse('/login', status_code=303)
    ip = client_ip(request)
    tok = _pre_token(request)

    def again(error: str, status: int, headers: dict[str, str] | None = None) -> Response:
        resp = _render(request, 'login_code.html', {'title': 'Enter your code', 'csrf': tok, 'error': error}, status)
        for k, v in (headers or {}).items():
            resp.headers[k] = v
        return resp
    held: list[int] = []
    how: list[str | None] = []

    def check() -> bool:
        wait = users.too_many_attempts(conn, user.username, ip)
        if wait:
            held.append(wait)
            return True
        how.append(twofactor.check(conn, user.id, code))
        if how[0] != twofactor.REUSED:
            users.record_attempt(conn, user.username, ip, how[0] is not None)
        return True
    if await request.app.state.hash_slots.run(check) is None:
        return again('The server is busy. Try again in a moment.', 503, {'Retry-After': '2'})
    if held:
        return again('Too many attempts. Try again in %d seconds.' % held[0], 429, {'Retry-After': str(held[0])})
    if how[0] == twofactor.REUSED:
        return again('That code was just used. Wait for the next one the app shows.', 401)
    if how[0] is None:
        repo.audit(conn, household_id=None, user_id=user.id, action='login.code.failed')
        if twofactor.codes_locked(conn, user.id):
            if twofactor.failures(conn, user.id) == twofactor.CODE_FAILURES:
                repo.audit(conn, household_id=None, user_id=user.id, action='2fa.locked')
            return again('Too many wrong codes today, so codes from the app are refused for now. Use one of your '
                         'recovery codes, or ask the admin to reset two-factor sign-in.', 429)
        return again('That code is not right. Codes change every 30 seconds and each works once.', 401)
    twofactor.drop_challenge(conn, challenge)
    repo.audit(conn, household_id=None, user_id=user.id, action='login.ok',
               detail={'second_step': how[0]})
    # after a recovery code, show how many are left
    resp = RedirectResponse('/account/2fa' if how[0] == 'recovery' else '/', status_code=303)
    _sign_in(request, conn, user.id, resp)
    resp.delete_cookie(TWO_FACTOR_COOKIE, path='/login')
    return resp


@router.get('/logout', response_class=HTMLResponse)
def logout_page(request: Request, conn: Conn, auth: CurrentAuth) -> Response:
    return _render(request, 'error.html', {'title': 'Sign out', 'message': 'Use the Sign out button at the top.',
                                           'csrf': auth.csrf})


@router.post('/logout')
def logout(request: Request, conn: Conn) -> Response:
    token = session_token(request)
    if token:
        sess = users.get_session(conn, token)
        users.delete_session(conn, token)
        if sess:
            repo.audit(conn, household_id=None, user_id=sess.user_id, action='logout')
    resp = RedirectResponse('/login', status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path='/')
    resp.delete_cookie(SECURE_SESSION_COOKIE, path='/', secure=True)
    return resp


# ---------------------------------------------------------------- first run

def _setup_open(conn: sqlite3.Connection) -> bool:
    return users.user_count(conn) == 0


@router.get('/setup', response_class=HTMLResponse)
def setup_page(request: Request, conn: Conn) -> Response:
    if not _setup_open(conn):
        raise ApiError(404, 'not_found', 'Not found.')
    tok = _pre_token(request)
    resp = _render(request, 'setup.html', {'title': 'Set up Hearthfund', 'csrf': tok,
                                           'demo_default': settings_of(request).demo})
    _pre_csrf(request, resp, tok)
    return resp


def _demo(conn: sqlite3.Connection, hid: int, request: Request, user_id: int) -> None:
    """Fill a household with the made-up demo data (budget, history and a waiting bank export)."""
    demo.install(conn, hid, today=today_of(request), user_id=user_id)


@router.post('/setup')
def setup(request: Request, conn: Conn, token: Annotated[str, Form(max_length=128)] = '',
          username: Annotated[str, Form(max_length=64)] = '', password: Annotated[str, Form(max_length=256)] = '',
          household: Annotated[str, Form(max_length=80)] = 'Household',
          start: Annotated[str, Form(max_length=10)] = 'empty') -> Response:
    if not _setup_open(conn):
        raise ApiError(404, 'not_found', 'Not found.')
    tok = _pre_token(request)

    def again(error: str, status: int = 400) -> Response:
        return _render(request, 'setup.html', {'title': 'Set up Hearthfund', 'csrf': tok, 'error': error,
                                               'username': username, 'household': household}, status)
    stored = repo.get_setting(conn, SETUP_HASH_KEY)
    given = hashlib.sha256(token.strip().encode()).hexdigest()
    if not stored or not hmac.compare_digest(stored, given):
        return again('That setup token is not right. It is in the file setup-token in the data folder.', 403)
    try:
        uid, hid = users.create_first_admin(conn, username, password, household)
    except users.SetupDone:
        return again('Setup is already done.', 409)
    except ValueError as e:
        return again(str(e))
    repo.delete_setting(conn, SETUP_HASH_KEY)
    (settings_of(request).data_dir / SETUP_TOKEN_FILE).unlink(missing_ok=True)
    repo.audit(conn, household_id=None, user_id=uid, action='setup.done')
    if start == 'demo':
        _demo(conn, hid, request, uid)
    resp = RedirectResponse('/h/%d' % hid, status_code=303)
    _sign_in(request, conn, uid, resp)
    return resp


# ---------------------------------------------------------------- account

@router.get('/account', response_class=HTMLResponse)
def account_page(request: Request, conn: Conn, auth: CurrentAuth) -> Response:
    hh = [{'id': h.id, 'name': h.name, 'role': r} for h, r in repo.households_for_user(conn, auth.user.id)]
    ctx = page_context(request, conn, auth, title='Account', page_data={'csrf': auth.csrf})
    ctx['households'] = hh
    ctx['two_factor'] = auth.user.two_factor
    return _render(request, 'account.html', ctx)


@router.post('/account/password', response_class=HTMLResponse)
async def change_password(request: Request, conn: Conn, auth: CurrentAuth,
                          current: Annotated[str, Form(max_length=256)] = '',
                          new: Annotated[str, Form(max_length=256)] = '') -> Response:
    hh = [{'id': h.id, 'name': h.name, 'role': r} for h, r in repo.households_for_user(conn, auth.user.id)]

    def page(status: int | None = None, headers: dict[str, str] | None = None, **kw: Any) -> Response:
        ctx = page_context(request, conn, auth, title='Account', page_data={'csrf': auth.csrf}, **kw)
        ctx['households'] = hh
        ctx['two_factor'] = auth.user.two_factor
        resp = _render(request, 'account.html', ctx, status or (400 if 'error' in kw else 200))
        for k, v in (headers or {}).items():
            resp.headers[k] = v
        return resp
    result: list[int | None] = []
    held: list[int] = []
    ip = client_ip(request)

    def check() -> bool:
        # throttled like /login: a stolen session cannot be used to guess the password
        wait = users.too_many_attempts(conn, auth.user.username, ip)
        if wait:
            held.append(wait)
            return True
        uid = users.verify_login(conn, auth.user.username, current)
        users.record_attempt(conn, auth.user.username, ip, uid is not None, clears=not auth.user.two_factor)
        result.append(uid)
        return True
    if await request.app.state.hash_slots.run(check) is None:
        return page(error='The server is busy. Try again in a moment.')
    if held:
        wait = held[0]
        return page(429, {'Retry-After': str(wait)}, error='Too many attempts. Try again in %d seconds.' % wait)
    if result[0] != auth.user.id:
        repo.audit(conn, household_id=None, user_id=auth.user.id, action='password.change.failed')
        return page(error='The current password is not right.')
    try:
        users.set_password(conn, auth.user.id, new, keep_token=auth.token)
    except ValueError as e:
        return page(error=str(e))
    repo.audit(conn, household_id=None, user_id=auth.user.id, action='password.change')
    return page(notice='Password changed. Your other sessions were signed out.')


# ---------------------------------------------------------------- two-factor sign-in (D-095)

async def _throttled(request: Request, conn: sqlite3.Connection, username: str, test: Any, *,
                     clears: bool = True) -> tuple[bool, str]:
    """Run `test` (password or code check) under the sign-in back-off for this user and address.
    (passed, message when it did not). `test` may return twofactor.REUSED: refused, but not counted as a guess.
    `clears=False` keeps the back-off after a success that proves only the password."""
    ip = client_ip(request)
    held: list[int] = []
    got: list[object] = []

    def check() -> bool:
        wait = users.too_many_attempts(conn, username, ip)
        if wait:
            held.append(wait)
            return True
        got.append(test())
        if got[0] != twofactor.REUSED:
            users.record_attempt(conn, username, ip, bool(got[0]), clears=clears)
        return True
    if await request.app.state.hash_slots.run(check) is None:
        return False, 'The server is busy. Try again in a moment.'
    if held:
        return False, 'Too many attempts. Try again in %d seconds.' % held[0]
    if got[0] == twofactor.REUSED:
        return False, 'That code was just used. Wait for the next one the app shows.'
    return bool(got[0]), ''


def _two_factor_page(request: Request, conn: sqlite3.Connection, auth: Auth, *, status: int = 200,
                     codes: list[str] | None = None, **kw: Any) -> Response:
    st = twofactor.state(conn, auth.user.id, session=auth.token)
    ctx = page_context(request, conn, auth, title='Two-factor sign-in', page_data={'csrf': auth.csrf}, **kw)
    ctx.update(state=st, codes=codes, required=settings_of(request).require_2fa)
    if st.pending_secret:
        link = totp.uri(st.pending_secret, auth.user.username, 'Hearthfund')
        spaced = ' '.join(st.pending_secret[i:i + 4] for i in range(0, len(st.pending_secret), 4))
        try:
            picture: str | None = qr.svg(link.encode('utf-8'))
        except ValueError:      # a long username with accents does not fit the codes drawn here: the key is typed
            picture = None
        ctx.update(secret=spaced, link=link, qr_svg=picture, minutes=twofactor.PENDING_MINUTES)
    if 'error' in kw and status == 200:
        status = 429 if str(kw['error']).startswith('Too many') else 400
    return _render(request, 'twofactor.html', ctx, status)


@router.get('/account/2fa', response_class=HTMLResponse)
def two_factor_page(request: Request, conn: Conn, auth: CurrentAuth) -> Response:
    return _two_factor_page(request, conn, auth)


@router.post('/account/2fa/start', response_class=HTMLResponse)
async def two_factor_start(request: Request, conn: Conn, auth: CurrentAuth,
                           password: Annotated[str, Form(max_length=256)] = '') -> Response:
    """Begin enrolment. The password is asked again so a borrowed session cannot add its own authenticator."""
    name = auth.user.username
    ok, why = await _throttled(request, conn, name, lambda: users.verify_login(conn, name, password) == auth.user.id,
                               clears=not auth.user.two_factor)
    if not ok:
        return _two_factor_page(request, conn, auth, error=why or 'The password is not right.')
    try:
        twofactor.begin(conn, auth.user.id, session=auth.token)
    except ValueError as e:
        return _two_factor_page(request, conn, auth, error=str(e))
    return _two_factor_page(request, conn, auth)


@router.post('/account/2fa/cancel', response_class=HTMLResponse)
def two_factor_cancel(request: Request, conn: Conn, auth: CurrentAuth) -> Response:
    twofactor.cancel(conn, auth.user.id)
    return _two_factor_page(request, conn, auth)


@router.post('/account/2fa/confirm', response_class=HTMLResponse)
async def two_factor_confirm(request: Request, conn: Conn, auth: CurrentAuth,
                             code: Annotated[str, Form(max_length=40)] = '') -> Response:
    if twofactor.state(conn, auth.user.id, session=auth.token).pending_secret is None:
        # nothing pending for this session: it was started elsewhere, or too long ago
        return _two_factor_page(request, conn, auth, error='That set-up has run out. Start it again here.')
    made: list[list[str] | None] = []

    def test() -> bool:
        made.append(twofactor.confirm(conn, auth.user.id, code, session=auth.token))
        return made[0] is not None
    ok, why = await _throttled(request, conn, auth.user.username, test)
    codes = made[0] if ok and made else None
    if codes is None:
        return _two_factor_page(request, conn, auth, error=why or 'That code is not right. Enter the 6 digits the '
                                                                  'app shows now.')
    repo.audit(conn, household_id=None, user_id=auth.user.id, action='2fa.on')
    return _two_factor_page(request, conn, auth, codes=codes,
                            notice='Two-factor sign-in is on. Your other sessions were signed out.')


@router.post('/account/2fa/codes', response_class=HTMLResponse)
async def two_factor_codes(request: Request, conn: Conn, auth: CurrentAuth,
                           code: Annotated[str, Form(max_length=40)] = '') -> Response:
    ok, why = await _throttled(request, conn, auth.user.username,
                               lambda: twofactor.check(conn, auth.user.id, code, recovery=False))
    if not ok:
        return _two_factor_page(request, conn, auth, error=why or 'That code is not right.')
    codes = twofactor.new_recovery_codes(conn, auth.user.id)
    repo.audit(conn, household_id=None, user_id=auth.user.id, action='2fa.codes')
    return _two_factor_page(request, conn, auth, codes=codes, notice='New recovery codes. The old ones no longer work.')


@router.post('/account/2fa/off', response_class=HTMLResponse)
async def two_factor_off(request: Request, conn: Conn, auth: CurrentAuth,
                         password: Annotated[str, Form(max_length=256)] = '',
                         code: Annotated[str, Form(max_length=40)] = '') -> Response:
    if settings_of(request).require_2fa:
        return _two_factor_page(request, conn, auth, error='This server requires two-factor sign-in.')
    name = auth.user.username

    def both() -> object:
        if users.verify_login(conn, name, password) != auth.user.id:
            return False
        how = twofactor.check(conn, auth.user.id, code)
        return how if how == twofactor.REUSED else how is not None
    ok, why = await _throttled(request, conn, name, both)
    if not ok:
        return _two_factor_page(request, conn, auth, error=why or 'The password or the code is not right.')
    twofactor.disable(conn, auth.user.id, keep_token=auth.token)
    repo.audit(conn, household_id=None, user_id=auth.user.id, action='2fa.off')
    return _two_factor_page(request, conn, auth,
                            notice='Two-factor sign-in is off. Your other sessions were signed out.')


@router.get('/')
def home(request: Request, conn: Conn) -> Response:
    if not users.user_count(conn):
        return RedirectResponse('/setup', status_code=303)
    from hbudget.web.deps import current_auth
    auth = current_auth(request, conn)
    hh = repo.households_for_user(conn, auth.user.id)
    if hh:
        return RedirectResponse('/h/%d' % hh[0][0].id, status_code=303)
    return RedirectResponse('/admin' if auth.user.is_admin else '/account', status_code=303)


@router.get('/switch')
def switch(request: Request, conn: Conn, auth: CurrentAuth, hid: int = 0) -> Response:
    if not repo.get_role(conn, auth.user.id, hid):
        raise ApiError(404, 'not_found', 'Not found.')
    return RedirectResponse('/h/%d' % hid, status_code=303)
