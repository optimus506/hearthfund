"""Investments (D-102): what the investment accounts hold, and how that changed over time.

A summary only: balances by date, typed in or read from a linked account. Nothing here plans, advises or looks
inside an account, and the figures are no part of the forecast, the debts or the goals. A change over a period is
the difference between two balances, so it holds what was paid in or taken out as well as what the market did."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from .models import Account, AccountKind
from .money import Cents

__all__ = ['DAILY_DAYS', 'HISTORY_MAX', 'PERIODS', 'STALE_DAYS', 'WEEKLY_DAYS', 'Change', 'Summary', 'change',
           'forget', 'held', 'record', 'summary', 'thin', 'total_series', 'value_on']

HISTORY_MAX = 400      # balances kept per account
DAILY_DAYS = 45        # every balance of the last weeks is kept (a daily sync gives one a day)
WEEKLY_DAYS = 730      # then the last one of each week for two years, then the last one of each month
STALE_DAYS = 35        # a typed balance older than this is marked: time to look at the statement again
PERIODS: tuple[tuple[str, int | None], ...] = (('month', 30), ('quarter', 91), ('year', 365), ('all', None))

Points = tuple[tuple[date, Cents], ...]


@dataclass(frozen=True, slots=True)
class Change:
    since: date            # the date of the earlier balance
    was: Cents
    amount: Cents          # now minus then
    pct_bp: int | None     # of the earlier balance, in basis points; None when that was 0


@dataclass(frozen=True, slots=True)
class Summary:
    accounts: tuple[Account, ...]
    total: Cents
    as_of: date | None                                   # the oldest "as of" among the accounts: how fresh the total is
    series: Points                                       # the total by date, oldest first
    changes: tuple[tuple[str, Change | None], ...]       # per PERIODS, for the total
    by_account: tuple[tuple[tuple[str, Change | None], ...], ...]


def held(accounts: Sequence[Account]) -> tuple[Account, ...]:
    return tuple(a for a in accounts if a.kind is AccountKind.INVESTMENT)


def thin(points: Sequence[tuple[date, Cents]], today: date) -> Points:
    """One balance per date (the later one wins), oldest first: every date of the last DAILY_DAYS, then the last
    of each week, then (past WEEKLY_DAYS) the last of each month; the first balance ever is always kept, so "since
    the start" keeps its start. At most HISTORY_MAX, the newest."""
    by_date = dict(sorted(points))
    if not by_date:
        return ()
    first = min(by_date)
    kept: dict[tuple[int, ...], date] = {}
    for d in by_date:                                    # ascending, so a later date replaces its bucket's earlier one
        age = (today - d).days
        if age <= DAILY_DAYS:
            key: tuple[int, ...] = (0, d.toordinal())
        elif age <= WEEKLY_DAYS:
            key = (1, *d.isocalendar()[:2])
        else:
            key = (2, d.year, d.month)
        kept[key] = d
    days = sorted({first, *kept.values()})
    if len(days) > HISTORY_MAX:
        days = [first, *days[-(HISTORY_MAX - 1):]]
    return tuple((d, by_date[d]) for d in days)


def record(acct: Account, on: date, balance: Cents, today: date) -> Account:
    """The account with `balance` noted for `on`. The current balance is the one of the newest date, so typing in
    an older statement fills the history without changing what the account holds now."""
    hist = dict(acct.history)
    if acct.as_of is not None:
        hist.setdefault(acct.as_of, acct.balance)
    hist[on] = balance
    pts = thin(tuple(hist.items()), today)
    last_day, last = pts[-1]
    return replace(acct, balance=last, as_of=last_day, history=pts)


def forget(acct: Account, on: date) -> Account:
    """Take one dated balance out (a typing mistake). The last one stays: an account always has a balance."""
    pts = tuple(p for p in acct.history if p[0] != on)
    if not pts or len(pts) == len(acct.history):
        return acct
    return replace(acct, balance=pts[-1][1], as_of=pts[-1][0], history=pts)


def value_on(points: Sequence[tuple[date, Cents]], day: date) -> Cents | None:
    """The balance on `day`: the last one noted on or before it. None before the first."""
    out = None
    for d, v in points:
        if d > day:
            break
        out = v
    return out


def _points(a: Account) -> Points:
    hist = dict(a.history)
    if a.as_of is not None:
        hist.setdefault(a.as_of, a.balance)
    return tuple(sorted(hist.items()))


def total_series(accounts: Sequence[Account]) -> Points:
    """The sum of the accounts on every date one of them has a balance for. An account counts from its first
    balance on, so the total steps up on the day an account is added: that is money that was there all along."""
    all_points = [_points(a) for a in accounts]
    days = sorted({d for pts in all_points for d, _ in pts})
    return tuple((day, sum(value_on(pts, day) or 0 for pts in all_points)) for day in days)


def change(points: Sequence[tuple[date, Cents]], today: date, days: int | None) -> Change | None:
    """From the balance `days` ago (None: the first one) to the newest. None when there is no balance that far
    back, or only one balance."""
    if len(points) < 2:
        return None
    now = points[-1][1]
    if days is None:
        since, was = points[0]
    else:
        cut = today - timedelta(days=days)
        at = [(d, v) for d, v in points if d <= cut]
        if not at:
            return None
        since, was = at[-1]
    if since == points[-1][0]:
        return None
    return Change(since, was, now - was, round((now - was) * 10000 / was) if was > 0 else None)


def summary(accounts: Sequence[Account], today: date) -> Summary:
    mine = held(accounts)
    series = total_series(mine)
    dates = [a.as_of for a in mine if a.as_of is not None]
    return Summary(
        accounts=mine, total=sum(a.balance for a in mine), as_of=min(dates) if dates else None, series=series,
        changes=tuple((name, change(series, today, days)) for name, days in PERIODS),
        by_account=tuple(tuple((name, change(_points(a), today, days)) for name, days in PERIODS) for a in mine))
