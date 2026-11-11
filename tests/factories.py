"""Small builders for synthetic test data (fake household, round amounts, years 2028-2031)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from hbudget.core.models import (
    BankTxn,
    Budget,
    Cadence,
    Horizon,
    Kind,
    LedgerRow,
    PayeeRule,
    RuleKind,
    Schedule,
    Series,
    Weekend,
)

D = date


def schedule(cadence: Cadence | str, start: date, **kw: Any) -> Schedule:
    return Schedule(cadence=Cadence(cadence), start=start, **kw)


def series(id: str, kind: Kind | str, amount: int, sched: Schedule, **kw: Any) -> Series:
    return Series(id=id, name=kw.pop('name', id.replace('-', ' ').capitalize()), kind=Kind(kind), amount=amount,
                  schedule=sched, **kw)


def row(id: str, name: str, amount: int, due: date, *, order: date | None = None, seq: int = 0,
        kind: Kind | str | None = None, **kw: Any) -> LedgerRow:
    k = Kind(kind) if kind else (Kind.INCOME if amount < 0 else Kind.BILL)
    return LedgerRow(id=id, series_id=kw.pop('series_id', None), name=name, kind=k, amount=amount, due_date=due,
                     order_date=order or due, seq=seq, **kw)


AS_OF = D(2029, 3, 1)


def budget(start: int = 100000, as_of: date = AS_OF, *, series: tuple[Series, ...] = (),
           rows: tuple[LedgerRow, ...] = (), horizon: Horizon | None = None, **kw: Any) -> Budget:
    return Budget(start_balance=start, as_of=as_of, horizon=horizon or Horizon('fixed', end=D(2029, 4, 30)),
                  series=series, rows=rows, **kw)


def txn(id: str, posted: date, amount: int, desc: str, balance: int | None = None, i: int = 0) -> BankTxn:
    return BankTxn(id=id, posted=posted, amount=amount, description=desc, balance=balance, file_index=i)


def chain(start_balance: int, items: list[tuple[str, date, int, str]], *, basis: bool = True) -> list[BankTxn]:
    """Bank postings given oldest first, with a consistent running balance, returned newest first.

    With basis=True an extra posting dated the day before the first item leaves the starting balance, so the
    export reaches back to it (as a real export must)."""
    out, bal = [], start_balance
    if basis and items:
        out.append(('basis', items[0][1] - timedelta(days=1), 0, 'OPENING', start_balance))
    for id_, d, amount, desc in items:
        bal += amount
        out.append((id_, d, amount, desc, bal))
    out.reverse()
    return [txn(i, d, a, s, b, n) for n, (i, d, a, s, b) in enumerate(out)]


def rule(id: str, kind: RuleKind | str, contains: tuple[str, ...], **kw: Any) -> PayeeRule:
    return PayeeRule(id=id, name=kw.pop('name', id), kind=RuleKind(kind), contains=contains, **kw)


__all__ = ['D', 'Weekend', 'budget', 'chain', 'row', 'rule', 'schedule', 'series', 'txn']
