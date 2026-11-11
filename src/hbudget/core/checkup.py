"""Bill check-up: what the bank paid compared with the budget (ARCHITECTURE section 11.2)."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from .alerts import period_start
from .models import BankTxn, Budget, RowState, RuleKind
from .money import Cents
from .rules import classify

__all__ = ['BillCheck', 'CheckUp', 'compute']

Verdict = Literal['ok', 'over', 'under']


@dataclass(frozen=True, slots=True)
class BillCheck:
    rule_id: str
    name: str
    targets: tuple[str, ...]
    kind: RuleKind
    budget: Cents | None
    per: Literal['month', 'deposit']
    actuals: tuple[tuple[date, Cents], ...]     # the last 6
    count: int
    average: Cents
    diff_tenths_pct: int | None
    verdict: Verdict


@dataclass(frozen=True, slots=True)
class CheckUp:
    since: date | None
    through: date | None
    transactions: int
    bills: tuple[BillCheck, ...]
    cap_weeks: tuple[tuple[str, tuple[tuple[date, Cents], ...]], ...]
    # spending under each "track" rule (not budgeted), per week for the last TRACK_WEEKS weeks, empty weeks included
    tracked: tuple[tuple[str, str, tuple[tuple[date, Cents], ...]], ...] = ()


TRACK_WEEKS = 8


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _full_months(first: date, last: date) -> set[date]:
    """Calendar months wholly inside [first, last], as in the original check-up."""
    m = date(first.year, first.month, 1)
    if first.day > 1:
        m = _next_month(m)
    out = set()
    while _next_month(m) <= last:
        out.add(m)
        m = _next_month(m)
    return out


def _round_div(n: int, d: int) -> int:
    q = (abs(n) * 2 + d) // (2 * d)
    return q if n >= 0 else -q


def compute(history: Sequence[BankTxn], budget: Budget) -> CheckUp:
    hist = sorted(history, key=lambda t: (t.posted, t.id))
    if not hist:
        return CheckUp(None, None, 0, (), ())
    open_rows = [r for r in budget.rows if r.state is RowState.OPEN]
    tol_tenths = budget.settings.alerts.bill_variance_bp // 10
    matched: dict[str, list[BankTxn]] = defaultdict(list)
    rules_used = []
    for t in hist:
        rule = classify(t, budget.rules)
        if rule is None or rule.kind not in (RuleKind.BILL, RuleKind.ASK, RuleKind.PAY, RuleKind.ALLOWANCE):
            continue
        if rule.id not in matched:
            rules_used.append(rule)
        matched[rule.id].append(t)
    full = _full_months(hist[0].posted, hist[-1].posted)
    bills: list[BillCheck] = []
    for rule in rules_used:
        planned = []
        for target in rule.targets:
            sizes = Counter(abs(r.amount) for r in open_rows if r.name == target or r.series_id == target)
            if sizes:
                planned.append(sizes.most_common(1)[0][0])
        budget_amt = planned[0] if len(planned) == 1 else None
        txns = matched[rule.id]
        if rule.kind is RuleKind.PAY:
            per: Literal['month', 'deposit'] = 'deposit'
            series = [(t.posted, abs(t.amount)) for t in txns]
            vals = [v for _, v in series]
        else:
            per = 'month'
            months: dict[date, Cents] = defaultdict(int)
            for t in txns:
                months[date(t.posted.year, t.posted.month, 1)] += abs(t.amount)
            series = sorted(months.items())
            vals = [v for m, v in series if m in full] or [v for _, v in series]
        average = _round_div(sum(vals), len(vals))
        diff = None
        verdict: Verdict = 'ok'
        if budget_amt:
            diff = _round_div(1000 * (average - budget_amt), budget_amt)
            if abs(diff) > tol_tenths:
                verdict = 'over' if diff > 0 else 'under'
        bills.append(BillCheck(rule.id, rule.name, rule.targets, rule.kind, budget_amt, per, tuple(series[-6:]),
                               len(vals), average, diff, verdict))
    caps = []
    ws = budget.settings.week_starts
    for cap in budget.settings.alerts.caps:
        weeks: dict[date, Cents] = defaultdict(int)
        for t in hist:
            rule = classify(t, budget.rules)
            if rule is not None and rule.id in cap.rule_ids and t.amount < 0:
                weeks[period_start(t.posted, 'week', ws)] += -t.amount
        caps.append((cap.id, tuple(sorted(weeks.items())[-12:])))
    last = period_start(hist[-1].posted, 'week', ws)
    starts = [last - timedelta(weeks=i) for i in range(TRACK_WEEKS - 1, -1, -1)]
    spent: dict[str, dict[date, Cents]] = defaultdict(lambda: defaultdict(int))
    for t in hist:
        rule = classify(t, budget.rules)
        if rule is not None and rule.kind is RuleKind.TRACK and t.amount < 0 and t.posted >= starts[0]:
            spent[rule.id][period_start(t.posted, 'week', ws)] += -t.amount
    tracked = tuple((r.id, r.name, tuple((w, spent[r.id][w]) for w in starts))
                    for r in budget.rules if r.kind is RuleKind.TRACK and r.id in spent)
    return CheckUp(hist[0].posted, hist[-1].posted, len(hist), tuple(bills), tuple(caps), tracked)
