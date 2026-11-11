"""Plaid: the only module that talks to the network, and only when the server enables it (docs/PLAID.md).

Nothing imports this module while HBUDGET_PLAID is off. The client posts JSON to one fixed host, the one for the
configured environment, with certificate checks and no redirects. Everything after the HTTP call is pure: Plaid's
transactions become `PlaidTxn` values in the bank's sign, and `bank_file` turns an account's postings into the
same `BankFile` a CSV or OFX upload gives, so reconciliation is unchanged.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from hbudget.core.models import BankTxn, Cents
from hbudget.core.textmask import mask

from .bank import BankFile

HOSTS = {'sandbox': 'sandbox.plaid.com', 'production': 'production.plaid.com'}
API_VERSION = '2020-09-14'
TIMEOUT = 30
MAX_PAGES = 100                      # 100 x 500 transactions: far more than two years of one household
DAYS_CHOICES = (90, 180, 365, 730)
MAX_TEXT = 200

Role = Literal['bank', 'card', 'ignore']
# (url, body) -> (HTTP status, response body); tests pass a fake one, so the suite never opens a socket
Transport = Callable[[str, bytes], tuple[int, bytes]]


class PlaidError(Exception):
    """A Plaid request failed. `code` is Plaid's error_code (or `network`); `message` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def login_required(self) -> bool:
        return self.code in ('ITEM_LOGIN_REQUIRED', 'PENDING_EXPIRATION', 'PENDING_DISCONNECT')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:   # a redirect off the Plaid host is refused
        return None


def _https(url: str, body: bytes) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(
        context=ssl.create_default_context()))
    req = urllib.request.Request(url, data=body, method='POST', headers={  # noqa: S310 - fixed https host
        'Content-Type': 'application/json', 'Plaid-Version': API_VERSION, 'User-Agent': 'hearthfund'})
    try:
        with opener.open(req, timeout=TIMEOUT) as res:
            return int(res.status), res.read()
    except urllib.error.HTTPError as e:
        return int(e.code), e.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PlaidError('network', 'Could not reach Plaid. Check the server\'s internet connection.') from None


# ---------------------------------------------------------------- values

@dataclass(frozen=True, slots=True)
class PlaidAccount:
    account_id: str
    name: str
    mask: str
    type: str                        # depository, credit, loan, investment, other
    subtype: str
    current: Cents | None            # Plaid's current balance (for credit: what is owed)


@dataclass(frozen=True, slots=True)
class PlaidTxn:
    plaid_id: str
    account_id: str
    posted: date
    authorized: date | None
    amount: Cents                    # BANK sign: - out, + in (Plaid's own sign is the opposite)
    description: str                 # masked
    pending: bool


@dataclass(frozen=True, slots=True)
class SyncPage:
    added: tuple[PlaidTxn, ...]
    modified: tuple[PlaidTxn, ...]
    removed: tuple[str, ...]         # Plaid transaction ids
    cursor: str
    ready: bool                      # False while Plaid is still fetching the first history


