"""Running balance and the forecast readout (ARCHITECTURE section 7).

Same definitions as the original workbook: the low excludes the starting balance (MIN from the first data row)
and the first row with the minimum wins (VLOOKUP); the high includes the start; end = balance after the last row.

With `Settings.card_transfers_on_payday` (D-087) an open card-funded row (bills and spending charged to a credit
card) takes nothing from checking on its own date: one "Move to <card>" item per card on the payday that opens its
pay period takes the period's total, after that day's rows. Rows before the first open payday are left on their dates.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from .ledger import ordered
from .models import Budget, Kind, LedgerRow, RowState, WhatIf
from .money import Cents

__all__ = ['BalancePoint', 'Forecast', 'ForecastDelta', 'chart_points', 'compare', 'compute', 'series_for_chart']


@dataclass(frozen=True, slots=True)
class BalancePoint:
    row_id: str | None
    label: str
    due_date: date
    order_date: date
    amount: Cents
    balance: Cents


@dataclass(frozen=True, slots=True)
class Forecast:
    start: Cents
    as_of: date
    points: tuple[BalancePoint, ...]
    low: Cents
    low_date: date
    low_row_id: str | None
    high: Cents
    high_date: date
    end: Cents
    end_date: date
    year_ends: tuple[tuple[int, Cents], ...]
    now: Cents


@dataclass(frozen=True, slots=True)
class ForecastDelta:
    low_change: Cents
    end_change: Cents
    verdict: Literal['ok', 'tight', 'below_zero']


def compute(budget: Budget, *, today: date, include_expected: bool = True, whatifs: Sequence[WhatIf] = (),
            row_amounts: Mapping[str, Cents] | None = None) -> Forecast:
    amounts = row_amounts or {}
    # (sort key, row id, label, due, order, amount); real rows sort before virtual items on the same date
    items: list[tuple[tuple[date, int, int, str, int], str | None, str, date, date, Cents]] = []
    moved = _moved_to_payday(budget) if budget.settings.card_transfers_on_payday else {}
    to_card: dict[tuple[date, str], Cents] = {}
    for r in ordered(budget.rows):
        if r.state is not RowState.OPEN:
            continue
        amount = amounts.get(r.id, r.amount)
        if r.id in moved:
            key = (moved[r.id], r.card or '')
            to_card[key] = to_card.get(key, 0) + amount
            amount = 0
        items.append(((r.order_date, 0, r.seq, r.id, 0), r.id, r.name, r.due_date, r.order_date, amount))
    names = {a.id: a.name for a in budget.accounts}
    virtual: list[tuple[str, Cents, date]] = [('Move to ' + names.get(card, card), total, payday)
                                             for (payday, card), total in to_card.items()]
    if include_expected:
        virtual += [(e.label, e.amount if e.direction == 'out' else -e.amount, e.date)
                    for e in budget.expected if e.status == 'open']
    virtual += [(w.label, w.amount if w.direction == 'out' else -w.amount, w.date) for w in whatifs]
    for n, (label, amount, when) in enumerate(virtual):
        od = max(when, budget.as_of)
        items.append(((od, 1, 0, label, n), None, label, when, od, amount))
    items.sort(key=lambda x: x[0])

    balance = budget.start_balance
    points: list[BalancePoint] = []
    for _, row_id, label, due, od, amount in items:
        balance -= amount
        points.append(BalancePoint(row_id, label, due, od, amount, balance))

    start = budget.start_balance
    if points:
        low_p = min(points, key=lambda p: p.balance)            # min() returns the first minimum
        low, low_date, low_row = low_p.balance, low_p.due_date, low_p.row_id
        high_p = max(points, key=lambda p: p.balance)
        high, high_date = (start, budget.as_of) if start >= high_p.balance else (high_p.balance, high_p.due_date)
        end, end_date = points[-1].balance, points[-1].due_date
    else:
        low, low_date, low_row = start, budget.as_of, None
        high, high_date = start, budget.as_of
        end, end_date = start, budget.as_of

    year_ends: list[tuple[int, Cents]] = []
    last_year = max(points[-1].order_date.year if points else budget.as_of.year, budget.as_of.year)
    i, running = 0, start
    for year in range(budget.as_of.year, last_year + 1):
        while i < len(points) and points[i].order_date.year <= year:
            running = points[i].balance
            i += 1
        year_ends.append((year, running))
    now = start
    for p in points:
        if p.order_date <= today:
            now = p.balance
    return Forecast(start=start, as_of=budget.as_of, points=tuple(points), low=low, low_date=low_date,
                    low_row_id=low_row, high=high, high_date=high_date, end=end, end_date=end_date,
                    year_ends=tuple(year_ends), now=now)


def _moved_to_payday(budget: Budget) -> dict[str, date]:
    """Open card-funded money-out rows -> the open payday on or before their date (rows before the first are kept)."""
    pays = sorted({r.order_date for r in budget.rows
                   if r.state is RowState.OPEN and r.kind is Kind.INCOME and r.amount < 0})
    out: dict[str, date] = {}
    for r in budget.rows:
        if _card_funded(r):
            i = bisect_right(pays, r.order_date)
            if i:
                out[r.id] = pays[i - 1]
    return out


def _card_funded(r: LedgerRow) -> bool:
    return bool(r.card) and r.state is RowState.OPEN and r.kind is not Kind.INCOME and r.amount > 0


def chart_points(f: Forecast) -> list[tuple[date, Cents, str | None, int]]:
    """series_for_chart with, per point, the label of the last row on that date and how many rows the date has
    (None and 0 for the starting balance). Late rows (still open, ordered before the balance date) count on the
    balance date, so the dates never go back in time."""
    out: list[tuple[date, Cents, str | None, int]] = [(f.as_of, f.start, None, 0)]
    for p in f.points:
        d = max(p.order_date, f.as_of)
        if out[-1][0] == d:
            out[-1] = (d, p.balance, p.label, out[-1][3] + 1)
        else:
            out.append((d, p.balance, p.label, 1))
    return out


def series_for_chart(f: Forecast) -> list[tuple[date, Cents]]:
    """One point per order_date (the last balance of that date), starting with (as_of, start)."""
    return [(d, c) for d, c, _, _ in chart_points(f)]


def compare(base: Forecast, other: Forecast, *, cushion: Cents) -> ForecastDelta:
    """Judge `other` (the what-if) on its own; the changes are other - base."""
    verdict: Literal['ok', 'tight', 'below_zero']
    if other.low < 0:
        verdict = 'below_zero'
    elif other.low < cushion:
        verdict = 'tight'
    else:
        verdict = 'ok'
    return ForecastDelta(other.low - base.low, other.end - base.end, verdict)
