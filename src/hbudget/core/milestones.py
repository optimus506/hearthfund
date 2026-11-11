"""Milestones and wins (D-082): a debt paid off, a goal reached, weeks above the cushion, a month under budget.

`detect` finds what is true now; the caller compares the keys with the household's kept history, so each win is
celebrated once. Gentle on purpose: there are no losses, no streaks that break, nothing about bad weeks."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta

from .dates import add_months, week_start
from .goals import progress
from .models import BankTxn, Budget, Kind, Milestone, RowState, RuleKind
from .money import Cents, fmt
from .payoff import NOT_DEBTS
from .rules import classify

__all__ = ['CUSHION_WEEKS', 'HISTORY_MAX', 'detect', 'merge']

CUSHION_WEEKS = (4, 8, 13, 26, 52)      # weeks in a row with the balance never under the cushion
HISTORY_MAX = 200


def _debts_paid(budget: Budget) -> list[Milestone]:
    out = []
    for a in budget.accounts:
        if a.kind in NOT_DEBTS or a.balance > 0:
            continue
        owed = [v for _, v in a.history if v > 0]
        if owed:                              # it had a balance once, and now has none
            on = next((d for d, v in reversed(a.history) if v <= 0), a.as_of) or date.min
            out.append(Milestone('debt:' + a.id, 'debt_paid', '%s is paid off' % a.name, on))
    return out


def _goals(budget: Budget, today: date) -> list[Milestone]:
    return [Milestone('goal:' + p.goal.id, 'goal_reached', 'Goal reached: %s' % p.goal.name, today)
            for p in progress(budget, today=today) if p.reached]


def _above_cushion(budget: Budget, history: Sequence[BankTxn], today: date) -> list[Milestone]:
    cushion = budget.settings.alerts.cushion
    ws = budget.settings.week_starts
    lows: dict[date, Cents] = {}
    for t in history:
        if t.balance is None:
            continue
        w = week_start(t.posted, ws)
        lows[w] = min(lows.get(w, t.balance), t.balance)
    this_week = week_start(today, ws)
    weeks = sorted(w for w in lows if w < this_week)
    run: list[date] = []
    for w in weeks:                          # the latest unbroken run of finished weeks at or above the cushion
        if lows[w] >= cushion and (not run or w - run[-1] == timedelta(weeks=1)):
            run.append(w)
        elif lows[w] >= cushion:
            run = [w]
        else:
            run = []
    out = []
    for n in CUSHION_WEEKS:
        if len(run) >= n:
            start = run[0]
            out.append(Milestone('cushion:%d:%s' % (n, start.isoformat()), 'above_cushion',
                                 '%d weeks in a row above the %s cushion' % (n, fmt(cushion)),
                                 start + timedelta(weeks=n) - timedelta(days=1)))
    return out


def _under_budget(budget: Budget, history: Sequence[BankTxn], today: date) -> list[Milestone]:
    """Full calendar months the bank history covers where less went out than the budget planned for them."""
    if not history:
        return []
    first = min(t.posted for t in history)
    out_by: dict[date, Cents] = defaultdict(int)
    for t in history:
        if t.amount < 0:
            rule = classify(t, budget.rules)
            if rule is None or rule.kind is not RuleKind.IGNORE:
                out_by[date(t.posted.year, t.posted.month, 1)] -= t.amount
    plan_by: dict[date, Cents] = defaultdict(int)
    for r in budget.rows:
        if r.state is not RowState.REMOVED and r.kind is not Kind.INCOME and r.amount > 0:
            plan_by[date(r.due_date.year, r.due_date.month, 1)] += r.amount + r.paid
    out = []
    this_month = date(today.year, today.month, 1)
    for m in sorted(plan_by):
        if m >= this_month or m < first or (m == date(first.year, first.month, 1) and first.day > 1):
            continue                          # only months the history covers from the 1st, and finished
        if m in out_by and 0 < out_by[m] < plan_by[m]:
            out.append(Milestone('under:%s' % m.strftime('%Y-%m'), 'under_budget',
                                 '%s: %s under budget' % (m.strftime('%B %Y'), fmt(plan_by[m] - out_by[m])),
                                 add_months(m, 1) - timedelta(days=1)))
    return out


def detect(budget: Budget, history: Sequence[BankTxn], *, today: date) -> list[Milestone]:
    return sorted(_debts_paid(budget) + _goals(budget, today) + _above_cushion(budget, history, today)
                  + _under_budget(budget, history, today), key=lambda m: (m.on, m.key))


def merge(kept: Sequence[Milestone], found: Sequence[Milestone]) -> tuple[tuple[Milestone, ...], list[Milestone]]:
    """The history with the new wins added (newest last, at most HISTORY_MAX), and the new wins alone."""
    known = {m.key for m in kept}
    new = [m for m in found if m.key not in known]
    return tuple([*kept, *new][-HISTORY_MAX:]), new
