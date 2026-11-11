"""The optional Plaid bank connection (docs/PLAID.md). Registered only when HBUDGET_PLAID is not `off`.

A household owner turns it on and links a bank through Plaid Link; a sync stores what Plaid sent (fetch) and turns
it into ordinary uploads (bring_in): one waiting bank upload for the reconciled checking account (reviewed in the
check-in like a file) and an imported card upload for card accounts. Nothing here runs unless a member asks for
it, except the optional daily sync (auto_sync), which only fetches: the uploads are still made when a member syncs.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import Field

from hbudget.core.cards import categorise
from hbudget.core.models import CardTxn
from hbudget.db import connection, repo
from hbudget.db import plaid as pdb
from hbudget.db.connection import transaction
from hbudget.io import clean_filename
from hbudget.io import plaid as pl
from hbudget.web.deps import ApiError, Conn, CurrentAuth, Editor, Member, Owner, Viewer, member, page_context
from hbudget.web.routes.api_household import Body, load, new_id
from hbudget.web.routes.pages import nav

router = APIRouter()
api = APIRouter(prefix='/api/h/{hid}/plaid')

LINK_SCRIPT = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js'
WINDOW_DAYS = 7          # a bank upload reaches this far behind the last reconciliation, like a weekly export
AUTO_EVERY = timedelta(hours=20)    # the daily sync leaves a bank alone that synced more recently than this


def link_csp(env: str) -> str:
    """The Link pages' policy: today's, plus Plaid's script, its frame and its API host. No other page has it."""
    return ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self' %s; "
            "frame-src https://cdn.plaid.com; connect-src 'self' https://%s; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'" % (LINK_SCRIPT, pl.HOSTS[env]))


def client(request: Request) -> pl.PlaidClient:
    c: pl.PlaidClient = request.app.state.plaid_client
    return c


def _key(request: Request) -> bytes:
    k: bytes = request.app.state.plaid_key
    return k


def _require_on(m: Member) -> None:
    if not pdb.enabled(m.conn, m.hid):
        raise ApiError(409, 'plaid_off', 'The bank connection is turned off for this household.')


def _token(request: Request, m: Member, item: pdb.Item) -> str:
    try:
        return pdb.unseal(_key(request), item.token_sealed, pdb.aad(m.hid, item.item_id))
    except ValueError:
        raise ApiError(409, 'relink', 'This bank connection was saved with another secret key. Unlink it and link '
                                      'the bank again.') from None


def _plaid_call[T](fn: Callable[..., T], *args: Any, **kw: Any) -> T:
    try:
        return fn(*args, **kw)
    except pl.PlaidError as e:
        raise ApiError(502, 'plaid', e.message) from None


def _audit(m: Member, action: str, row: str | None = None, **detail: Any) -> None:
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action=action, row_id=row, detail=detail)


# ---------------------------------------------------------------- state

def status(request: Request, m: Member) -> dict[str, Any]:
    on = pdb.enabled(m.conn, m.hid)
    accts = pdb.accounts(m.conn, m.hid) if on else []
    items = []
    for it in pdb.items(m.conn, m.hid) if on else []:
        items.append({'item_id': it.item_id, 'institution': it.institution, 'linked_at': it.linked_at,
                      'last_sync_at': it.last_sync_at, 'last_error': it.last_error,
                      'accounts': [{'account_id': a.account_id, 'name': a.name, 'mask': a.mask, 'type': a.type,
                                    'subtype': a.subtype, 'role': a.role, 'current': a.current}
                                   for a in accts if a.item_id == it.item_id]})
    s = request.app.state.settings
    return {'server': s.plaid, 'enabled': on, 'items': items, 'oauth': s.plaid_redirect_uri is not None,
            'days_choices': list(pl.DAYS_CHOICES), 'household_name': m.household.name,
            'auto_sync': pdb.auto_sync(m.conn, m.hid) if on else 'off'}


@api.get('')
def get_status(request: Request, m: Viewer) -> dict[str, Any]:
    return status(request, m)


class EnableBody(Body):
    confirm: str = Field(max_length=200)


