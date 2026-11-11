"""Credit-card spending by category and week (ARCHITECTURE section 11.3, D-016)."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Literal

from .dates import add_months, week_start
from .models import Budget, Cadence, CardCategory, CardTxn, LedgerRow, RowState, Schedule, Series
from .money import Cents
from .recurrence import Occurrence, amount_for, occurrences

__all__ = ['DEFAULT_CATEGORIES', 'CardSummary', 'CardWeek', 'Period', 'Store', 'categorise', 'recategorise', 'spending',
           'store', 'stores', 'weekly']

OTHER = 'Other'
PAYMENT = 'Payment'

# generic keywords only, never real merchant names (D-016)
DEFAULT_CATEGORIES: tuple[CardCategory, ...] = (
    CardCategory('Grocery', ('GROCERY', 'SUPERMARKET', 'MARKET', 'FOODS')),
    CardCategory('Fuel', ('FUEL', 'GAS STATION', 'PETROL')),
    CardCategory('Eating out', ('RESTAURANT', 'CAFE', 'COFFEE', 'PIZZA', 'GRILL', 'DINER', 'TAKEOUT'),
                 bank_categories=('Food & Dining',)),
    CardCategory('Pharmacy', ('PHARMACY', 'DRUG')),
)


@dataclass(frozen=True, slots=True)
class CardWeek:
    week: date
    actual: tuple[tuple[str, Cents], ...]
    budget: tuple[tuple[str, Cents], ...]


@dataclass(frozen=True, slots=True)
class CardSummary:
    categories: tuple[str, ...]
    weeks: tuple[CardWeek, ...]
    since: date | None
    through: date | None
    full_weeks: int
    avg_per_week: tuple[tuple[str, Cents], ...]
    budget_per_week: tuple[tuple[str, Cents], ...]
    weeks_over: tuple[tuple[str, int], ...]
    monthly_spend: Cents
    by_category: tuple[tuple[str, Cents], ...]


_CUT = re.compile(r'[*#]')
_WORD = re.compile(r"[^A-Z0-9&' ]+")
STORE_MAX = 40


def store(description: str) -> str:
    """The store a card purchase was made at, from its description (D-083): upper case, cut at the first "*" or
    "#" (an order or branch number follows), words with a digit dropped, other punctuation turned to spaces.
    "CORNER MARKET #1234 SPRINGFIELD" and "CORNER MARKET #5678 SPRINGFIELD" are one store; "SHOP*AB12CD" is "SHOP".
    A description that leaves nothing keeps its whole upper-cased text."""
    text = ' '.join(description.upper().split())
    head = _CUT.split(text, maxsplit=1)[0]
    words = [w for w in _WORD.sub(' ', head).split() if not any(ch.isdigit() for ch in w)]
    name = ' '.join(words).strip(" &'") or text
    return name[:STORE_MAX].rstrip()


def categorise(description: str, bank_category: str, categories: Sequence[CardCategory], *,
               amount: Cents | None = None) -> str:
    """A store moved to a category by hand, else the first category with a keyword in the text, else by the
    export's own category, else "Other". A positive amount (a payment to the card) is "Payment"."""
    if amount is not None and amount > 0:
        return PAYMENT
    text = ' '.join(description.upper().split())
    shop = store(description)
    for c in categories:
        if shop in c.merchants:
            return c.name
    for c in categories:
        if any(k.upper() in text for k in c.keywords):
            return c.name
    bank = bank_category.strip().lower()
    if bank:
        for c in categories:
            if any(b.strip().lower() == bank for b in c.bank_categories):
                return c.name
    return OTHER


def recategorise(txns: Sequence[CardTxn], categories: Sequence[CardCategory]) -> list[tuple[str, str]]:
    """(id, new category) for each stored purchase the categories now sort differently. A purchase moved by hand
    keeps its category while that category exists; one that only the export's bank category placed keeps it when no
    store move or keyword matches, unless that category was deleted."""
    known = {c.name for c in categories}
    out = []
    for t in txns:
        if t.by_hand and (t.category in known or t.category in (OTHER, PAYMENT)):
            continue
        cat = categorise(t.description, '', categories, amount=t.amount)
        if cat == OTHER and t.category in known:
            continue
        if cat != t.category:
            out.append((t.id, cat))
    return out


