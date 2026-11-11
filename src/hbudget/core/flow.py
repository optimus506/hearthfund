"""Where the money goes: data for the Report tab's Sankey (ARCHITECTURE section 11.1)."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .cards import categorise
from .dates import add_months
from .models import BankTxn, Budget, CardTxn, Kind, RowState, RuleKind
from .money import Cents
from .rules import classify

__all__ = ['DEFAULT_COLOURS', 'Flow', 'FlowGroupTotal', 'FlowItem', 'actual', 'months_back', 'planned']

DEFAULT_COLOURS = {
    'Money in': '--f-in', 'Left over': '--f-in', 'Debt payments': '--f-debt', 'Home': '--f-home',
    'Food & everyday': '--f-food', 'Card': '--f-card', 'Savings drawn': '--f-savings', 'Utilities': '--f-util',
    'Insurance': '--f-ins', 'Family': '--f-family', 'Other': '--f-other',
}
FOOD = 'Food & everyday'


@dataclass(frozen=True, slots=True)
class FlowItem:
    name: str
    amount: Cents


@dataclass(frozen=True, slots=True)
class FlowGroupTotal:
    name: str
    colour: str
    items: tuple[FlowItem, ...]
    total: Cents


@dataclass(frozen=True, slots=True)
class Flow:
    start: date
    end: date            # exclusive
    months: int
    sources: tuple[FlowItem, ...]
    groups: tuple[FlowGroupTotal, ...]
    note: str
    complete: bool
    pay: Cents = 0       # the part of the money in that is pay (pay rules / income rows), not savings or loans


def months_back(last: date, n: int = 12) -> tuple[date, date]:
    """n full calendar months ending before the month of last + 1 day (start inclusive, end exclusive)."""
    nxt = last + timedelta(days=1)
    end = date(nxt.year, nxt.month, 1)
    return add_months(end, -n), end


def _items(d: dict[str, Cents]) -> tuple[FlowItem, ...]:
    return tuple(FlowItem(k, v) for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0])) if v)


def _pack(budget: Budget, start: date, end: date, sources: dict[str, Cents],
          groups: dict[str, dict[str, Cents]], note: str, complete: bool, pay: Cents, months: int = 12) -> Flow:
    colours = {g.name: g.colour for g in budget.flow_groups}
    totals = [FlowGroupTotal(name, colours.get(name, DEFAULT_COLOURS.get(name, '--f-other')), _items(items),
                             sum(items.values())) for name, items in groups.items() if sum(items.values())]
    totals.sort(key=lambda g: (-g.total, g.name))
    money_in, money_out = sum(sources.values()), sum(g.total for g in totals)
    if money_in > money_out:
        totals.append(FlowGroupTotal('Left over', DEFAULT_COLOURS['Left over'], (), money_in - money_out))
    return Flow(start, end, months, _items(sources), tuple(totals), note, complete, pay)


def _split(total: Cents, weights: dict[str, Cents]) -> dict[str, Cents]:
    """Split total in proportion to weights, exactly (largest remainder)."""
    w = sum(weights.values())
    shares = {k: total * v // w for k, v in weights.items()}
    rest = total - sum(shares.values())
    for k in sorted(weights, key=lambda k: (-(total * weights[k] % w), k))[:rest]:
        shares[k] += 1
    return shares


def _month_count(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def actual(history: Sequence[BankTxn], card_txns: Sequence[CardTxn], budget: Budget, *, today: date,
           months: int = 12) -> Flow:
    """The last `months` full months of bank postings; card payments are split by what was bought on the card.

    Only full calendar months count (D-076): when the history starts mid-month, that month is left out, and the
    per-month figures are divided by the months actually covered, not by `months`."""
    if not history:
        start, end = months_back(today, months)
        return _pack(budget, start, end, {}, {}, 'No bank history yet.', False, 0, months)
    start, end = months_back(max(t.posted for t in history), months)
    oldest = min(t.posted for t in history)
    first_full = oldest if oldest.day == 1 else add_months(date(oldest.year, oldest.month, 1), 1)
    complete = first_full <= start
    if first_full >= end:
        return _pack(budget, start, end, {}, {}, 'The bank history covers less than one full month so far.', False, 0,
                     months)
    start = max(start, first_full)
    covered = max(1, _month_count(date(start.year, start.month, 1), end))
    by_id = budget.series_by_id()
    group_of_rule: dict[str, str] = {}
    for g in budget.flow_groups:
        for rid in g.rule_ids:
            group_of_rule.setdefault(rid, g.name)
    has_food = any(g.name == FOOD for g in budget.flow_groups)
    sources: dict[str, Cents] = defaultdict(int)
    pay = 0
    groups: dict[str, dict[str, Cents]] = defaultdict(lambda: defaultdict(int))
    card_paid: dict[str, Cents] = defaultdict(int)       # card rule id -> payments
    for t in history:
        if not start <= t.posted < end:
            continue
        rule = classify(t, budget.rules)
        if rule is not None and rule.kind is RuleKind.IGNORE:
            continue
        if t.amount > 0:
            if rule is not None and rule.kind is RuleKind.PAY:
                first = rule.targets[0] if rule.targets else ''
                sources[by_id[first].name if first in by_id else (first or rule.name)] += t.amount
                pay += t.amount
            elif rule is not None and rule.kind is RuleKind.SAVINGS_DRAW:
                sources['From savings'] += t.amount
            elif rule is not None and rule.kind is RuleKind.LOAN_IN:
                sources['Loans'] += t.amount
            else:
                sources['Other money in'] += t.amount
            continue
        out = -t.amount
        if rule is None:
            if has_food:
                groups[FOOD][categorise(t.description, t.category, budget.card_categories)] += out
            else:
                groups['Other']['Other'] += out
        elif rule.kind is RuleKind.CARD:
            card_paid[rule.id] += out
        else:
            groups[group_of_rule.get(rule.id, 'Other')][rule.name] += out
    spent: dict[str, Cents] = defaultdict(int)
    for c in card_txns:
        if start <= c.purchased < end and c.amount < 0:
            spent[c.category or 'Other'] += -c.amount
    for rid, paid in card_paid.items():
        group = group_of_rule.get(rid, 'Card')
        if spent:
            for name, share in _split(paid, spent).items():
                groups[group][name] += share
        else:
            groups[group]['Card payments'] += paid
    note = 'Bank postings %s to %s.' % (start.isoformat(), (end - timedelta(days=1)).isoformat())
    if card_paid:
        note += ' Card payments are split by what was bought on the card in the same months.'
    if not complete:
        note += (' The history covers %d full month%s, so the monthly figures are averages over those.'
                 % (covered, '' if covered == 1 else 's'))
    return _pack(budget, start, end, sources, groups, note, complete, pay, covered)


def planned(budget: Budget, *, today: date) -> Flow:
    """The next 12 months of open budget rows."""
    start = today + timedelta(days=1)
    end = date(start.year + 1, 3, 1) if (start.month, start.day) == (2, 29) else start.replace(year=start.year + 1)
    group_of: dict[str, str] = {}
    for g in budget.flow_groups:
        for key in (*g.series_ids, *g.row_names):
            group_of.setdefault(key, g.name)
    sources: dict[str, Cents] = defaultdict(int)
    pay = 0
    groups: dict[str, dict[str, Cents]] = defaultdict(lambda: defaultdict(int))
    for r in budget.rows:
        if r.state is not RowState.OPEN or not start <= r.due_date < end or r.amount == 0:
            continue
        if r.kind is Kind.INCOME or r.amount < 0:
            sources[r.name] += abs(r.amount)
            if r.kind is Kind.INCOME:
                pay += abs(r.amount)
        else:
            name = group_of.get(r.series_id or '', group_of.get(r.name, 'Other'))
            groups[name][r.name] += r.amount
    note = 'Budget rows due %s to %s.' % (start.isoformat(), (end - timedelta(days=1)).isoformat())
    return _pack(budget, start, end, sources, groups, note, True, pay)