@api.post('/enable')
def enable(body: EnableBody, request: Request, m: Owner) -> dict[str, Any]:
    if body.confirm.strip().casefold() != m.household.name.strip().casefold():
        raise ApiError(400, 'invalid', 'Type the household name exactly to turn the bank connection on.')
    pdb.enable(m.conn, m.hid, m.auth.user.username)
    _audit(m, 'plaid.enable', env=request.app.state.settings.plaid)
    return status(request, m)


@api.post('/disable')
def disable(request: Request, m: Owner) -> dict[str, Any]:
    """Unlink every bank at Plaid, then forget the tokens. The stored bank and card history stays."""
    problems = []
    for it in pdb.items(m.conn, m.hid):
        try:
            client(request).remove(pdb.unseal(_key(request), it.token_sealed, pdb.aad(m.hid, it.item_id)))
        except (pl.PlaidError, ValueError):
            problems.append(it.institution or it.item_id)
    pdb.disable(m.conn, m.hid)
    _audit(m, 'plaid.disable', not_removed_at_plaid=len(problems))
    out = status(request, m)
    if problems:
        out['note'] = ('Turned off. Plaid could not be told for %s; remove the app from your bank\'s connected apps '
                       'if it is still listed.' % ', '.join(problems))
    return out


# ---------------------------------------------------------------- linking

class LinkTokenBody(Body):
    days: int = 90
    item_id: str | None = Field(default=None, max_length=200)    # set = sign in again to that bank


@api.post('/link-token')
def link_token(body: LinkTokenBody, request: Request, m: Owner) -> dict[str, Any]:
    _require_on(m)
    access = None
    if body.item_id:
        it = pdb.get_item(m.conn, m.hid, body.item_id)
        if it is None:
            raise ApiError(404, 'not_found', 'No such bank connection.')
        access = _token(request, m, it)
    s = request.app.state.settings
    tok = _plaid_call(client(request).link_token, user_ref='hbudget-%d-%d' % (m.hid, m.user_id or 0),
                      client_name='Hearthfund', days=body.days, redirect_uri=s.plaid_redirect_uri,
                      access_token=access)
    return {'link_token': tok}


class ExchangeBody(Body):
    public_token: str = Field(min_length=1, max_length=300)
    institution: str = Field(default='', max_length=120)


@api.post('/exchange')
def exchange(body: ExchangeBody, request: Request, m: Owner) -> dict[str, Any]:
    _require_on(m)
    c = client(request)
    access, item_id = _plaid_call(c.exchange, body.public_token)
    accts: list[pl.PlaidAccount] = _plaid_call(c.accounts, access)
    have_bank = any(a.role == 'bank' for a in pdb.accounts(m.conn, m.hid))
    roles = pl.default_roles(accts, have_bank)
    institution = ' '.join(body.institution.split())[:80]
    sealed = pdb.seal(_key(request), access, pdb.aad(m.hid, item_id))
    with transaction(m.conn):
        pdb.add_item(m.conn, m.hid, item_id, institution, sealed, m.auth.user.username)
        pdb.upsert_accounts(m.conn, m.hid, item_id, [(a.account_id, a.name, a.mask, a.type, a.subtype, a.current)
                                                     for a in accts], dict(roles))
        _audit(m, 'plaid.link', accounts=len(accts))
    return status(request, m)


@api.post('/items/{item_id}/relinked')
def relinked(item_id: str, request: Request, m: Owner) -> dict[str, Any]:
    """After signing in again (Link update mode) the token stays the same; clear the error."""
    _require_on(m)
    if pdb.get_item(m.conn, m.hid, item_id) is None:
        raise ApiError(404, 'not_found', 'No such bank connection.')
    pdb.save_sync(m.conn, m.hid, item_id, None, '')
    return status(request, m)


@api.delete('/items/{item_id}')
def unlink(item_id: str, request: Request, m: Owner) -> dict[str, Any]:
    _require_on(m)
    it = pdb.get_item(m.conn, m.hid, item_id)
    if it is None:
        raise ApiError(404, 'not_found', 'No such bank connection.')
    note = ''
    try:
        client(request).remove(pdb.unseal(_key(request), it.token_sealed, pdb.aad(m.hid, it.item_id)))
    except (pl.PlaidError, ValueError):
        note = 'Plaid could not be told; remove the app from your bank\'s connected apps if it is still listed.'
    pdb.remove_item(m.conn, m.hid, item_id)
    _audit(m, 'plaid.unlink')
    out = status(request, m)
    if note:
        out['note'] = note
    return out


