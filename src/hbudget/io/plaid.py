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

    @property
    def refresh_note(self) -> str | None:
        """Plain words when an on-demand refresh is refused for a reason the household cannot fix by retrying."""
        if self.code in ('PRODUCT_NOT_ENABLED', 'PRODUCTS_NOT_ENABLED', 'INVALID_PRODUCT', 'PRODUCT_NOT_READY',
                         'UNAUTHORIZED_ROUTE_ACCESS', 'PRODUCT_UNAVAILABLE'):
            return ('Plaid\'s on-demand refresh is not part of this Plaid account. It is an add-on: request it in '
                    'the Plaid dashboard, or turn HBUDGET_PLAID_REFRESH off.')
        if self.code in ('PRODUCTS_NOT_SUPPORTED', 'NO_ACCOUNTS', 'INSTITUTION_NOT_SUPPORTED'):
            return 'This bank does not support an on-demand refresh. A normal sync still works.'
        if self.code in ('TRANSACTIONS_REFRESH_LIMIT', 'RATE_LIMIT_EXCEEDED', 'RATE_LIMIT'):
            return 'Plaid was asked to refresh this bank too often. Try again later.'
        return None

    @property
    def liabilities_note(self) -> str:
        """Plain words for why a bank's rates, minimums and due dates could not be read (Plaid Liabilities)."""
        if self.code in ('PRODUCTS_NOT_SUPPORTED', 'NO_LIABILITY_ACCOUNTS', 'INSTITUTION_NOT_SUPPORTED'):
            return 'This bank does not give rates, minimum payments or due dates through Plaid.'
        if self.code in ('ADDITIONAL_CONSENT_REQUIRED', 'ACCESS_NOT_GRANTED', 'INVALID_ACCESS_TOKEN'):
            return 'The bank has not been asked for this yet, or it was not allowed. Turn it on again to allow it.'
        if self.code in ('PRODUCT_NOT_ENABLED', 'PRODUCTS_NOT_ENABLED', 'INVALID_PRODUCT', 'UNAUTHORIZED_ROUTE_ACCESS',
                         'PRODUCT_UNAVAILABLE'):
            return ('Liabilities is not part of this Plaid account. Request it in the Plaid dashboard, or turn '
                    'HBUDGET_PLAID_LIABILITIES off.')
        if self.code == 'PRODUCT_NOT_READY':
            return 'Plaid is still fetching this from the bank. It comes with a later sync.'
        return self.message


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
class Liability:
    """What Plaid Liabilities says about one card or loan. Everything may be missing: banks send what they have."""
    account_id: str
    apr_bp: int | None = None            # a card's purchase rate, a loan's interest rate
    minimum: Cents | None = None         # the minimum (a mortgage: the next monthly) payment
    next_due: date | None = None
    statement_balance: Cents | None = None


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


def _bp(v: Any) -> int | None:
    try:
        bp = round(Decimal(str(v)) * 100)
    except (ArithmeticError, ValueError, TypeError):
        return None
    return bp if 0 <= bp <= 9999 else None


def _opt_cents(v: Any) -> Cents | None:
    try:
        return None if v is None else max(cents(v), 0)
    except (ArithmeticError, ValueError, TypeError):
        return None


