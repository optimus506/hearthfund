"""The optional Plaid bank connection (docs/PLAID.md). Registered only when HBUDGET_PLAID is not `off`.

A household owner turns it on and links a bank through Plaid Link; a sync stores what Plaid sent (fetch) and turns
it into ordinary uploads (bring_in): one waiting bank upload for the reconciled checking account (reviewed in the
check-in like a file) and an imported card upload for card accounts. Nothing here runs unless a member asks for
it, except the optional daily sync (auto_sync), which only fetches: the uploads are still made when a member syncs.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import Field

from hbudget.core.cards import categorise
from hbudget.core.models import Account, AccountKind, CardTxn
from hbudget.core.payoff import NOT_DEBTS
from hbudget.db import connection, repo
from hbudget.db import plaid as pdb
from hbudget.db.connection import transaction
from hbudget.io import clean_filename
from hbudget.io import plaid as pl
from hbudget.web.deps import ApiError, Conn, CurrentAuth, Editor, Member, Owner, Viewer, member, page_context
from hbudget.web.routes.api_household import Body, edit, load, new_id
from hbudget.web.routes.pages import nav

router = APIRouter()
api = APIRouter(prefix='/api/h/{hid}/plaid')

LINK_SCRIPT = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js'
WINDOW_DAYS = 7          # a bank upload reaches this far behind the last reconciliation, like a weekly export
AUTO_EVERY = timedelta(hours=20)    # the daily sync leaves a bank alone that synced more recently than this
REFRESH_EVERY = timedelta(hours=1)  # an on-demand refresh is paid for per request: one an hour per bank
PENDING_DAYS = 14        # a pending transaction older than this is stale: it no longer explains a balance gap


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

def shown_role(a: pdb.Account) -> str:
    """What the account is for, as the page and the API say it: `invest` when its balance is kept for an
    investment account (stored as tracking, next to the role `ignore`)."""
    return 'invest' if a.tracking else a.role


def status(request: Request, m: Member) -> dict[str, Any]:
    on = pdb.enabled(m.conn, m.hid)
    accts = pdb.accounts(m.conn, m.hid) if on else []
    items = []
    for it in pdb.items(m.conn, m.hid) if on else []:
        items.append({'item_id': it.item_id, 'institution': it.institution, 'linked_at': it.linked_at,
                      'last_sync_at': it.last_sync_at, 'last_error': it.last_error,
                      'liabilities': it.liabilities, 'liabilities_note': it.liabilities_note,
                      'accounts': [{'account_id': a.account_id, 'name': a.name, 'mask': a.mask, 'type': a.type,
                                    'subtype': a.subtype, 'role': shown_role(a), 'current': a.current,
                                    'debt': a.debt, 'terms': a.liability}
                                   for a in accts if a.item_id == it.item_id]})
    s = request.app.state.settings
    debts = [{'id': a.id, 'name': a.name} for a in load(m).accounts if a.kind not in NOT_DEBTS] if items else []
    return {'server': s.plaid, 'enabled': on, 'items': items, 'debts': debts,
            'liabilities': bool(s.plaid_liabilities), 'oauth': s.plaid_redirect_uri is not None,
            'balance': balance(m) if items else None, 'refresh': bool(s.plaid_refresh),
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
    liabilities: bool = False        # with item_id: also ask to allow rates, minimums and due dates (D-106)


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
    if body.liabilities and not (s.plaid_liabilities and access):
        raise ApiError(400, 'invalid', 'Rates, minimums and due dates are not offered on this server '
                                       '(HBUDGET_PLAID_LIABILITIES), or no linked bank was named.')
    tok = _plaid_call(client(request).link_token, user_ref='hbudget-%d-%d' % (m.hid, m.user_id or 0),
                      client_name='Hearthfund', days=body.days, redirect_uri=s.plaid_redirect_uri,
                      access_token=access, consent=('liabilities',) if body.liabilities else ())
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


class LiabilitiesBody(Body):
    on: bool


@api.post('/items/{item_id}/liabilities')
def set_liabilities(item_id: str, body: LiabilitiesBody, request: Request, m: Owner) -> dict[str, Any]:
    """Turn Plaid Liabilities on or off for one linked bank (D-106). On: the page calls this after the bank's
    customer allowed it in Plaid Link, and the first read happens at once, so a bank that does not offer it says
    so now. From then on every sync reads the rate, the minimum payment and the next due date of the bank's cards
    and loans. Off: the app stops asking and forgets what it read. Plaid bills the product per bank and month
    from the first read until the bank is unlinked, also after it is turned off here."""
    _require_on(m)
    it = pdb.get_item(m.conn, m.hid, item_id)
    if it is None:
        raise ApiError(404, 'not_found', 'No such bank connection.')
    s = request.app.state.settings
    if body.on and not s.plaid_liabilities:
        raise ApiError(400, 'invalid', 'Rates, minimums and due dates are not offered on this server '
                                       '(HBUDGET_PLAID_LIABILITIES).')
    pdb.set_liabilities(m.conn, m.hid, item_id, body.on)
    _audit(m, 'plaid.liabilities', on=body.on)
    if body.on:
        read_liabilities(m.conn, m.hid, client(request), _token(request, m, it), item_id, m.today)
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
    role: Literal['bank', 'card', 'invest', 'ignore']


def _track(m: Member, acct: pdb.Account, institution: str) -> None:
    """Keep the linked account's balance for an investment account on Money > Investments (D-102): the one it kept
    before when that still exists, else a new one named after the bank and the account. Only the balance is used;
    none of the account's transactions or holdings are read."""
    with transaction(m.conn, immediate=True):
        has = any(a.id == acct.tracks and a.kind is AccountKind.INVESTMENT for a in load(m).accounts)
        aid = acct.tracks if has else None
        if aid is None:
            name = re.sub(r'\d{6,}', '', ' '.join(('%s %s' % (institution, acct.name)).split()))[:60].strip()
            aid = new_id('inv', name)
            new = Account(aid, name or 'Investment account', AccountKind.INVESTMENT, include_in_payoff=False)
            edit(m, 'investment.add', lambda b: replace(b, accounts=(*b.accounts, new)), aid)
        pdb.set_role(m.conn, m.hid, acct.account_id, 'ignore')
        pdb.set_tracking(m.conn, m.hid, acct.account_id, True, aid)
        if acct.current is not None:
            repo.note_investment_balance(m.conn, m.hid, aid, m.today, acct.current, m.today)