def cents(v: Any) -> Cents:
    """Plaid's decimal amount (parsed as Decimal) -> integer cents."""
    return int((Decimal(str(v)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def parse_txn(raw: Mapping[str, Any]) -> PlaidTxn | None:
    """One Plaid transaction; None when it lacks an id, account, date or amount."""
    posted = _date(raw.get('date'))
    if not raw.get('transaction_id') or not raw.get('account_id') or posted is None or raw.get('amount') is None:
        return None
    text = str(raw.get('original_description') or raw.get('name') or raw.get('merchant_name') or '')
    return PlaidTxn(plaid_id=str(raw['transaction_id']), account_id=str(raw['account_id']), posted=posted,
                    authorized=_date(raw.get('authorized_date')), amount=-cents(raw['amount']),
                    description=mask(' '.join(text.split())[:MAX_TEXT]), pending=bool(raw.get('pending')))


def parse_account(raw: Mapping[str, Any]) -> PlaidAccount:
    cur = (raw.get('balances') or {}).get('current')
    return PlaidAccount(account_id=str(raw['account_id']), name=str(raw.get('name') or '')[:80],
                        mask=str(raw.get('mask') or '')[:8], type=str(raw.get('type') or ''),
                        subtype=str(raw.get('subtype') or ''), current=None if cur is None else cents(cur))


def default_roles(accounts: Sequence[PlaidAccount], have_bank: bool = False) -> dict[str, Role]:
    """The first checking account is reconciled (unless one already is), credit cards go to the Card page, the rest
    is ignored. The household changes these on the Bank connection page."""
    out: dict[str, Role] = {}
    for a in accounts:
        if a.type == 'depository' and a.subtype == 'checking' and not have_bank:
            out[a.account_id] = 'bank'
            have_bank = True
        elif a.type == 'credit':
            out[a.account_id] = 'card'
        else:
            out[a.account_id] = 'ignore'
    return out


# ---------------------------------------------------------------- pure: postings -> upload

def bank_file(txns: Sequence[tuple[str, date, Cents, str]], current: Cents | None,
              start_balance: Cents) -> tuple[BankFile, bool]:
    """An account's postings, (txn id, posted, amount, description) for the window to reconcile, as a BankFile.

    Plaid sends no running balance, so it is worked back from the account's current balance. It is kept only when
    the app's start balance appears in it, so reconcile can check the chain and find its basis as with a bank file;
    otherwise the file has no balances and reconcile uses the date and the transactions already seen. The flag
    says whether the balance lined up."""
    ordered = sorted(txns, key=lambda t: (t[1], t[0]), reverse=True)        # newest first, stable within a day
    balances: list[Cents | None] = [None] * len(ordered)
    lined_up = False
    if current is not None and ordered:
        run = current
        for i, (_, _, amount, _) in enumerate(ordered):
            balances[i] = run
            run -= amount
        lined_up = start_balance in balances      # the basis must be inside the window, as with a bank file
    keep = lined_up
    out = tuple(BankTxn(id=tid, posted=posted, amount=amount, description=desc,
                        balance=balances[i] if keep else None, file_index=i)
                for i, (tid, posted, amount, desc) in enumerate(ordered))
    first = min((t.posted for t in out), default=date.min)
    last = max((t.posted for t in out), default=date.min)
    return BankFile(txns=out, ignored=0, duplicates=0, first=first, last=last,
                    latest_balance=current if keep else None, has_balance=keep), keep


# ---------------------------------------------------------------- client

class PlaidClient:
    def __init__(self, env: str, client_id: str, secret: str, *, transport: Transport | None = None) -> None:
        if env not in HOSTS:
            raise ValueError('unknown Plaid environment')
        self.env = env
        self._base = 'https://' + HOSTS[env]
        self._auth = {'client_id': client_id, 'secret': secret}
        self._send = transport or _https

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        status, raw = self._send(self._base + path, json.dumps({**self._auth, **body}).encode())
        try:
            data = json.loads(raw.decode('utf-8'), parse_float=Decimal) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        if status != 200 or not isinstance(data, dict):
            d = data if isinstance(data, dict) else {}
            msg = d.get('display_message') or d.get('error_message') or 'Plaid answered with an error (%d).' % status
            raise PlaidError(str(d.get('error_code') or 'http_%d' % status), str(msg)[:300])
        return data

    def link_token(self, *, user_ref: str, client_name: str, days: int, redirect_uri: str | None,
                   access_token: str | None = None) -> str:
        """A token for Plaid Link: a new connection, or (with access_token) signing in again to an existing one."""
        body: dict[str, Any] = {'client_name': client_name[:30] or 'Hearthfund', 'language': 'en',
                                'country_codes': ['US'], 'user': {'client_user_id': user_ref}}
        if access_token:
            body['access_token'] = access_token
        else:
            body['products'] = ['transactions']
            body['transactions'] = {'days_requested': days if days in DAYS_CHOICES else 90}
        if redirect_uri:
            body['redirect_uri'] = redirect_uri
        return str(self._post('/link/token/create', body)['link_token'])

    def exchange(self, public_token: str) -> tuple[str, str]:
        d = self._post('/item/public_token/exchange', {'public_token': public_token})
        return str(d['access_token']), str(d['item_id'])

    def accounts(self, access_token: str) -> list[PlaidAccount]:
        d = self._post('/accounts/get', {'access_token': access_token})
        return [parse_account(a) for a in d.get('accounts') or []]

    def sync(self, access_token: str, cursor: str | None) -> SyncPage:
        """Every change since `cursor`, all pages. A change during paging restarts from `cursor` (Plaid's advice)."""
        for _ in range(3):
            added: list[PlaidTxn] = []
            modified: list[PlaidTxn] = []
            removed: list[str] = []
            cur = cursor or ''
            ready = True
            try:
                for _ in range(MAX_PAGES):
                    body: dict[str, Any] = {'access_token': access_token, 'count': 500,
                                            'options': {'include_original_description': True}}
                    if cur:
                        body['cursor'] = cur
                    d = self._post('/transactions/sync', body)
                    added += [t for t in map(parse_txn, d.get('added') or []) if t]
                    modified += [t for t in map(parse_txn, d.get('modified') or []) if t]
                    removed += [str(r['transaction_id']) for r in d.get('removed') or [] if r.get('transaction_id')]
                    cur = str(d.get('next_cursor') or cur)
                    ready = d.get('transactions_update_status', 'HISTORICAL_UPDATE_COMPLETE') != 'NOT_READY'
                    if not d.get('has_more'):
                        return SyncPage(tuple(added), tuple(modified), tuple(removed), cur, ready)
                raise PlaidError('too_many_pages', 'Plaid sent more pages than expected. Try again.')
            except PlaidError as e:
                if e.code != 'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION':
                    raise
        raise PlaidError('busy', 'Plaid was updating while the app read it. Try again in a minute.')

    def remove(self, access_token: str) -> None:
        self._post('/item/remove', {'access_token': access_token})
