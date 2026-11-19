"""Keeping debt balances current (D-105): which typed balances are due for a look at the statement, and
which promo rates end soon.

A balance a linked bank account keeps gets a new date at every sync, so it never comes up here while the link
works; when the link breaks, its date ages like a typed one and the reminder covers it."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from .models import Account, Budget
from .payoff import NOT_DEBTS

__all__ = ['PROMO_DAYS', 'SNOOZE', 'SNOOZE_DAYS', 'due', 'promos_ending', 'snoozed_until', 'stale']

SNOOZE = ('debt_balances', 'until')     # the (alert, key) under which settings.alert_snoozes keeps "remind me later"
SNOOZE_DAYS = 14
PROMO_DAYS = 60                         # a promo rate ending within this many days is pointed out


def stale(accounts: Sequence[Account], today: date, every_days: int) -> tuple[Account, ...]:
    """Debts that still owe something whose balance is older than `every_days` (or has no date); none when the
    reminder is off (0). Oldest first."""
    if every_days <= 0:
        return ()
    cut = today - timedelta(days=every_days)
    old = [a for a in accounts if a.kind not in NOT_DEBTS and a.balance > 0 and (a.as_of is None or a.as_of < cut)]
    return tuple(sorted(old, key=lambda a: (a.as_of or date.min, a.name)))


def snoozed_until(budget: Budget) -> date | None:
    return next((on for c, k, on in budget.settings.alert_snoozes if (c, k) == SNOOZE), None)


def due(budget: Budget, today: date) -> tuple[Account, ...]:
    """What the reminder lists today: the stale debts, unless it was put off until a later day."""
    until = snoozed_until(budget)
    if until is not None and today < until:
        return ()
    return stale(budget.accounts, today, budget.settings.alerts.debt_update_days)


def promos_ending(accounts: Sequence[Account], today: date) -> tuple[Account, ...]:
    """Debts with a balance whose promo rate ends within PROMO_DAYS, soonest first."""
    end = today + timedelta(days=PROMO_DAYS)
    soon = [a for a in accounts if a.kind not in NOT_DEBTS and a.balance > 0 and a.promo_ends is not None
            and today <= a.promo_ends <= end]
    return tuple(sorted(soon, key=lambda a: (a.promo_ends or today, a.name)))