@api.post('/accounts/{account_id}/role')
def set_role(account_id: str, body: RoleBody, request: Request, m: Owner) -> dict[str, Any]:
    _require_on(m)
    acct = next((a for a in pdb.accounts(m.conn, m.hid) if a.account_id == account_id), None)
    if acct is None:
        raise ApiError(404, 'not_found', 'No such account.')
    if body.role == 'invest':
        it = pdb.get_item(m.conn, m.hid, acct.item_id)
        _track(m, acct, it.institution if it else '')
    else:
        with transaction(m.conn):
            pdb.set_role(m.conn, m.hid, account_id, body.role)
            pdb.set_tracking(m.conn, m.hid, account_id, False)     # the investment account and its history stay
    _audit(m, 'plaid.role', role=body.role)
    return status(request, m)


class DebtBody(Body):
    debt: Annotated[str, Field(max_length=120)] | None


@api.post('/accounts/{account_id}/debt')
def set_debt(account_id: str, body: DebtBody, request: Request, m: Owner) -> dict[str, Any]:
    """Let a linked card or loan keep the balance of one debt on the Debts page (D-105): every sync, the daily one
    included, writes what the bank says is owed, with the day as its date. null stops it; the debt keeps its last
    balance. Whatever the account is used for stays as it is."""
    _require_on(m)
    acct = next((a for a in pdb.accounts(m.conn, m.hid) if a.account_id == account_id), None)
    if acct is None:
        raise ApiError(404, 'not_found', 'No such account.')
    if body.debt is not None and not any(a.id == body.debt and a.kind not in NOT_DEBTS for a in load(m).accounts):
        raise ApiError(400, 'invalid', 'No debt with that id.')
    with transaction(m.conn, immediate=True):
        pdb.set_debt(m.conn, m.hid, account_id, body.debt)
        if body.debt is not None and acct.current is not None:
            repo.note_debt_balance(m.conn, m.hid, body.debt, m.today, acct.current)
    _audit(m, 'plaid.debt', debt=body.debt or '')
    return status(request, m)


