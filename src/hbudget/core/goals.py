"""Goals (D-081): an emergency fund in months of bills, savings targets and a debt-free countdown.

Progress only: the app moves no money, so "saved" is what the household types in, and the debt-free goal reads the
debts' balances and the payoff plan."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from .dates import add_months
from .models import Account, Budget, Goal, Kind, RowState
from .money import Cents
from .payoff import NOT_DEBTS, plan

__all__ = ['GoalProgress', 'monthly_bills', 'progress', 'total_debt']


@dataclass(frozen=True, slots=True)
class GoalProgress:
    goal: Goal
    target: Cents                 # what "done" means, in cents (for debt_free: the debt when the goal was set)
    current: Cents                # saved so far, or debt paid down so far
    pct: int                      # 0..100, rounded down (100 only when reached)
    reached: bool
    monthly_needed: Cents | None  # to reach it by the goal's date (None without a date or once reached)
    months_left: int | None       # debt_free: months until the payoff plan finishes (None: not within the plan)
    done_on: date | None          # debt_free: the month the plan pays the last debt


def monthly_bills(budget: Budget, *, today: date) -> Cents:
    """What goes out in an average month: open bill and spending rows due in the next 12 months, divided by 12."""
    end = add_months(today, 12)
    out = sum(r.amount for r in budget.rows if r.state is RowState.OPEN and r.kind is not Kind.INCOME
              and r.amount > 0 and today <= r.due_date < end)
    return (out + 11) // 12


def total_debt(accounts: Sequence[Account]) -> Cents:
    return sum(a.balance for a in accounts if a.kind not in NOT_DEBTS and a.balance > 0 and a.include_in_payoff
               and not a.is_mortgage)


def _months_until(today: date, by: date) -> int:
    n, d = 0, today
    while d < by:
        n += 1
        d = add_months(today, n)
    return max(1, n)


def progress(budget: Budget, *, today: date) -> list[GoalProgress]:
    out = []
    bills = monthly_bills(budget, today=today)
    debt_now = total_debt(budget.accounts)
    p = plan(budget.accounts, method='avalanche', today=today) if any(g.kind == 'debt_free'
                                                                       for g in budget.settings.goals) else None
    for g in budget.settings.goals:
        months_left = done_on = None
        if g.kind == 'debt_free':
            target = max(g.start_debt or 0, debt_now)
            current = target - debt_now
            if p is not None and p.debt_free_on is not None:
                done_on = p.debt_free_on
                months_left = p.result.months
            reached = debt_now == 0
        else:
            target = (g.target or 0) if g.kind == 'savings' else bills * max(1, g.months)
            current = g.saved
            reached = target > 0 and current >= target
        pct = 100 if reached else (min(99, current * 100 // target) if target > 0 else 0)
        needed = None
        if g.by and not reached and g.kind != 'debt_free' and g.by > today:
            needed = -(-(target - current) // _months_until(today, g.by))
        out.append(GoalProgress(g, target, current, pct, reached, needed, months_left, done_on))
    return out