class AutoSyncBody(Body):
    value: Literal['off', 'daily']


@api.post('/auto-sync')
def set_auto_sync(body: AutoSyncBody, request: Request, m: Owner) -> dict[str, Any]:
    _require_on(m)
    pdb.set_auto_sync(m.conn, m.hid, body.value)
    _audit(m, 'plaid.auto_sync', value=body.value)
    return status(request, m)


class RoleBody(Body):
    role: Literal['bank', 'card', 'ignore']


@api.post('/accounts/{account_id}/role')
def set_role(account_id: str, body: RoleBody, request: Request, m: Owner) -> dict[str, Any]:
    _require_on(m)
    if not pdb.set_role(m.conn, m.hid, account_id, body.role):
        raise ApiError(404, 'not_found', 'No such account.')
    _audit(m, 'plaid.role', role=body.role)
    return status(request, m)


# ---------------------------------------------------------------- sync

def _money(c: int) -> str:
    return ('-$' if c < 0 else '$') + f'{abs(c) / 100:,.2f}'


def _bank_upload(m: Member, acct: pdb.Account, institution: str) -> dict[str, Any] | None:
    """A waiting bank upload with the account's postings from a week before the last reconciliation, or None
    when nothing in it is new."""
    from hbudget.web.routes.api_reconcile import _upload_json

    b = load(m)
    since = b.last_reconciled_through
    rows = pdb.account_txns(m.conn, m.hid, acct.account_id, since - timedelta(days=WINDOW_DAYS) if since else None)
    with transaction(m.conn):
        ids = pdb.assign_ids(m.conn, m.hid, 'bank_txns', rows)
    seen = repo.seen_txn_ids(m.conn, m.hid)
    fresh = [r for r in rows if ids[r.plaid_id] not in seen and (since is None or r.posted >= since)]
    if not fresh:
        return None
    # an earlier Plaid upload still waiting for review: keep it when it holds exactly these postings, else replace
    # it with one that holds them all (and none the bank withdrew), so days without a check-in, or a sync before
    # the last one was reviewed, still mean one upload to review
    waiting = _waiting_plaid_uploads(m)
    want = {ids[r.plaid_id] for r in rows}
    for held in waiting:
        if {t.id for t in repo.upload_txns(m.conn, m.hid, held.id)} == want:
            return _upload_json(held)
    bf, lined_up = pl.bank_file([(ids[r.plaid_id], r.posted, r.amount, r.description) for r in rows],
                                acct.current, b.start_balance)
    if lined_up:
        note = 'From Plaid. Balance checked: Plaid shows %s.' % _money(acct.current or 0)
    elif acct.current is None:
        note = 'From Plaid. Plaid gave no balance, so postings are matched by date.'
    else:
        note = ('From Plaid. Plaid\'s balance (%s) does not line up with the checkbook start balance (%s): a posting '
                'may be missing or still pending. Postings are matched by date.'
                % (_money(acct.current), _money(b.start_balance)))
    upload_id = new_id('plaid')
    name = clean_filename('Plaid-%s-%s' % (institution or 'bank', acct.mask))
    with transaction(m.conn):
        for old in waiting:
            repo.delete_upload(m.conn, m.hid, old.id, postings=True)
        repo.create_upload(m.conn, m.hid, upload_id=upload_id, kind='bank', filename=name,
                           uploaded_by=m.auth.user.username, rows=len(bf.txns), date_from=bf.first, date_to=bf.last,
                           latest_balance=bf.latest_balance, has_balance=bf.has_balance, note=note)
        new, dup = repo.insert_txns(m.conn, m.hid, upload_id, bf.txns)
        repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='upload.bank', row_id=upload_id,
                   detail={'rows': len(bf.txns), 'new': new, 'duplicates': dup, 'source': 'plaid',
                           'replaced': len(waiting)})
    u = repo.get_upload(m.conn, m.hid, upload_id)
    assert u is not None
    return _upload_json(u)