# ---------------------------------------------------------------- sync

def _money(c: int) -> str:
    return ('-$' if c < 0 else '$') + f'{abs(c) / 100:,.2f}'


def _n(n: int, word: str) -> str:
    return '%d %s%s' % (n, word, '' if n == 1 else 's')


def _the(n: int, word: str) -> str:
    return 'the %s' % word if n == 1 else 'the %s' % _n(n, word)


def gap_text(g: pl.Gap, bank: int, checkbook: int) -> str:
    """One or two plain sentences for a balance difference; empty when the balances agree."""
    if g.reason == 'agree':
        return ''
    diff = '%s %s than the checkbook (%s)' % (_money(abs(g.ahead)), 'more' if g.ahead > 0 else 'less',
                                              _money(checkbook))
    head = 'The bank shows %s, %s. ' % (_money(bank), diff)
    left_out = _n(g.pending, 'pending transaction')
    if g.reason == 'postings':
        what = _the(g.postings, 'newest posting')
        return head + ('%s make%s up the difference: review the upload to bring the checkbook up to date.'
                       % (what.capitalize(), 's' if g.postings == 1 else ''))
    if g.reason == 'pending':
        what = _the(g.pending, 'pending transaction')
        if g.postings:
            what = '%s and %s' % (_the(g.postings, 'newest posting'), what)
        return head + ('%s make%s up the difference. The bank already counts pending transactions; the '
                       'checkbook counts them when they post.'
                       % (what.capitalize(), 's' if g.postings + g.pending == 1 else ''))
    return head + ('The postings Plaid has sent so far do not explain the difference%s. A posting has most likely '
                   'not been sent yet (they usually arrive within a day): sync again later before looking for a '
                   'mistake.' % (', with or without the %s' % left_out if g.pending else ''))


def balance(m: Member, b: Any = None) -> dict[str, Any] | None:
    """The linked checking account's balance at the bank next to the checkbook's, with what explains a difference
    (pl.balance_gap). None when no checking account is linked or Plaid gave no balance. Reads only."""
    acct = next((a for a in pdb.accounts(m.conn, m.hid) if a.role == 'bank'), None)
    if acct is None or acct.current is None:
        return None
    b = b or load(m)
    since = b.last_reconciled_through
    rows = pdb.account_txns(m.conn, m.hid, acct.account_id, since - timedelta(days=WINDOW_DAYS) if since else None)
    rows.sort(key=lambda r: (r.posted, r.plaid_id), reverse=True)
    pending = pdb.pending_amounts(m.conn, m.hid, acct.account_id, m.today - timedelta(days=PENDING_DAYS))
    g = pl.balance_gap(acct.current, b.start_balance, [r.amount for r in rows], pending)
    return {'bank': acct.current, 'checkbook': b.start_balance, 'ahead': g.ahead, 'reason': g.reason,
            'postings': g.postings, 'pending': g.pending, 'pending_total': g.pending_total,
            'message': gap_text(g, acct.current, b.start_balance)}


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


