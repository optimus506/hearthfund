"""The monthly recap and the year in review: what happened in a month (or a calendar year) from the bank and card
history and the ledger, next to the period before (D-090). Nothing is stored.

Money in and out are the bank postings in the period, the household's ignored lines left out; pay is what the pay
rules match. A card payment counts as money out when it leaves the bank, and what was bought on the card is shown
by category (purchase dates) against the budget for the categories that have one. Bills paid are the bill rows
cleared in the period; bills still open are bill rows due in the period that nobody has cleared.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from .alerts import payee_key
from .cards import spending
from .dates import add_months
from .models import BankTxn, Budget, CardTxn, Kind, Milestone, RowState, RuleKind
from .money import Cents
from .rules import classify
from .subscriptions import Recurring, detect

__all__ = ['Recap', 'Totals', 'periods_with_history', 'recap']

Span = Literal['month', 'year']
TOP = 5


@dataclass(frozen=True, slots=True)
class Totals:
    money_in: Cents
    money_out: Cents
    pay: Cents
    card_spent: Cents

    @property
    def net(self) -> Cents:
        return self.money_in - self.money_out


@dataclass(frozen=True, slots=True)
class Recap:
    span: Span
    start: date
    end: date                                        # exclusive
    totals: Totals
    before: Totals                                   # the period before, the same length
    has_history: bool                                # any bank posting in the period
    complete: bool                                   # the period is over
    top_payees: tuple[tuple[str, Cents], ...]        # money out by payee (card payments left out), biggest first
    card: tuple[tuple[str, Cents, Cents | None], ...]   # category, spent, budget (None: no budget line)
    bills_paid: tuple[tuple[str, date, Cents], ...]  # name, due, amount
    bills_open: tuple[tuple[str, date, Cents], ...]  # due in the period and still open
    new_recurring: tuple[Recurring, ...]             # first charged in the period
    price_changes: tuple[tuple[Recurring, date, Cents, Cents], ...]   # changed in the period: (item, from, old, new)
    wins: tuple[Milestone, ...]
    last_balance: Cents | None                       # the bank balance on the period's last posting, when known


def _bounds(span: Span, start: date) -> tuple[date, date]:
    first = date(start.year, 1, 1) if span == 'year' else date(start.year, start.month, 1)
    return first, add_months(first, 12 if span == 'year' else 1)


def _totals(history: Sequence[BankTxn], card_txns: Sequence[CardTxn], budget: Budget, start: date,
            end: date) -> tuple[Totals, Counter[str]]:
    money_in = money_out = pay = 0
    payees: Counter[str] = Counter()
    for t in history:
        if not start <= t.posted < end:
            continue
        rule = classify(t, budget.rules)
        if rule is not None and rule.kind is RuleKind.IGNORE:
            continue
        if t.amount > 0:
            money_in += t.amount
            if rule is not None and rule.kind is RuleKind.PAY:
                pay += t.amount
        else:
            money_out -= t.amount
            if rule is None or rule.kind is not RuleKind.CARD:
                payees[rule.name if rule is not None else (payee_key(t.description) or 'Other')] += -t.amount
    card = sum(-c.amount for c in card_txns if c.amount < 0 and start <= c.purchased < end)
    return Totals(money_in, money_out, pay, card), payees


def recap(history: Sequence[BankTxn], card_txns: Sequence[CardTxn], budget: Budget, *, span: Span, start: date,
          today: date) -> Recap:
    first, end = _bounds(span, start)
    before_first = add_months(first, -12 if span == 'year' else -1)
    totals, payees = _totals(history, card_txns, budget, first, end)
    before, _ = _totals(history, card_txns, budget, before_first, first)
    last_day = min(end, today + timedelta(days=1)) - timedelta(days=1)
    plan: dict[str, Cents] = {}
    spent: Counter[str] = Counter()
    for p in spending(card_txns, budget, budget.card_categories, since=first, until=end - timedelta(days=1)):
        spent.update(dict(p.spent))
        for k, v in p.budget:
            plan[k] = plan.get(k, 0) + v
    card = tuple((k, v, plan.get(k)) for k, v in sorted(spent.items(), key=lambda kv: (-kv[1], kv[0])))
    bills = [r for r in budget.rows if r.kind is Kind.BILL]
    paid = sorted(((r.name, r.due_date, r.paid or r.amount) for r in bills if r.state is RowState.CLEARED
                   and r.cleared_on is not None and first <= r.cleared_on < end), key=lambda x: (x[1], x[0]))
    still_open = sorted(((r.name, r.due_date, r.amount) for r in bills if r.state is RowState.OPEN
                         and first <= r.due_date <= last_day and r.due_date < today), key=lambda x: (x[1], x[0]))
    found = detect(history, card_txns, budget, today=last_day)
    new = tuple(i for i in found.items if first <= i.first < end)
    changes = tuple((i, d, old, new_p) for i in found.items for d, old, new_p in i.price_changes if first <= d < end)
    wins = tuple(x for x in budget.settings.milestones if first <= x.on < end)
    in_period = [t for t in history if first <= t.posted < end]
    last = max(in_period, key=lambda t: (t.posted, t.id), default=None)
    return Recap(span, first, end, totals, before, bool(in_period), end <= today,
                 tuple(payees.most_common(TOP)), card, tuple(paid), tuple(still_open), new, changes, wins,
                 last.balance if last is not None else None)


def periods_with_history(history: Sequence[BankTxn], span: Span) -> list[date]:
    """The months (or years) that have bank postings, oldest first."""
    return sorted({_bounds(span, t.posted)[0] for t in history})