def _waiting_plaid_uploads(m: Member) -> list[repo.Upload]:
    """Bank uploads a sync made that nobody has reconciled yet."""
    return [u for u in repo.list_uploads(m.conn, m.hid, kind='bank')
            if u.status == 'waiting' and u.id.startswith('plaid-') and u.filename.startswith('Plaid-')]


def _card_upload(m: Member, acct: pdb.Account, institution: str) -> dict[str, Any] | None:
    """An imported card upload with every posting of the account that no upload has taken yet."""
    from hbudget.web.routes.api_reconcile import _upload_json

    rows = pdb.account_txns(m.conn, m.hid, acct.account_id, unassigned=True)
    if not rows:
        return None
    b = load(m)
    with transaction(m.conn):
        ids = pdb.assign_ids(m.conn, m.hid, 'card_txns', rows)
    txns = [CardTxn(id=ids[r.plaid_id], purchased=min(r.authorized or r.posted, r.posted), posted=r.posted,
                    amount=r.amount, description=r.description,
                    category=categorise(r.description, '', b.card_categories, amount=r.amount)) for r in rows]
    upload_id = new_id('plaid')
    name = clean_filename('Plaid-%s-%s' % (institution or 'card', acct.mask))
    with transaction(m.conn):
        repo.create_upload(m.conn, m.hid, upload_id=upload_id, kind='card', filename=name,
                           uploaded_by=m.auth.user.username, rows=len(txns),
                           date_from=min(t.purchased for t in txns), date_to=max(t.posted for t in txns),
                           latest_balance=None, has_balance=False, status='imported',
                           note='From Plaid: card purchases added to the Card tab')
        new, dup = repo.insert_card_txns(m.conn, m.hid, upload_id, txns)
        repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='upload.card', row_id=upload_id,
                   detail={'rows': len(txns), 'new': new, 'duplicates': dup, 'source': 'plaid'})
    u = repo.get_upload(m.conn, m.hid, upload_id)
    assert u is not None
    return _upload_json(u)


def fetch(conn: sqlite3.Connection, hid: int, c: pl.PlaidClient, key: bytes,
          items: Sequence[pdb.Item], now: datetime | None = None) -> dict[str, Any]:
    """Ask Plaid for what changed at each bank and store it: posted transactions, withdrawn ones, balances. Makes
    no upload, so it is safe to run with nobody watching (the daily sync)."""
    out: dict[str, Any] = {'added': 0, 'pending': 0, 'changed': 0, 'removed': 0, 'not_ready': False, 'problems': []}
    for it in items:
        try:
            token = pdb.unseal(key, it.token_sealed, pdb.aad(hid, it.item_id))
        except ValueError as e:
            out['problems'].append({'item_id': it.item_id, 'institution': it.institution, 'message': str(e),
                                    'relink': False})
            continue
        try:
            page = c.sync(token, it.cursor)
            accts = c.accounts(token)
        except pl.PlaidError as e:
            pdb.save_sync(conn, hid, it.item_id, None, e.message, now)
            out['problems'].append({'item_id': it.item_id, 'institution': it.institution, 'message': e.message,
                                    'relink': e.login_required})
            continue
        have_bank = any(a.role == 'bank' for a in pdb.accounts(conn, hid))
        posted = [t for t in page.added if not t.pending]
        with transaction(conn):
            pdb.upsert_accounts(conn, hid, it.item_id, [(a.account_id, a.name, a.mask, a.type, a.subtype, a.current)
                                                        for a in accts], dict(pl.default_roles(accts, have_bank)))
            stored = pdb.store_txns(conn, hid, [(t.plaid_id, t.account_id, t.posted, t.authorized, t.amount,
                                                 t.description) for t in posted])
            out['changed'] += pdb.known(conn, hid, [t.plaid_id for t in page.modified if not t.pending])
            out['removed'] += len(pdb.mark_removed(conn, hid, page.removed))
            pdb.save_sync(conn, hid, it.item_id, page.cursor, '', now)
        out['added'] += len(stored)
        out['pending'] += len(page.added) - len(posted)
        out['not_ready'] = out['not_ready'] or (not page.ready and not posted)
    return out