def read_liabilities(conn: sqlite3.Connection, hid: int, c: pl.PlaidClient, token: str, item_id: str,
                     day: date) -> int:
    """Ask one bank for the rate, minimum payment and next due date of its cards and loans (Plaid Liabilities,
    D-106) and keep them per account; a debt whose balance the account keeps takes the rate and the minimum.
    A refusal is kept as a note for the page and never fails the sync. Returns how many debts were updated."""
    try:
        found = c.liabilities(token)
    except pl.PlaidError as e:
        pdb.save_liabilities(conn, hid, item_id, e.liabilities_note)
        return 0
    debts = {a.account_id: a.debt for a in pdb.accounts(conn, hid) if a.item_id == item_id and a.debt}
    data = {x.account_id: {'apr_bp': x.apr_bp, 'minimum': x.minimum, 'next_due': x.next_due.isoformat()
                           if x.next_due else None, 'statement_balance': x.statement_balance, 'as_of': day.isoformat()}
            for x in found}
    pdb.save_liabilities(conn, hid, item_id, '' if found else 'The bank sent no rates, minimums or due dates.', data)
    n = 0
    for x in found:
        if x.account_id in debts and (x.apr_bp is not None or x.minimum is not None):
            repo.note_debt_terms(conn, hid, debts[x.account_id], day, x.apr_bp, x.minimum)
            n += 1
    return n


def fetch(conn: sqlite3.Connection, hid: int, c: pl.PlaidClient, key: bytes,
          items: Sequence[pdb.Item], now: datetime | None = None, today: date | None = None, *,
          liabilities: bool = False) -> dict[str, Any]:
    """Ask Plaid for what changed at each bank and store it: posted transactions, withdrawn ones, balances. Makes
    no upload, so it is safe to run with nobody watching (the daily sync). An account that keeps an investment
    account's balance (D-102) has it noted for `today`: one balance a day, the later sync's wins."""
    out: dict[str, Any] = {'added': 0, 'pending': 0, 'changed': 0, 'removed': 0, 'not_ready': False, 'problems': [],
                           'investments': 0, 'debts': 0, 'terms': 0}
    day = today or (now or datetime.now(UTC)).date()
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
            seen = (*page.added, *page.modified)
            pdb.set_pending(conn, hid, [(t.plaid_id, t.account_id, t.posted, t.amount) for t in seen if t.pending],
                            [*page.removed, *(t.plaid_id for t in seen if not t.pending)],
                            older_than=(now or datetime.now(UTC)).date() - timedelta(days=2 * PENDING_DAYS))
            pdb.save_sync(conn, hid, it.item_id, page.cursor, '', now)
            for a in pdb.accounts(conn, hid):
                if a.item_id != it.item_id or not a.tracking or not a.tracks or a.current is None:
                    continue
                if repo.note_investment_balance(conn, hid, a.tracks, day, a.current, day):
                    out['investments'] += 1
                else:                                   # the investment account was deleted: stop keeping it
                    pdb.set_tracking(conn, hid, a.account_id, False)
            for a in pdb.accounts(conn, hid):
                if a.item_id != it.item_id or not a.debt or a.current is None:
                    continue
                if repo.note_debt_balance(conn, hid, a.debt, day, a.current):
                    out['debts'] += 1
                else:                                   # the debt was deleted: stop keeping it
                    pdb.set_debt(conn, hid, a.account_id, None)
            if liabilities and it.liabilities:
                out['terms'] += read_liabilities(conn, hid, c, token, it.item_id, day)
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


def refresh(conn: sqlite3.Connection, hid: int, c: pl.PlaidClient, key: bytes, items: Sequence[pdb.Item],
            now: datetime | None = None) -> dict[str, Any]:
    """Ask Plaid to fetch from each bank now (its paid on-demand refresh), at most once per REFRESH_EVERY and
    bank. Returns how many banks were asked and, per bank that was not, why. The caller syncs afterwards."""
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {'refreshed': 0, 'refresh_notes': []}

    def note(it: pdb.Item, message: str) -> None:
        out['refresh_notes'].append({'institution': it.institution, 'message': message})
    for it in items:
        if not pdb.claim_refresh(conn, hid, it.item_id, now - REFRESH_EVERY, now):
            note(it, 'Already refreshed in the last hour; this sync reads what Plaid has now.')
            continue
        try:
            c.refresh(pdb.unseal(key, it.token_sealed, pdb.aad(hid, it.item_id)))
        except ValueError:
            pdb.release_refresh(conn, hid, it.item_id, it.last_refresh_at)
            continue                                    # another secret key: the sync reports it
        except pl.PlaidError as e:
            # give the turn back only when Plaid said no; with no answer (network, a bare HTTP error) the request
            # may have been done and charged, so the hour stands rather than risk paying twice
            answered = e.code != 'network' and not e.code.startswith('http_')
            if answered:
                pdb.release_refresh(conn, hid, it.item_id, it.last_refresh_at)
            if not answered:
                note(it, 'The refresh may not have gone through: %s Try again in an hour.' % e.message)
            elif e.refresh_note:
                note(it, e.refresh_note)
            elif not e.login_required:                  # sign in again, or the network: the sync reports those
                note(it, 'The refresh did not work: %s' % e.message)
            continue
        out['refreshed'] += 1
    return out


