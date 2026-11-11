"""Subscriptions and other recurring charges, found in the bank and card history (issue #34, D-084, D-088).

Nothing is stored: every call looks at the history as it is. A charge is money out to one payee (the bank's
`alerts.payee_key`, the card's `cards.store`). Charges of the same price (within 1%, so a 1-cent change is no price
change) whose gaps are a month (25 to 35 days, or 55 to 70 when one month was skipped) or a year (350 to 380 days)
form a run; a run that picks up a period (or two, for monthly) after another run of the same payee ended, at a
different price, is the same subscription at a new price. A subscription needs at least two charges of one price,
three when the payee is also paid other amounts (a restaurant visited now and then), so a store visited every month
for varying sums is never one.

A bank payee whose charges match a **bill** payee rule is a bill (or loan): any new price continues it, however
far it moved, and it is counted apart from the subscriptions. Payees that stand for many merchants (PayPal, Square,
Amazon, ...) are `generic`: their prices are never joined, since two amounts may be two services. A payee the
household marks "not a subscription" (`Settings.not_recurring`, by source and payee) is listed apart and left out
of the totals. A charge that missed its date while the payee kept charging other amounts is `irregular`, not
stopped.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from itertools import pairwise
from typing import Literal

from .alerts import payee_key
from .cards import store
from .dates import add_months
from .models import BankTxn, Budget, CardTxn, RuleKind
from .money import Cents
from .rules import classify

__all__ = ['Recurring', 'RecurringSummary', 'detect', 'is_generic']

Cadence = Literal['monthly', 'yearly']
Status = Literal['active', 'new', 'stopped', 'irregular']
Group = Literal['bill', 'subscription']
GAPS: dict[Cadence, tuple[int, int]] = {'monthly': (25, 35), 'yearly': (350, 380)}
SKIPPED: dict[Cadence, tuple[int, int] | None] = {'monthly': (55, 70), 'yearly': None}   # one period missed
STEP: dict[Cadence, int] = {'monthly': 1, 'yearly': 12}
GRACE: dict[Cadence, int] = {'monthly': 10, 'yearly': 30}   # days past the expected date before "stopped"
NEW_DAYS = 70              # first charged this recently: new
STOPPED_SHOWN_DAYS = 180   # a stopped charge is listed this long after it was expected
SAME_PRICE_BP = 100        # prices within 1% are one price
# bank lines that are not charges: pay, card payments, transfers and what the household ignores
NOT_CHARGES = frozenset({RuleKind.PAY, RuleKind.CARD, RuleKind.IGNORE, RuleKind.SAVINGS_DRAW, RuleKind.LOAN_IN})
# payees that stand for many merchants: a payment service or a marketplace
GENERIC = ('PAYPAL', 'VENMO', 'SQ ', 'SQ*', 'SQUARE', 'GOOGLE', 'APPLE.COM', 'APPLE COM', 'AMAZON', 'AMZN', 'ZELLE',
           'CASH APP', 'STRIPE')


@dataclass(frozen=True, slots=True)
class Recurring:
    id: str
    payee: str
    source: Literal['bank', 'card']
    cadence: Cadence
    amount: Cents                                   # the latest charge (positive)
    monthly: Cents                                  # a yearly charge spread over 12 months
    first: date
    last: date
    next: date                                      # when the next charge is expected
    count: int
    status: Status
    price_changes: tuple[tuple[date, Cents, Cents], ...] = ()   # (from, old, new)
    rule_id: str | None = None                      # the payee rule its last bank charge matched
    rule_name: str | None = None
    targets: tuple[str, ...] = ()                   # what that rule clears: series ids or one-off row names
    category: str | None = None                     # a card charge's category
    group: Group = 'subscription'                   # 'bill': a bank payee a bill rule matches (bills and loans)
    generic: bool = False                           # a payment service or marketplace: may be several services
    latest: tuple[date, Cents] | None = None        # irregular: the payee's latest charge, at another price


@dataclass(frozen=True, slots=True)
class RecurringSummary:
    items: tuple[Recurring, ...]                   # live ones first (biggest monthly cost first), then stopped
    monthly_total: Cents                            # live ones (active, new, irregular), yearly ones over 12 months
    yearly_total: Cents                             # the same for a whole year
    price_rises: int = 0                            # live ones that cost more than when they started
    hidden: tuple[Recurring, ...] = ()              # ones the household marked "not a subscription" (no totals)
    bills_monthly: Cents = 0                        # the part of monthly_total that is bills and loans
    subscriptions_monthly: Cents = 0                # the rest


@dataclass(slots=True)
class _Run:
    amount: Cents                                   # its latest charge
    dates: list[date]
    cadence: Cadence | None = None


def is_generic(payee: str) -> bool:
    return payee.startswith(GENERIC)


def _fits(gap: int, cadence: Cadence, *, skip: bool = False) -> bool:
    lo, hi = GAPS[cadence]
    if lo <= gap <= hi:
        return True
    s = SKIPPED[cadence]
    return skip and s is not None and s[0] <= gap <= s[1]


def _cadence_of(gap: int) -> Cadence | None:
    return next((c for c in GAPS if _fits(gap, c)), None)


def _same_price(a: Cents, b: Cents) -> bool:
    return abs(a - b) * 10000 <= SAME_PRICE_BP * max(a, b)


def _runs(charges: Sequence[tuple[date, Cents]]) -> list[_Run]:
    """Charges of one price split into runs whose gaps are all one cadence (a skipped month allowed once the
    cadence is known); singles are runs of one."""
    prices: list[Cents] = []                        # one representative per price
    by_price: dict[Cents, list[tuple[date, Cents]]] = defaultdict(list)
    for d, a in sorted(charges, key=lambda x: x[1]):
        p = next((p for p in prices if _same_price(p, a)), None)
        if p is None:
            prices.append(a)
            p = a
        by_price[p].append((d, a))
    out: list[_Run] = []
    for group in by_price.values():
        run: _Run | None = None
        for d, a in sorted(group):
            if run is not None:
                gap = (d - run.dates[-1]).days
                c = run.cadence or _cadence_of(gap)
                if c is not None and _fits(gap, c, skip=run.cadence is not None):
                    run.cadence = c
                    run.dates.append(d)
                    run.amount = a
                    continue
                out.append(run)
            run = _Run(a, [d])
        if run is not None:
            out.append(run)
    return out


def _chains(runs: list[_Run], *, loose: bool, generic: bool) -> list[list[_Run]]:
    """Runs joined into subscriptions: a run that starts one period (or a skipped month) after another one ended,
    at another price, is its new price; at most twice or half as much, unless `loose` (a bill). A chain starts with
    a run of two or more charges. Nothing is joined for a `generic` payee."""
    chains: list[list[_Run]] = []
    for r in sorted(runs, key=lambda x: (x.dates[0], x.amount)):
        best = None
        for ch in chains if not generic else ():
            last = ch[-1]
            if last.cadence is None or _same_price(last.amount, r.amount):
                continue
            if r.cadence not in (None, last.cadence) or not _fits((r.dates[0] - last.dates[-1]).days, last.cadence,
                                                                  skip=True):
                continue
            if not loose and not (last.amount <= 2 * r.amount and r.amount <= 2 * last.amount):
                continue
            if best is None or abs(last.amount - r.amount) < abs(best[-1].amount - r.amount):
                best = ch
        if best is not None:
            r.cadence = r.cadence or best[-1].cadence
            best.append(r)
        else:
            chains.append([r])
    return [ch for ch in chains if ch[0].cadence is not None]


def _id(source: str, payee: str, first: date, amount: Cents) -> str:
    return 'rc-' + hashlib.sha256(('%s|%s|%s|%d' % (source, payee, first, amount)).encode()).hexdigest()[:12]


def _make(source: Literal['bank', 'card'], payee: str, chain: list[_Run], stray: Sequence[tuple[date, Cents]],
          today: date) -> Recurring | None:
    """One subscription; `stray` are the payee's charges in no chain (they keep a late one `irregular`)."""
    cadence = chain[0].cadence
    assert cadence is not None
    dates = sorted(d for r in chain for d in r.dates)
    first, last = dates[0], dates[-1]
    nxt = add_months(last, STEP[cadence])
    latest = None
    if today > nxt + timedelta(days=GRACE[cadence]):
        later = max(((d, a) for d, a in stray if d > last), default=None)
        if later is not None and (today - later[0]).days <= GAPS[cadence][1] + GRACE[cadence]:
            status: Status = 'irregular'                  # missed its date, but the payee still charges
            latest = later
        elif (today - nxt).days > STOPPED_SHOWN_DAYS:
            return None
        else:
            status = 'stopped'
    else:
        status = 'new' if cadence == 'monthly' and (today - first).days <= NEW_DAYS else 'active'
    amount = chain[-1].amount
    changes = tuple((b.dates[0], a.amount, b.amount) for a, b in pairwise(chain))
    monthly = amount if cadence == 'monthly' else (amount + 6) // 12
    return Recurring(_id(source, payee, first, chain[0].amount), payee, source, cadence, amount, monthly, first, last,
                     nxt, len(dates), status, changes, generic=is_generic(payee), latest=latest)