def bring_in(m: Member) -> dict[str, Any]:
    """Turn the stored postings into uploads: the checking account's waiting bank upload (everything since the last
    reconciliation, however many syncs brought it) and a card upload per card account."""
    institution = {it.item_id: it.institution for it in pdb.items(m.conn, m.hid)}
    out: dict[str, Any] = {'bank': None, 'cards': []}
    for a in pdb.accounts(m.conn, m.hid):
        inst = institution.get(a.item_id, '')
        if a.role == 'bank':
            out['bank'] = _bank_upload(m, a, inst)
        elif a.role == 'card':
            card = _card_upload(m, a, inst)
            if card:
                out['cards'].append(card)
    return out


def waiting_count(conn: sqlite3.Connection, hid: int, since: Any) -> int:
    """Postings already stored (by the daily sync) that no upload has taken yet, for the check-in."""
    n = 0
    for a in pdb.accounts(conn, hid):
        if a.role == 'bank':
            n += len(pdb.account_txns(conn, hid, a.account_id, since, unassigned=True))
        elif a.role == 'card':
            n += len(pdb.account_txns(conn, hid, a.account_id, unassigned=True))
    return n


def run_sync(request: Request, m: Member) -> dict[str, Any]:
    _require_on(m)
    items = pdb.items(m.conn, m.hid)
    if not items:
        raise ApiError(409, 'no_bank', 'No bank is linked yet. Link one on the Bank connection page.')
    out = fetch(m.conn, m.hid, client(request), _key(request), items)
    out.update(bring_in(m))
    _audit(m, 'plaid.sync', added=out['added'], pending=out['pending'], problems=len(out['problems']))
    return out


def auto_sync(app: Any, now: datetime | None = None) -> int:
    """The daily sync: for each household that turned it on, fetch every bank not synced in the last AUTO_EVERY.
    Only stores; the uploads are made when someone syncs in the check-in. Returns how many banks it asked."""
    now = now or datetime.now(UTC)
    conn = connection.connect(app.state.settings.db_path)
    asked = 0
    try:
        for hid in pdb.auto_households(conn):
            due = [it for it in pdb.items(conn, hid) if pdb.claim(conn, hid, it.item_id, now - AUTO_EVERY, now)]
            if not due:
                continue
            out = fetch(conn, hid, app.state.plaid_client, app.state.plaid_key, due, now)
            repo.audit(conn, household_id=hid, user_id=None, action='plaid.autosync',
                       detail={'added': out['added'], 'pending': out['pending'], 'problems': len(out['problems'])})
            asked += len(due)
    finally:
        conn.close()
    return asked


@api.post('/sync')
def sync(request: Request, m: Editor) -> dict[str, Any]:
    return run_sync(request, m)


# ---------------------------------------------------------------- pages

def _link_page(request: Request, conn: Any, auth: Any, m: Member | None, data: dict[str, Any]) -> Response:
    s = request.app.state.settings
    ctx = page_context(request, conn, auth, title='Bank connection', household=m.household if m else None,
                       role=m.role if m else None, tab='connections',
                       nav=nav(m.role, 'connections', plaid=True) if m else None, strip=False,
                       page_data={'csrf': auth.csrf, 'plaid_script': LINK_SCRIPT, **data})
    r: Response = request.app.state.templates.TemplateResponse(request, 'connections.html', ctx)
    r.headers['content-security-policy'] = link_csp(s.plaid)
    r.headers['cross-origin-opener-policy'] = 'same-origin-allow-popups'   # Link opens the bank in a pop-up
    return r


@router.get('/h/{hid}/connections', response_class=HTMLResponse)
def connections_page(request: Request, m: Member = Depends(member('viewer'))) -> Response:  # noqa: B008
    return _link_page(request, m.conn, m.auth, m, {'hid': m.hid, 'role': m.role,
                                                   'household_name': m.household.name})


@router.get('/plaid/oauth', response_class=HTMLResponse)
def oauth_return(request: Request, conn: Conn, auth: CurrentAuth) -> Response:
    """Where an OAuth bank sends the browser back (registered in the Plaid dashboard). The page finishes Link with
    the token it kept in sessionStorage; it shows no household data."""
    return _link_page(request, conn, auth, None, {'oauth': True})