def run_sync(state: Any, m: Member, *, refresh_first: bool = False) -> dict[str, Any]:
    """Sync every linked bank and bring the postings in. `refresh_first` asks Plaid to fetch from the banks
    before that (only on a server that turned HBUDGET_PLAID_REFRESH on)."""
    _require_on(m)
    items = pdb.items(m.conn, m.hid)
    if not items:
        raise ApiError(409, 'no_bank', 'No bank is linked yet. Link one on the Bank connection page.')
    asked: dict[str, Any] = {}
    if refresh_first:
        if not state.settings.plaid_refresh:
            raise ApiError(409, 'refresh_off', 'Refreshing from the bank on demand is not turned on for this server '
                                               '(HBUDGET_PLAID_REFRESH). A normal sync still works.')
        asked = refresh(m.conn, m.hid, state.plaid_client, state.plaid_key, items)
        _audit(m, 'plaid.refresh', banks=asked['refreshed'], refused=len(asked['refresh_notes']))
    out = fetch(m.conn, m.hid, state.plaid_client, state.plaid_key, items, today=m.today,
                liabilities=state.settings.plaid_liabilities)
    out.update(asked)
    out.update(bring_in(m))
    out['balance'] = balance(m)
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
            out = fetch(conn, hid, app.state.plaid_client, app.state.plaid_key, due, now, app.state.clock(),
                        liabilities=app.state.settings.plaid_liabilities)
            repo.audit(conn, household_id=hid, user_id=None, action='plaid.autosync',
                       detail={'added': out['added'], 'pending': out['pending'], 'problems': len(out['problems'])})
            asked += len(due)
    finally:
        conn.close()
    return asked


def summary(state: Any, m: Member) -> dict[str, Any]:
    """The bank connection for the API and MCP: what is linked, when it last synced, and the balance next to the
    checkbook's. No tokens, no Plaid ids."""
    on = pdb.enabled(m.conn, m.hid)
    accts = pdb.accounts(m.conn, m.hid) if on else []
    banks = [{'institution': it.institution, 'last_sync_at': it.last_sync_at, 'problem': it.last_error or None,
              'accounts': [{'name': a.name, 'mask': a.mask, 'used_for': shown_role(a), 'balance': a.current,
                            'keeps_debt': a.debt}
                           for a in accts if a.item_id == it.item_id]}
             for it in (pdb.items(m.conn, m.hid) if on else [])]
    b = load(m) if banks else None
    return {'available': True, 'enabled': on, 'banks': banks, 'can_refresh': bool(state.settings.plaid_refresh),
            'auto_sync': pdb.auto_sync(m.conn, m.hid) if on else 'off',
            'reconciled_through': b.last_reconciled_through.isoformat() if b and b.last_reconciled_through else None,
            'waiting_postings': waiting_count(m.conn, m.hid, b.last_reconciled_through) if b else 0,
            'balance': balance(m, b) if b else None}


@api.post('/sync')
def sync(request: Request, m: Editor) -> dict[str, Any]:
    return run_sync(request.app.state, m)


@api.post('/refresh')
def refresh_now(request: Request, m: Editor) -> dict[str, Any]:
    """"Refresh from the bank now": Plaid fetches from the bank, then the normal sync runs."""
    return run_sync(request.app.state, m, refresh_first=True)


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
