"""Pay periods for the Home page: what is due before the next payday, and the paydays after it.

A payday is a date with an open income row. Period 0 runs from now until the first payday (it also holds
open rows that are already late); each later period runs from one payday until the next. Rows on a payday
belong to the period that starts there. Totals come from the forecast, so open expected items count too.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from hbudget.core.forecast import Forecast
from hbudget.core.ledger import ordered
from hbudget.core.models import Budget, Cents, Kind, LedgerRow, RowState

__all__ = ['CardTransfer', 'PayPeriod', 'periods', 'transfer_plan', 'transfers']


@dataclass(frozen=True, slots=True)
class CardTransfer:
    """What to move from checking to one credit card: its card-funded rows that are still open (#31, D-085)."""
    card: str                    # the credit-card account id (row.card)
    total: Cents                 # positive
    rows: tuple[LedgerRow, ...]  # in ledger order


@dataclass(frozen=True, slots=True)
class PayPeriod:
    start: date | None          # the payday that opens it; None for "now until the first payday"
    end: date | None            # the next payday (not included); None when no later payday is known
    pay_in: Cents               # money in during the period (positive)
    out: Cents                  # money out during the period (positive)
    balance_end: Cents          # the balance just before `end` (or after the last row when end is None)
    low: Cents                  # the lowest balance in the period (the opening balance counts)
    rows: tuple[LedgerRow, ...]  # open bill and spending rows in the period, in ledger order
    transfers: tuple[CardTransfer, ...] = ()   # per card: the period's card-funded rows, to move to that card


def periods(budget: Budget, f: Forecast, *, today: date, count: int = 4) -> list[PayPeriod]:
    """Period 0 and up to `count - 1` periods after it. Empty when no payday is scheduled from today on."""
    pays = sorted({r.order_date for r in budget.rows
                   if r.state is RowState.OPEN and r.kind is Kind.INCOME and r.amount < 0 and r.order_date >= today})
    if not pays:
        return []
    bounds: list[date | None] = [None, *pays[:count]]
    out: list[PayPeriod] = []
    opening = f.start
    rows = [r for r in ordered(budget.rows) if r.state is RowState.OPEN and r.kind is not Kind.INCOME]
    for i in range(min(count, len(bounds))):
        lo_d, hi_d = bounds[i], bounds[i + 1] if i + 1 < len(bounds) else None

        def inside(d: date, lo_d: date | None = lo_d, hi_d: date | None = hi_d) -> bool:
            return (lo_d is None or d >= lo_d) and (hi_d is None or d < hi_d)
        pts = [p for p in f.points if inside(p.order_date)]
        balance_end = pts[-1].balance if pts else opening
        out.append(PayPeriod(
            start=lo_d, end=hi_d,
            pay_in=-sum(p.amount for p in pts if p.amount < 0), out=sum(p.amount for p in pts if p.amount > 0),
            balance_end=balance_end, low=min([opening, *(p.balance for p in pts)]),
            rows=tuple(r for r in rows if inside(r.order_date)),
            transfers=transfers(r for r in rows if inside(r.order_date))))
        opening = balance_end
    return out


def transfers(rows: Iterable[LedgerRow]) -> tuple[CardTransfer, ...]:
    """The open card-funded rows grouped by card (in the order the cards first appear), with their totals."""
    by: dict[str, list[LedgerRow]] = defaultdict(list)
    for r in rows:
        if r.card and r.state is RowState.OPEN and r.kind is not Kind.INCOME and r.amount > 0:
            by[r.card].append(r)
    return tuple(CardTransfer(c, sum(r.amount for r in rs), tuple(rs)) for c, rs in by.items())


def transfer_plan(budget: Budget, card: str, *, through: date) -> CardTransfer:
    """The card's open card-funded rows due through `through` (late ones included): what to move to it by then."""
    t = transfers(r for r in ordered(budget.rows) if r.card == card and r.due_date <= through)
    return t[0] if t else CardTransfer(card, 0, ())