def _found(source: Literal['bank', 'card'], payee: str, charges: list[tuple[date, Cents]], today: date, *,
           loose: bool) -> list[Recurring]:
    """The payee's subscriptions: chains with two charges, or three when the payee is also paid other amounts."""
    chains = _chains(_runs(charges), loose=loose, generic=is_generic(payee))
    left: dict[tuple[date, Cents], int] = defaultdict(int)   # (date, amount) -> charges in no chain
    for c in charges:
        left[c] += 1
    for ch in chains:
        for r in ch:
            for d in r.dates:
                k = next(k for k in left if k[0] == d and left[k] and _same_price(k[1], r.amount))
                left[k] -= 1
    stray = [k for k, n in left.items() for _ in range(n)]
    need = 2 if loose or not stray else 3
    out = []
    for ch in chains:
        if sum(len(r.dates) for r in ch) < need:
            continue
        item = _make(source, payee, ch, stray, today)
        if item is not None:
            out.append(item)
    return out


def detect(history: Sequence[BankTxn], card_txns: Sequence[CardTxn], budget: Budget, *,
           today: date) -> RecurringSummary:
    """Recurring charges in the bank history (not pay, card payments, transfers or ignored lines) and the card
    purchases, with their price changes and whether they stopped."""
    bank: dict[str, list[BankTxn]] = defaultdict(list)
    is_bill: dict[str, bool] = defaultdict(bool)
    for t in history:
        if t.amount >= 0 or t.posted > today:
            continue
        rule = classify(t, budget.rules)
        if rule is not None and rule.kind in NOT_CHARGES:
            continue
        key = payee_key(t.description)
        if key:
            bank[key].append(t)
            is_bill[key] |= rule is not None and rule.kind is RuleKind.BILL
    card: dict[str, list[CardTxn]] = defaultdict(list)
    for c in card_txns:
        if c.amount < 0 and c.purchased <= today:
            card[store(c.description)].append(c)
    items: list[Recurring] = []
    for payee, ts in bank.items():
        for item in _found('bank', payee, [(t.posted, -t.amount) for t in ts], today, loose=is_bill[payee]):
            last = max((t for t in ts if t.posted == item.last and -t.amount == item.amount), key=lambda t: t.id)
            rule = classify(last, budget.rules)
            if rule is not None:
                item = replace(item, rule_id=rule.id, rule_name=rule.name, targets=rule.targets)
            items.append(replace(item, group='bill') if is_bill[payee] else item)
    for payee, cs in card.items():
        for item in _found('card', payee, [(c.purchased, -c.amount) for c in cs], today, loose=False):
            last_c = max((c for c in cs if c.purchased == item.last and -c.amount == item.amount), key=lambda c: c.id)
            items.append(replace(item, category=last_c.category or 'Other'))
    off = set(budget.settings.not_recurring)
    hidden = sorted((i for i in items if (i.source, i.payee) in off), key=lambda i: (i.payee, i.first))
    items = [i for i in items if (i.source, i.payee) not in off]
    live = [i for i in items if i.status != 'stopped']
    order = sorted(live, key=lambda i: (-i.monthly, i.payee)) + sorted(
        (i for i in items if i.status == 'stopped'), key=lambda i: (i.next, i.payee), reverse=True)
    monthly = sum(i.monthly for i in live)
    bills = sum(i.monthly for i in live if i.group == 'bill')
    yearly = sum(i.amount * (12 if i.cadence == 'monthly' else 1) for i in live)
    rises = sum(1 for i in live if i.price_changes and i.amount > i.price_changes[0][1])
    return RecurringSummary(tuple(order), monthly, yearly, rises, tuple(hidden), bills, monthly - bills)