@dataclass(frozen=True, slots=True)
class Store:
    name: str
    category: str                    # the category most of its purchases are in
    total: Cents                     # spent (positive)
    count: int
    last: date
    moved: bool                      # the store itself was moved to its category by hand


def stores(txns: Sequence[CardTxn], categories: Sequence[CardCategory], *, category: str | None = None,
           since: date | None = None, until: date | None = None) -> list[Store]:
    """Card purchases grouped by store, biggest spend first; only purchases in `category` when given."""
    moved = {m: c.name for c in categories for m in c.merchants}
    spent: Counter[str] = Counter()
    count: Counter[str] = Counter()
    cats: dict[str, Counter[str]] = defaultdict(Counter)
    last: dict[str, date] = {}
    for t in txns:
        if t.amount >= 0 or (since and t.purchased < since) or (until and t.purchased > until):
            continue
        cat = t.category or OTHER
        if category is not None and cat != category:
            continue
        s = store(t.description)
        spent[s] += -t.amount
        count[s] += 1
        cats[s][cat] += -t.amount
        last[s] = max(last.get(s, t.purchased), t.purchased)
    out = [Store(s, cats[s].most_common(1)[0][0], spent[s], count[s], last[s], s in moved) for s in spent]
    return sorted(out, key=lambda x: (-x.total, x.name))


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    spent: tuple[tuple[str, Cents], ...]       # category -> spent, biggest first
    budget: tuple[tuple[str, Cents], ...]      # the budgeted categories' plan for the period


def spending(txns: Sequence[CardTxn], budget: Budget, categories: Sequence[CardCategory], *, since: date,
             until: date, by: Literal['week', 'month'] = 'month', category: str | None = None) -> list[Period]:
    """Card spending per category and week or month from `since` through `until` (purchase dates), with the plan
    for the categories that have budget lines (by weeks: the weeks starting in the period)."""
    ws = budget.settings.week_starts

    def key(d: date) -> date:
        return week_start(d, ws) if by == 'week' else date(d.year, d.month, 1)
    spent: dict[date, Counter[str]] = defaultdict(Counter)
    for t in txns:
        if t.amount < 0 and since <= t.purchased <= until:
            cat = t.category or OTHER
            if category is None or cat == category:
                spent[key(t.purchased)][cat] += -t.amount
    starts: list[date] = []
    d = key(since)
    while since <= until and d <= until:
        starts.append(d)
        d = d + timedelta(days=7) if by == 'week' else add_months(d, 1)
    plan: dict[date, Counter[str]] = defaultdict(Counter)
    if starts:
        for c in categories:
            if not c.weekly_series or c.has_own_rows or (category is not None and c.name != category):
                continue
            for wk, amt in _plan(budget, c.weekly_series, starts[0], until).items():
                if starts[0] <= wk <= until:    # by months: the weeks that start in the month
                    plan[key(wk)][c.name] += amt
    return [Period(s, tuple(sorted(spent[s].items(), key=lambda kv: (-kv[1], kv[0]))),
                   tuple(sorted(plan[s].items()))) for s in starts]


def _avg(total: int, n: int) -> int:
    return (2 * total + n) // (2 * n) if n else 0


