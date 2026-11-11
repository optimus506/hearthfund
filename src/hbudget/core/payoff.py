"""Debts and the payoff plan (ARCHITECTURE section 10, D-013)."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from functools import partial
from typing import Literal

from .dates import add_months
from .models import Account, AccountKind, BankTxn, Budget
from .money import Cents

__all__ = ['DebtOutcome', 'PayoffPlan', 'PayoffResult', 'PromoVerdict', 'monthly_interest', 'plan', 'simulate',
           'utilization']

Method = Literal['avalanche', 'snowball']
NOT_DEBTS = (AccountKind.CHECKING, AccountKind.SAVINGS)


def _div_half_up(n: int, d: int) -> int:
    """n / d rounded half up, for n >= 0 and d > 0."""
    return (2 * n + d) // (2 * d)


def utilization(acct: Account) -> int | None:
    """Balance as basis points of the credit limit (None without a limit)."""
    if not acct.limit:
        return None
    return _div_half_up(max(0, acct.balance) * 10000, acct.limit)


def monthly_interest(acct: Account) -> Cents:
    """One month of interest at the current APR (the promo APR while a promo runs)."""
    return _div_half_up(max(0, acct.balance) * acct.apr_bp, 120000)


@dataclass(frozen=True, slots=True)
class DebtOutcome:
    id: str
    name: str
    paid_month: int | None
    paid_on: date | None
    interest: Cents
    balance_at_promo: Cents | None
    promo_ends: date | None
    deferred_interest: bool


@dataclass(frozen=True, slots=True)
class PayoffResult:
    months: int
    done: bool
    total_interest: Cents
    debts: tuple[DebtOutcome, ...]
    balances: tuple[Cents, ...] = ()     # the total owed at the start, then after each month


@dataclass(frozen=True, slots=True)
class PromoVerdict:
    debt_id: str
    name: str
    promo_ends: date
    paid_before: bool
    left_at_promo: Cents
    monthly_needed: Cents
    payments_left: int
    deferred_interest: bool
    apr_after_missing: bool


@dataclass(frozen=True, slots=True)
class PayoffPlan:
    method: Method
    result: PayoffResult
    other: PayoffResult
    baseline: PayoffResult
    start: date
    debt_free_on: date | None
    interest_saved: Cents
    months_sooner: int | None
    other_difference: Cents
    promos: tuple[PromoVerdict, ...]
    not_covering: tuple[str, ...]
    excluded: tuple[str, ...]
    other_debt_free_on: date | None = None     # the same plan with the other method


@dataclass(slots=True)
class _Debt:
    acct: Account
    bal: Cents
    apr: int
    interest: Cents = 0
    paid_month: int | None = None
    bal_at_promo: Cents | None = None


def _priority(d: _Debt, md: date, method: Method) -> tuple[int, int, int, str]:
    """Extra money goes first to a deferred-interest promo still running, then by the method (D-013)."""
    promo = d.acct.promo_ends
    urgent = 1 if d.acct.deferred_interest and promo and md < promo else 0
    if method == 'avalanche':
        return (-urgent, -d.apr, d.bal, d.acct.name)
    return (-urgent, d.bal, -d.apr, d.acct.name)


def simulate(debts: Sequence[Account], *, method: Method, extra: Cents = 0, once: Cents = 0,
             once_month: date | None = None, start: date, max_months: int = 600) -> PayoffResult:
    ds = [_Debt(a, a.balance, a.apr_bp, paid_month=0 if a.balance <= 0 else None) for a in debts]
    budget = sum(a.payment for a in debts) + extra
    once_key = once_month.replace(day=1) if once_month else None
    totals = [sum(max(0, d.bal) for d in ds)]
    m = 0
    while m < max_months and any(d.bal > 0 for d in ds):
        md = add_months(start, m)
        for d in ds:
            promo = d.acct.promo_ends
            if promo and md >= promo and d.bal_at_promo is None:
                d.bal_at_promo = max(0, d.bal)
                if d.acct.apr_after_bp is not None:
                    d.apr = d.acct.apr_after_bp
        for d in ds:
            if d.bal > 0:
                i = _div_half_up(d.bal * d.apr, 120000)
                d.bal += i
                d.interest += i
        avail = budget + (once if once_key == md else 0)
        for d in ds:
            if d.bal > 0:
                p = min(d.acct.payment, d.bal, avail)
                d.bal -= p
                avail -= p
        order = sorted((d for d in ds if d.bal > 0), key=partial(_priority, md=md, method=method))
        for d in order:
            if avail <= 0:
                break
            p = min(d.bal, avail)
            d.bal -= p
            avail -= p
        for d in ds:
            if d.bal <= 0 and d.paid_month is None:
                d.bal = 0
                d.paid_month = m + 1
        totals.append(sum(max(0, d.bal) for d in ds))
        m += 1
    outcomes = tuple(DebtOutcome(d.acct.id, d.acct.name, d.paid_month,
                                 add_months(start, d.paid_month - 1) if d.paid_month else None, d.interest,
                                 d.bal_at_promo, d.acct.promo_ends, d.acct.deferred_interest) for d in ds)
    return PayoffResult(m, not any(d.bal > 0 for d in ds), sum(d.interest for d in ds), outcomes, tuple(totals))


HISTORY_MAX = 120     # balances kept per account (ten years of monthly statements)


@dataclass(frozen=True, slots=True)
class Estimate:
    balance: Cents                   # the typed balance, plus interest, less the payments since
    since: date                      # the typed balance's date
    paid: Cents                      # reconciled payments after it
    payments: int
    interest: Cents


def estimate(acct: Account, budget: Budget, history: Sequence[BankTxn], *, today: date) -> Estimate | None:
    """A debt's balance between hand updates (#97): the typed balance, a month of interest at the APR on each monthly
    anniversary of its date, less the bank postings that paid the recurring items linked to this debt (`Series.debt`)
    after that date. None without a typed date, without a linked item, or with no payment since. The typed balance
    stays the one the payoff plan and the stale warning use."""
    if acct.as_of is None or acct.as_of >= today:
        return None
    linked = {s.id for s in budget.series if s.debt == acct.id}
    if not linked:
        return None
    posted = {t.id: t for t in history}
    paid_by: dict[str, BankTxn] = {}
    for r in budget.rows:
        if r.series_id not in linked:
            continue
        for tid in (*r.paid_txn_ids, *((r.cleared_txn_id,) if r.cleared_txn_id else ())):
            t = posted.get(tid)
            if t is not None and acct.as_of < t.posted <= today and t.amount < 0:
                paid_by[t.id] = t
    if not paid_by:
        return None
    events = sorted(paid_by.values(), key=lambda t: (t.posted, t.id))
    bal, interest, k = max(0, acct.balance), 0, 1
    for t in [*events, None]:
        until = t.posted if t is not None else today
        while add_months(acct.as_of, k) <= until:          # interest on each monthly anniversary before this payment
            i = _div_half_up(bal * acct.apr_bp, 120000)
            bal, interest, k = bal + i, interest + i, k + 1
        if t is not None:
            bal = max(0, bal + t.amount)
    paid = -sum(t.amount for t in events)
    return Estimate(bal, acct.as_of, paid, len(events), interest)


def record(old: Account | None, new: Account) -> Account:
    """Keep the balance history: when the balance or its date changes, the earlier balance and the new one are
    remembered (one per date, the latest wins), so the Debts page can show what was actually paid down."""
    hist = dict(old.history if old else new.history)
    if old is not None and old.as_of is not None and (old.balance, old.as_of) != (new.balance, new.as_of):
        hist.setdefault(old.as_of, old.balance)
    if new.as_of is not None and (old is None or (old.balance, old.as_of) != (new.balance, new.as_of)):
        hist[new.as_of] = new.balance
    return replace(new, history=tuple(sorted(hist.items()))[-HISTORY_MAX:])


def _free_on(start: date, r: PayoffResult) -> date | None:
    """The month the last debt is paid off (None when not within the simulated years)."""
    return add_months(start, max(r.months, 1) - 1) if r.done else None


def plan(accounts: Sequence[Account], *, method: Method, extra: Cents = 0, once: Cents = 0,
         once_month: date | None = None, include_mortgage: bool = False, today: date) -> PayoffPlan:
    start = add_months(today.replace(day=1), 1)
    owed = [a for a in accounts if a.kind not in NOT_DEBTS and a.balance > 0]
    debts = [a for a in owed if a.include_in_payoff and (include_mortgage or not a.is_mortgage)]
    excluded = tuple(a.name for a in owed if a not in debts)
    result = simulate(debts, method=method, extra=extra, once=once, once_month=once_month, start=start)
    other = simulate(debts, method='snowball' if method == 'avalanche' else 'avalanche', extra=extra, once=once,
                     once_month=once_month, start=start)
    baseline = simulate(debts, method=method, start=start)
    promos: list[PromoVerdict] = []
    by_id = {a.id: a for a in debts}
    for o in result.debts:
        if not o.promo_ends or o.promo_ends <= start:
            continue
        a = by_id[o.id]
        if o.balance_at_promo is None:
            paid_before, left = o.paid_month is not None, 0 if o.paid_month is not None else a.balance
        else:
            paid_before, left = o.balance_at_promo == 0, o.balance_at_promo
        n, md = 0, start
        while md < o.promo_ends:
            n += 1
            md = add_months(md, 1)
        promos.append(PromoVerdict(a.id, a.name, o.promo_ends, paid_before, left,
                                   _div_half_up(a.balance, max(1, n)), n, a.deferred_interest, a.apr_after_bp is None))
    not_covering = tuple(a.name for a in debts if a.payment * 120000 <= a.balance * a.apr_bp)
    return PayoffPlan(method, result, other, baseline, start, _free_on(start, result),
                      max(0, baseline.total_interest - result.total_interest),
                      baseline.months - result.months if baseline.done and result.done else None,
                      other.total_interest - result.total_interest, tuple(promos), not_covering, excluded,
                      _free_on(start, other))
