"""Alerts for the summary strip and the report (ARCHITECTURE section 8)."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from .dates import add_months, week_start
from .forecast import Forecast
from .models import BankTxn, Budget, CapRule, Kind, LedgerRow, RowState, RuleKind, Severity, Status, row_status
from .money import Cents, fmt
from .rules import classify

if TYPE_CHECKING:
    from .reconcile import Applied

__all__ = ['Alert', 'cap_totals', 'evaluate', 'payee_key', 'period_start', 'snoozed', 'unscheduled']


@dataclass(frozen=True, slots=True)
class Alert:
    code: str
    severity: Severity
    message: str
    row_id: str | None = None
    data: tuple[tuple[str, str], ...] = ()


def _d(d: date) -> str:
    return '%02d/%02d/%d' % (d.month, d.day, d.year)


def payee_key(description: str) -> str:
    """Masked payee text with digits and '#' removed, first 3 words: how recurring unmatched payees are grouped."""
    words = re.sub(r'[\d#]+', ' ', description.upper()).split()
    return ' '.join(words[:3])


def period_start(d: date, period: str, week_starts: int) -> date:
    return week_start(d, week_starts) if period == 'week' else d.replace(day=1)


def cap_totals(budget: Budget, history: Sequence[BankTxn], *, today: date) -> list[tuple[CapRule, Cents, Cents]]:
    """For each cap rule: (rule, total in the current period, average per period over the history)."""
    out: list[tuple[CapRule, Cents, Cents]] = []
    ws = budget.settings.week_starts
    for cap in budget.settings.alerts.caps:
        per: dict[date, Cents] = defaultdict(int)
        for t in history:
            rule = classify(t, budget.rules)
            if rule is not None and rule.id in cap.rule_ids and t.amount < 0:
                per[period_start(t.posted, cap.period, ws)] += -t.amount
        current = per.get(period_start(today, cap.period, ws), 0)
        if per:
            # the average runs from the period of the earliest posting in the history, so quiet periods count
            first = period_start(min(t.posted for t in history), cap.period, ws)
            n, p = 0, first
            last = period_start(today, cap.period, ws)
            while p <= last:
                n += 1
                p = p + timedelta(days=7) if cap.period == 'week' else add_months(p, 1)
            avg = (sum(per.values()) + n // 2) // n
        else:
            avg = 0
        out.append((cap, current, avg))
    return out


def unscheduled(budget: Budget, *, today: date) -> list[LedgerRow]:
    """Open bills due within the alert window (settings.alerts.unscheduled_days) that are neither scheduled nor on
    autopay, in due-date order: the "unscheduled" alerts and the summary strip's "Not scheduled". Bills paid with a
    credit card are left out: nothing is scheduled from checking for them (the card's own payment row is)."""
    soon = today + timedelta(days=budget.settings.alerts.unscheduled_days)
    by_id = budget.series_by_id()
    return [r for r in sorted(budget.rows, key=lambda r: (r.due_date, r.order_date, r.seq))
            if r.state is RowState.OPEN and r.kind is Kind.BILL and r.amount != 0 and r.due_date <= soon
            and not r.card and row_status(r, by_id) is Status.NONE]


RECENT_MONTHS = 12          # repeating-payee alerts look at this many calendar months, this one included
QUIET_DAYS = 60             # ...and drop a payee with nothing posted for this long (#104)
BILL_WORDS = ('BILL PAYMT', 'BILL PAY', 'AUTOPAY', 'EPAYMENT', 'E-PAYMENT')


def _month(d: date) -> int:
    return d.year * 12 + d.month - 1


def snoozed(budget: Budget, code: str, key: str, months: Iterable[int]) -> bool:
    """An alert the household set aside stays away until the payee posts in a month after the one it was set
    aside in ("ignore until it posts again", #104)."""
    for c, k, on in budget.settings.alert_snoozes:
        if c == code and k == key:
            return not any(m > _month(on) for m in months)
    return False


def _similar(amounts: Sequence[Cents]) -> bool:
    mid = sorted(amounts)[len(amounts) // 2]
    return mid > 0 and all(abs(x - mid) * 4 <= mid for x in amounts)       # within 25% of the median


def _repeating(budget: Budget, history: Sequence[BankTxn], today: date) -> list[Alert]:
    """Payees that keep coming back without a budget line, in the last RECENT_MONTHS and posted within QUIET_DAYS:
    - unmatched_recurring: no payee rule at all, in two or more months (#104);
    - unbudgeted: money out that only a `track` rule catches, at a similar amount in consecutive months (three, or
      two when the bank calls it a bill payment) (#95). A track rule is for spending; a steady monthly payment
      it catches is usually a bill the forecast leaves out."""
    first = _month(today) - RECENT_MONTHS + 1
    loose: dict[str, set[int]] = defaultdict(set)
    tracked: dict[str, dict[int, Cents]] = defaultdict(lambda: defaultdict(int))
    billish: set[str] = set()
    last: dict[str, date] = {}
    for t in history:
        if not first <= _month(t.posted) <= _month(today):
            continue
        key = payee_key(t.description)
        if not key:
            continue
        rule = classify(t, budget.rules)
        if rule is None:
            loose[key].add(_month(t.posted))
        elif rule.kind is RuleKind.TRACK and t.amount < 0:
            tracked[key][_month(t.posted)] += -t.amount
            if any(w in t.description.upper() for w in BILL_WORDS):
                billish.add(key)
        else:
            continue
        last[key] = max(last.get(key, t.posted), t.posted)
    out: list[Alert] = []
    fresh = {k for k, d in last.items() if (today - d).days <= QUIET_DAYS}
    for key in sorted(k for k, v in loose.items() if len(v) >= 2 and k in fresh):
        if not snoozed(budget, 'unmatched_recurring', key, loose[key]):
            out.append(Alert('unmatched_recurring', Severity.INFO, '"%s" shows up in %d of the last %d months with no '
                             'payee rule.' % (key, len(loose[key]), RECENT_MONTHS), data=(('payee', key),)))
    for key in sorted(k for k in tracked if k in fresh):
        by_month = tracked[key]
        run = [max(by_month)]                          # the consecutive months ending with the latest one
        while run[-1] - 1 in by_month:
            run.append(run[-1] - 1)
        need = 2 if key in billish else 3
        amounts = [by_month[m] for m in run]
        if len(run) < need or not _similar(amounts) or snoozed(budget, 'unbudgeted', key, by_month):
            continue
        typical = sorted(amounts)[len(amounts) // 2]
        out.append(Alert('unbudgeted', Severity.INFO, '"%s" was paid %d months in a row (about %s) but is only '
                         'tracked, not budgeted.' % (key, len(run), fmt(typical)),
                         data=(('payee', key), ('amount', str(typical)))))
    return out


def evaluate(budget: Budget, forecast: Forecast, *, today: date, recent: Applied | None = None,
             history: Sequence[BankTxn] = ()) -> list[Alert]:
    a = budget.settings.alerts
    out: list[Alert] = []
    if forecast.low < 0:
        out.append(Alert('below_zero', Severity.BAD, 'The forecast drops below $0: %s on %s.'
                         % (fmt(forecast.low), _d(forecast.low_date)), forecast.low_row_id))
    elif forecast.low < a.cushion:
        out.append(Alert('cushion', Severity.WARN, 'The forecast low of %s on %s is under the %s cushion.'
                         % (fmt(forecast.low), _d(forecast.low_date), fmt(a.cushion)), forecast.low_row_id))

    if recent is not None and recent.result is not None:
        res = recent.result
        txns = {t.id: t for t in res.prepared.txns}
        before = {r.id: r for r in (recent.before or recent.budget).rows}
        allowance = {x.id for x in recent.budget.rules if x.kind is RuleKind.ALLOWANCE}   # many payments a row
        pairs = [(m.txn_id, m.row_id) for m in res.auto if m.rule_id not in allowance]
        props = {p.id: p for p in res.proposals}
        pairs += [(props[pid].txn_id, props[pid].row_ids[0]) for pid, how in recent.accepted
                  if how == 'clear' and props[pid].kind in ('ask', 'tolerance') and len(props[pid].row_ids) == 1]
        alert_by_series = {x.series_id: x for x in a.income}
        for txn_id, row_id in pairs:
            t, r = txns[txn_id], before[row_id]
            posted, budgeted = abs(t.amount), abs(r.amount)
            off = abs(posted - budgeted) * 10000 > a.bill_variance_bp * budgeted
            if r.kind is not Kind.INCOME and budgeted and off:
                pct = (posted - budgeted) * 100 / budgeted
                out.append(Alert('bill_variance', Severity.WARN, '%s posted at %s, budget %s (%+.0f%%).'
                                 % (r.name, fmt(posted), fmt(budgeted), pct), r.id))
            ia = alert_by_series.get(r.series_id) if r.kind is Kind.INCOME and r.series_id else None
            if ia is not None and ia.short_by is not None and posted < budgeted - ia.short_by:
                out.append(Alert('income_check_short', Severity.WARN, '%s deposit of %s is %s short of the budget %s.'
                                 % (r.name, fmt(posted), fmt(budgeted - posted), fmt(budgeted)), r.id))
        for u in res.unknown:
            t = txns[u.txn_id]
            out.append(Alert('unmatched', Severity.WARN, 'Unmatched bank item: %d/%d %s %s (%s).'
                             % (t.posted.month, t.posted.day, fmt(t.amount), t.description[:40], u.reason)))

    if a.unmatched_recurring and history:
        out += _repeating(budget, history, today)

    hist = {t.id: t for t in history}
    for ia in a.income:
        cleared = sorted((r for r in budget.rows if r.series_id == ia.series_id and r.state is RowState.CLEARED
                          and r.cleared_txn_id in hist), key=lambda r: r.due_date)[-ia.window:]
        if not cleared:
            continue
        posted = sum(abs(hist[r.cleared_txn_id].amount) for r in cleared if r.cleared_txn_id)
        budgeted = sum(abs(r.amount) for r in cleared)
        if budgeted and posted * 10000 < ia.min_avg_bp * budgeted:
            n = len(cleared)
            out.append(Alert('income_short', Severity.WARN, '%s deposits averaged %s over the last %d, under %s%% of '
                             'the %s budget.' % (cleared[-1].name, fmt(posted // n), n,
                                                 format(ia.min_avg_bp / 100, 'g'), fmt(budgeted // n))))

    for cap, total, avg in cap_totals(budget, history, today=today):
        if total > cap.amount:
            out.append(Alert('cap', Severity.WARN, '%s: %s this %s, over the %s cap (average %s per %s).'
                             % (cap.name, fmt(total), cap.period, fmt(cap.amount), fmt(avg), cap.period),
                             data=(('cap', cap.id),)))

    for r in unscheduled(budget, today=today):
        out.append(Alert('unscheduled', Severity.WARN, '%s (%d/%d, %s) is due within %d days and not scheduled.'
                         % (r.name, r.due_date.month, r.due_date.day, fmt(r.amount), a.unscheduled_days), r.id))
    late = today - timedelta(days=a.expected_grace_days)
    for e in budget.expected:
        if e.status == 'open' and e.date < late:
            out.append(Alert('expected_late', Severity.INFO, '%s was expected on %s and has not posted. Mark it done '
                             'if it went through.' % (e.label, _d(e.date)), data=(('expected', e.id),)))
    return out