def _back_to(sched: Schedule, before: date) -> Schedule:
    """The same schedule, started on or before `before`: a weekly line set up last month still budgeted the card
    weeks before it was entered. Weekdays and days of the month are kept; a one-off is not repeated."""
    if sched.start <= before or sched.cadence is Cadence.ONCE:
        return sched
    if sched.cadence in (Cadence.WEEKLY, Cadence.BIWEEKLY):
        step = 14 if sched.cadence is Cadence.BIWEEKLY else 7 * max(1, sched.interval)
        n = -(-(sched.start - before).days // step)
        return replace(sched, start=sched.start - timedelta(days=n * step))
    day = sched.day or sched.start.day       # monthly and the rest: from the first of that month, same day(s)
    return replace(sched, start=date(before.year, before.month, 1), day=day if sched.cadence is not
                   Cadence.SEMIMONTHLY else sched.day)


def _plan(budget: Budget, series_ids: Sequence[str], start: date, end: date) -> Counter[date]:
    """Planned amounts of the linked series by week start, from the schedules (seasonal amounts included), not from
    the forecast's rows: past rows leave the forecast once cleared, which made every finished week's budget $0
    (#45). A row changed by hand or removed counts as that week's plan instead."""
    ws = budget.settings.week_starts
    by_id = budget.series_by_id()
    own_rows: dict[tuple[str, date], LedgerRow] = {}
    for r in budget.rows:
        if r.series_id and (r.overridden or r.state is RowState.REMOVED):
            own_rows[(r.series_id, r.nominal_date or r.due_date)] = r
    out: Counter[date] = Counter()
    for sid in series_ids:
        s = by_id.get(sid)
        if s is None or not s.active:
            continue
        for occ in occurrences(_back_to(s.schedule, start - timedelta(days=7)), start - timedelta(days=7), end):
            out[week_start(occ.actual, ws)] += _planned(own_rows.get((sid, occ.nominal)), s, occ)
    return out


def _planned(changed: LedgerRow | None, s: Series, occ: Occurrence) -> Cents:
    """One occurrence's planned amount: the row's own amount when it was changed by hand, 0 when removed."""
    if changed is None:
        return amount_for(s, occ)
    return 0 if changed.state is RowState.REMOVED else abs(changed.amount)


def weekly(card_txns: Sequence[CardTxn], budget: Budget, categories: Sequence[CardCategory], *, today: date,
           weeks: int = 12, months: int | None = None) -> CardSummary:
    """Card spending by week and category. With `months`, only purchases from that many months back count."""
    ws = budget.settings.week_starts
    own = {c.name for c in categories if c.has_own_rows}
    shown = [c.name for c in categories if not c.has_own_rows] + [OTHER]
    budgeted = {c.name: c.weekly_series for c in categories if c.weekly_series and not c.has_own_rows}
    cutoff = add_months(today, -months) if months else None
    buys = [t for t in card_txns if t.amount < 0 and (cutoff is None or t.purchased >= cutoff)]
    actual: dict[date, Counter[str]] = defaultdict(Counter)
    for t in buys:
        if t.category in own:
            continue
        name = t.category if t.category in shown else OTHER
        actual[week_start(t.purchased, ws)][name] += -t.amount
    this_week = week_start(today, ws)
    span_start = min(actual, default=this_week)
    span_end = max([*actual, this_week]) + timedelta(days=6)
    plan: dict[date, Counter[str]] = defaultdict(Counter)
    for name, series in budgeted.items():
        for wk, amt in _plan(budget, series, span_start, span_end).items():
            plan[wk][name] += amt
    full = sorted(w for w in actual if w < this_week)
    last = sorted(actual)[-weeks:]
    out_weeks = tuple(CardWeek(w, tuple((c, actual[w][c]) for c in shown),
                               tuple((c, plan[w][c]) for c in budgeted)) for w in last)
    avg = tuple((c, _avg(sum(actual[w][c] for w in full), len(full))) for c in shown)
    bud = tuple((c, _avg(sum(plan[w][c] for w in full), len(full))) for c in budgeted)
    over = tuple((c, sum(1 for w in full if plan[w][c] and actual[w][c] * 10 > plan[w][c] * 11)) for c in budgeted)
    since = min((t.purchased for t in buys), default=None)
    through = max((t.purchased for t in buys), default=None)
    total = sum(-t.amount for t in buys)
    days = (through - since).days if since and through else 0
    # total / months, where months = days / 30.44 (at least one month)
    monthly = _avg(total * 3044, days * 100) if days * 100 > 3044 else total
    by_cat: Counter[str] = Counter()
    for t in buys:
        by_cat[t.category or OTHER] += -t.amount
    return CardSummary(tuple(shown), out_weeks, since, through, len(full), avg, bud, over, monthly,
                       tuple(sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))))