def parse_liabilities(raw: Mapping[str, Any]) -> list[Liability]:
    """The `liabilities` object of /liabilities/get: cards, mortgages and student loans. Account numbers, addresses
    and the rest of what Plaid sends are not read."""
    out: list[Liability] = []
    for c in raw.get('credit') or []:
        if not c.get('account_id'):
            continue
        aprs = [a for a in c.get('aprs') or [] if isinstance(a, Mapping)]
        buy = next((a for a in aprs if a.get('apr_type') == 'purchase_apr'), None)
        out.append(Liability(str(c['account_id']), _bp(buy.get('apr_percentage')) if buy else None,
                             _opt_cents(c.get('minimum_payment_amount')), _date(c.get('next_payment_due_date')),
                             _opt_cents(c.get('last_statement_balance'))))
    for m in raw.get('mortgage') or []:
        if m.get('account_id'):
            rate = m.get('interest_rate') if isinstance(m.get('interest_rate'), Mapping) else {}
            out.append(Liability(str(m['account_id']), _bp(rate.get('percentage')),
                                 _opt_cents(m.get('next_monthly_payment')), _date(m.get('next_payment_due_date'))))
    for s in raw.get('student') or []:
        if s.get('account_id'):
            out.append(Liability(str(s['account_id']), _bp(s.get('interest_rate_percentage')),
                                 _opt_cents(s.get('minimum_payment_amount')), _date(s.get('next_payment_due_date')),
                                 _opt_cents(s.get('last_statement_balance'))))
    return out


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


@dataclass(frozen=True, slots=True)
class Gap:
    """The bank's balance next to the checkbook's, and what explains a difference."""
    ahead: Cents                     # bank balance minus checkbook balance
    reason: str                      # agree | postings | pending | unexplained
    postings: int = 0                # the newest N stored postings are part of the explanation
    pending: int = 0                 # pending transactions known for the account
    pending_total: Cents = 0


def balance_gap(current: Cents, checkbook: Cents, amounts: Sequence[Cents], pending: Sequence[Cents] = ()) -> Gap:
    """Why Plaid's balance differs from the checkbook's. `amounts` are the stored postings of the window, newest
    first (as bank_file orders them); `pending` the amounts of pending transactions.

    - agree: the balances are equal.
    - postings: the newest N postings lead from the checkbook balance to the bank's, so reviewing them closes it.
    - pending: the bank's balance already counts the pending transactions; without them (and after the newest N
      postings, if any) it meets the checkbook.
    - unexplained: neither; Plaid has most likely not sent a posting yet.
    """
    ahead, total = current - checkbook, sum(pending)
    if ahead == 0:
        return Gap(0, 'agree', pending=len(pending), pending_total=total)

    def reach(start: Cents) -> int | None:
        run = start
        for n, amount in enumerate(amounts):
            if run == checkbook:
                return n
            run -= amount
        return len(amounts) if run == checkbook else None
    n = reach(current)
    if n is not None:
        return Gap(ahead, 'postings', n, len(pending), total)
    n = reach(current - total) if total else None
    if n is not None:
        return Gap(ahead, 'pending', n, len(pending), total)
    return Gap(ahead, 'unexplained', 0, len(pending), total)


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
                   access_token: str | None = None, consent: Sequence[str] = ()) -> str:
        """A token for Plaid Link: a new connection, or (with access_token) signing in again to an existing one.
        `consent` names further products to ask the bank's customer to allow on that existing connection (Link's
        update mode, `additional_consented_products`); nothing is asked for on a new connection."""
        body: dict[str, Any] = {'client_name': client_name[:30] or 'Hearthfund', 'language': 'en',
                                'country_codes': ['US'], 'user': {'client_user_id': user_ref}}
        if access_token:
            body['access_token'] = access_token
            if consent:
                body['additional_consented_products'] = list(consent)
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

    def liabilities(self, access_token: str) -> list[Liability]:
        """Rates, minimum payments and due dates of the bank's cards and loans (/liabilities/get, a paid product
        billed per linked bank and month once it is used)."""
        d = self._post('/liabilities/get', {'access_token': access_token})
        raw = d.get('liabilities')
        return parse_liabilities(raw) if isinstance(raw, Mapping) else []

    def refresh(self, access_token: str) -> None:
        """Ask Plaid to fetch from the bank now (/transactions/refresh, a paid add-on). It answers when the fetch is
        done, usually within ten seconds; the new postings then come with the next sync."""
        self._post('/transactions/refresh', {'access_token': access_token})

    def remove(self, access_token: str) -> None:
        self._post('/item/remove', {'access_token': access_token})
