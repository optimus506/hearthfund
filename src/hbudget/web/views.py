"""Core objects -> JSON for the API. Money stays integer cents; dates are ISO strings."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from hbudget.core import alerts as alerts_mod
from hbudget.core import forecast as fc
from hbudget.core import paydays
from hbudget.core import payoff as po
from hbudget.core.alerts import Alert
from hbudget.core.cards import OTHER, CardSummary, Period, Store, store
from hbudget.core.checkup import CheckUp
from hbudget.core.flow import Flow
from hbudget.core.forecast import Forecast
from hbudget.core.ledger import ordered
from hbudget.core.models import (
    Account,
    BankTxn,
    Budget,
    CardCategory,
    CardTxn,
    ExpectedItem,
    FlowGroup,
    Kind,
    LedgerRow,
    PayeeRule,
    RowState,
    Series,
    row_key,
    row_status,
)
from hbudget.core.paydays import CardTransfer
from hbudget.core.payoff import PayoffPlan, PayoffResult
from hbudget.core.recap import Recap
from hbudget.core.recap import Totals as RecapTotals
from hbudget.core.reconcile import Prepared, ReconcileResult
from hbudget.core.subscriptions import Recurring, RecurringSummary

J = dict[str, Any]


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def forecast_readout(f: Forecast, *, today: date) -> J:
    ye = next(((y, c) for y, c in f.year_ends if y == today.year), f.year_ends[-1] if f.year_ends else None)
    return {'start': f.start, 'now': f.now, 'low': f.low, 'low_date': iso(f.low_date), 'high': f.high,
            'high_date': iso(f.high_date), 'end': f.end, 'end_date': iso(f.end_date),
            'year_end': {'year': ye[0], 'balance': ye[1]} if ye else None,
            'year_ends': [[y, c] for y, c in f.year_ends],
            'chart': chart(f)}


def chart(f: Forecast) -> list[list[Any]]:
    """[date, balance after that date, the last row's name, rows on that date] per point."""
    return [[d.isoformat(), c, name, n] for d, c, name, n in fc.chart_points(f)]


def alert(a: Alert) -> J:
    return {'code': a.code, 'severity': str(a.severity), 'message': a.message, 'row_id': a.row_id,
            'data': dict(a.data)}


def summary(budget: Budget, f: Forecast, alert_list: list[Alert], *, today: date) -> J:
    out = forecast_readout(f, today=today)
    out.pop('chart')
    due = alerts_mod.unscheduled(budget, today=today)
    out.update({'balance': budget.start_balance, 'as_of': iso(budget.as_of),
                'reconciled_through': iso(budget.last_reconciled_through),
                'cushion': budget.settings.alerts.cushion, 'alerts': [alert(a) for a in alert_list],
                # the strip's "Not scheduled": bills due within the alert window with no status yet
                'unscheduled': {'count': len(due), 'total': sum(r.amount for r in due),
                                'days': budget.settings.alerts.unscheduled_days},
                'expected_open': sum(1 for e in budget.expected if e.status == 'open')})
    return out


def compute_summary(budget: Budget, history: list[BankTxn], *, today: date) -> J:
    f = fc.compute(budget, today=today)
    return summary(budget, f, alerts_mod.evaluate(budget, f, today=today, history=history), today=today)


def home(budget: Budget, history: list[BankTxn], *, today: date, days: int = 60) -> J:
    """Home: the summary, the next `days` of the balance chart, and the pay periods (core.paydays)."""
    f = fc.compute(budget, today=today)
    s = summary(budget, f, alerts_mod.evaluate(budget, f, today=today, history=history), today=today)
    by_id = budget.series_by_id()
    horizon = date.fromordinal(today.toordinal() + days).isoformat()
    pts = chart(f)
    head = [p for p in pts if p[0] <= horizon]
    # carry the balance to the horizon so the line ends at the right edge
    spark = head + ([[horizon, head[-1][1], None, 0]] if head and len(pts) > len(head) else [])
    per = paydays.periods(budget, f, today=today)
    return {'summary': s, 'spark': spark, 'today': today.isoformat(),
            'periods': [{'start': iso(p.start), 'end': iso(p.end), 'pay_in': p.pay_in, 'out': p.out,
                         'balance_end': p.balance_end, 'low': p.low,
                         'rows': [row(r, by_id, None, today=today) for r in p.rows],
                         'transfers': [card_transfer(t, budget) for t in p.transfers]} for p in per]}


def card_names(budget: Budget) -> dict[str, str]:
    return {a.id: a.name for a in budget.accounts}


def card_transfer(t: CardTransfer, budget: Budget) -> J:
    """What to move to one card (#31): its open card-funded rows and their total."""
    return {'card': t.card, 'card_name': card_names(budget).get(t.card, t.card), 'total': t.total,
            'rows': [{'id': r.id, 'name': r.name, 'due': iso(r.due_date), 'amount': r.amount} for r in t.rows]}


def row(r: LedgerRow, by_id: dict[str, Series], balance: int | None, *, today: date) -> J:
    s = by_id.get(r.series_id or '')
    return {'id': r.id, 'key': row_key(r), 'series_id': r.series_id, 'name': r.name, 'kind': str(r.kind),
            'amount': r.amount, 'due': iso(r.due_date), 'order': iso(r.order_date), 'state': str(r.state),
            'status': str(row_status(r, by_id)),
            'status_override': str(r.status_override) if r.status_override else None,
            'amount_was': r.amount_was, 'paid': r.paid, 'card': r.card, 'balance': balance,
            'spending': r.kind is Kind.SPENDING or (s is not None and s.kind is Kind.SPENDING),
            'late': r.state is RowState.OPEN and r.due_date < today, 'note': r.note}


def ledger(budget: Budget, f: Forecast, *, today: date, start: date | None = None, end: date | None = None) -> J:
    """Open and removed rows in ledger order with the balance after each (removed rows carry the running balance).
    Rows marked already paid by hand since the last reconciliation are listed too, like removed ones, so they can be
    undone; rows cleared against a bank posting are not."""
    by_id = budget.series_by_id()
    bal = {p.row_id: p.balance for p in f.points if p.row_id}
    running = budget.start_balance
    since = budget.last_reconciled_through
    rows = []
    for r in ordered(budget.rows):
        if r.state is RowState.CLEARED and (r.cleared_txn_id is not None or r.cleared_on is None
                                            or (since is not None and r.cleared_on < since)):
            continue
        if r.state is RowState.OPEN:
            running = bal.get(r.id, running)
        if (start and r.order_date < start) or (end and r.order_date > end):
            continue
        rows.append(row(r, by_id, running, today=today))
    return {'rows': rows, 'start': budget.start_balance, 'today': today.isoformat(), 'low': f.low,
            'cushion': budget.settings.alerts.cushion, 'chart': chart(f), 'cards': card_names(budget),
            'series': [{'id': s.id, 'name': s.name, 'kind': str(s.kind), 'status': str(s.status), 'notes': s.notes}
                       for s in budget.series]}


def series(s: Series) -> J:
    c = s.schedule
    return {'id': s.id, 'name': s.name, 'kind': str(s.kind), 'amount': s.amount,
            'schedule': {'cadence': str(c.cadence), 'start': iso(c.start), 'end': iso(c.end), 'interval': c.interval,
                         'day': c.day, 'days': list(c.days) if c.days else None, 'month': c.month,
                         'weekend': str(c.weekend)},
            'funded_by': s.funded_by, 'status': str(s.status), 'card': s.card, 'debt': s.debt, 'group': s.group,
            'month_amounts': [[_months(m), a] for m, a in s.month_amounts], 'cycle_amounts': list(s.cycle_amounts),
            'active': s.active, 'notes': s.notes, 'generated_through': iso(s.generated_through)}


def _months(m: object) -> str:
    return str(m)


def expected(e: ExpectedItem) -> J:
    return {'id': e.id, 'label': e.label, 'amount': e.amount, 'direction': e.direction, 'date': iso(e.date),
            'status': e.status}


def account(a: Account, est: po.Estimate | None = None) -> J:
    return {'id': a.id, 'name': a.name, 'kind': str(a.kind), 'balance': a.balance, 'apr_bp': a.apr_bp,
            'estimate': None if est is None else {'balance': est.balance, 'since': iso(est.since), 'paid': est.paid,
                                                  'payments': est.payments, 'interest': est.interest},
            'payment': a.payment, 'minimum': a.minimum, 'limit': a.limit, 'as_of': iso(a.as_of),
            'promo_ends': iso(a.promo_ends), 'apr_after_bp': a.apr_after_bp, 'deferred_interest': a.deferred_interest,
            'is_mortgage': a.is_mortgage, 'include_in_payoff': a.include_in_payoff, 'notes': a.notes,
            'utilization_bp': po.utilization(a), 'monthly_interest': po.monthly_interest(a),
            'history': [[iso(d), v] for d, v in a.history]}


def _result(r: PayoffResult) -> J:
    return {'months': r.months, 'done': r.done, 'total_interest': r.total_interest, 'balances': list(r.balances),
            'debts': [{'id': d.id, 'name': d.name, 'paid_month': d.paid_month, 'paid_on': iso(d.paid_on),
                       'interest': d.interest, 'balance_at_promo': d.balance_at_promo} for d in r.debts]}


def payoff(p: PayoffPlan) -> J:
    return {'method': p.method, 'result': _result(p.result), 'other': _result(p.other),
            'baseline': _result(p.baseline), 'start': iso(p.start), 'debt_free_on': iso(p.debt_free_on),
            'interest_saved': p.interest_saved, 'months_sooner': p.months_sooner,
            'other_difference': p.other_difference, 'other_debt_free_on': iso(p.other_debt_free_on),
            'promos': [{'debt_id': v.debt_id, 'name': v.name, 'promo_ends': iso(v.promo_ends),
                        'paid_before': v.paid_before, 'left_at_promo': v.left_at_promo,
                        'monthly_needed': v.monthly_needed, 'payments_left': v.payments_left,
                        'deferred_interest': v.deferred_interest, 'apr_after_missing': v.apr_after_missing}
                       for v in p.promos],
            'not_covering': list(p.not_covering), 'excluded': list(p.excluded)}


LEFT_OVER = 'Left over'   # the group core.flow adds for money in that was not spent


def pct(part: int, whole: int) -> int | None:
    """part as a whole percentage of whole, rounded half up (None when whole is not positive)."""
    return (200 * part + whole) // (2 * whole) if whole > 0 else None


def flow(f: Flow) -> J:
    """The Sankey data plus the totals the page shows; the page does no money maths of its own."""
    spent = [g for g in f.groups if not (g.name == LEFT_OVER and not g.items)]
    money_in = sum(s.amount for s in f.sources)
    money_out = sum(g.total for g in spent)
    left, short, n = max(0, money_in - money_out), max(0, money_out - money_in), max(1, f.months)
    biggest = max(spent, key=lambda g: g.total, default=None)   # groups come largest first; ties keep that order
    return {'start': iso(f.start), 'end': iso(f.end), 'months': f.months, 'note': f.note, 'complete': f.complete,
            'sources': [{'name': s.name, 'amount': s.amount, 'pct': pct(s.amount, money_in)} for s in f.sources],
            'groups': [{'name': g.name, 'colour': g.colour, 'total': g.total, 'pct': pct(g.total, money_out),
                        'items': [{'name': i.name, 'amount': i.amount} for i in g.items]} for g in spent],
            'totals': {'money_in': money_in, 'money_out': money_out, 'left_over': left, 'short': short,
                       'pay': f.pay, 'pay_covers_pct': pct(f.pay, money_out),
                       'left_over_pct': pct(left, money_in),
                       'biggest': {'name': biggest.name, 'colour': biggest.colour, 'total': biggest.total,
                                   'per_month': biggest.total // n, 'pct': pct(biggest.total, money_out)}
                       if biggest else None,
                       'per_month': {'money_in': money_in // n, 'money_out': money_out // n, 'left_over': left // n,
                                     'short': short // n, 'pay': f.pay // n}}}


def checkup(c: CheckUp, budget: Budget) -> J:
    caps = {x.id: x for x in budget.settings.alerts.caps}   # core.checkup keys cap_weeks by cap id
    return {'since': iso(c.since), 'through': iso(c.through), 'transactions': c.transactions,
            'bills': [{'rule_id': b.rule_id, 'name': b.name, 'targets': list(b.targets), 'kind': str(b.kind),
                       'budget': b.budget, 'per': b.per, 'actuals': [[iso(d), v] for d, v in b.actuals],
                       'count': b.count, 'average': b.average,
                       'diff_pct': None if b.diff_tenths_pct is None else b.diff_tenths_pct / 10,
                       'verdict': b.verdict} for b in c.bills],
            'cap_weeks': [_cap(cap_id, periods, caps.get(cap_id)) for cap_id, periods in c.cap_weeks],
            'tracked': [{'rule_id': rid, 'name': name, 'weeks': [[iso(d), v] for d, v in weeks]}
                        for rid, name, weeks in c.tracked]}


def _cap(cap_id: str, periods: Any, rule: Any) -> J:
    return {'id': cap_id, 'name': rule.name if rule else cap_id, 'period': rule.period if rule else 'week',
            'amount': rule.amount if rule else 0,
            'periods': [[iso(d), v] for d, v in periods]}


def card_summary(s: CardSummary) -> J:
    return {'categories': list(s.categories),
            'weeks': [{'week': iso(w.week), 'actual': dict(w.actual), 'budget': dict(w.budget)} for w in s.weeks],
            'since': iso(s.since), 'through': iso(s.through),
            'weeks_averaged': s.full_weeks, 'weeks_shown': len(s.weeks),   # averages use finished weeks only
            'avg_per_week': dict(s.avg_per_week), 'budget_per_week': dict(s.budget_per_week),
            'weeks_over': s.weeks_over, 'monthly_spend': s.monthly_spend,
            'by_category': [[n, v] for n, v in s.by_category]}


def category(c: CardCategory) -> J:
    return {'name': c.name, 'keywords': list(c.keywords), 'bank_categories': list(c.bank_categories),
            'weekly_series': list(c.weekly_series), 'has_own_rows': c.has_own_rows, 'merchants': list(c.merchants)}


def card_store(s: Store) -> J:
    return {'store': s.name, 'category': s.category, 'spent': s.total, 'purchases': s.count, 'last': iso(s.last),
            'moved': s.moved}


def card_period(p: Period) -> J:
    return {'start': iso(p.start), 'spent': dict(p.spent), 'total': sum(v for _, v in p.spent),
            'budget': dict(p.budget)}


def card_purchase(t: CardTxn) -> J:
    return {'id': t.id, 'purchased': iso(t.purchased), 'posted': iso(t.posted), 'amount': -t.amount,
            'description': t.description, 'store': store(t.description), 'category': t.category or OTHER,
            'by_hand': t.by_hand}


def rule(r: PayeeRule) -> J:
    return {'id': r.id, 'name': r.name, 'kind': str(r.kind), 'contains': list(r.contains), 'regex': r.regex,
            'direction': str(r.direction), 'min_amount': r.min_amount, 'max_amount': r.max_amount,
            'targets': list(r.targets), 'card': r.card, 'note': r.note}


def flow_group(g: FlowGroup) -> J:
    return {'name': g.name, 'colour': g.colour, 'rule_ids': list(g.rule_ids), 'series_ids': list(g.series_ids),
            'row_names': list(g.row_names)}


def txn(t: BankTxn) -> J:
    return {'id': t.id, 'posted': iso(t.posted), 'amount': t.amount, 'description': t.description,
            'balance': t.balance}


def preview(budget: Budget, prepared: Prepared, result: ReconcileResult) -> J:
    rows = {r.id: r for r in budget.rows}
    txns = {t.id: t for t in prepared.txns}
    rules = {r.id: r for r in budget.rules}
    by_id = budget.series_by_id()

    def rj(rid: str) -> J:
        return row(rows[rid], by_id, None, today=budget.as_of)
    return {
        'basis': txn(prepared.basis) if prepared.basis else None, 'basis_ambiguous': prepared.basis_ambiguous,
        'new_count': len(prepared.new), 'through': iso(prepared.through), 'latest_balance': prepared.latest_balance,
        'balance_checked': prepared.balance_checked, 'ignored': prepared.ignored,
        'auto': [{'txn': txn(txns[a.txn_id]), 'row': rj(a.row_id), 'rule': a.rule_id, 'amount': a.amount}
                 for a in result.auto],
        'proposals': [{'id': p.id, 'kind': p.kind, 'note': p.note, 'txn': txn(txns[p.txn_id]) if p.txn_id else None,
                       'rows': [rj(x) for x in p.row_ids if x in rows], 'total': p.total, 'group': p.group,
                       'can_reduce': p.can_reduce, 'expected_id': p.expected_id} for p in result.proposals],
        'tracked': [{'txn': txn(txns[t.txn_id]), 'rule': rules[t.rule_id].name if t.rule_id in rules else t.rule_id,
                     'kind': str(t.kind)} for t in result.tracked],
        'unknown': [{'txn': txn(txns[u.txn_id]), 'reason': u.reason} for u in result.unknown],
    }


def recurring(s: RecurringSummary, b: Budget) -> J:
    """Recurring charges (#34): the budget line a rule links each one to is named when there is one."""
    names = {x.id: x.name for x in b.series}

    def item(i: Recurring) -> J:
        return {'id': i.id, 'payee': i.payee, 'source': i.source, 'cadence': i.cadence, 'amount': i.amount,
                'monthly': i.monthly, 'first': iso(i.first), 'last': iso(i.last), 'next': iso(i.next),
                'count': i.count, 'status': i.status,
                'price_changes': [{'from': iso(d), 'old': o, 'new': n} for d, o, n in i.price_changes],
                'rule': {'id': i.rule_id, 'name': i.rule_name} if i.rule_id else None,
                'budget_lines': [{'id': t, 'name': names.get(t, t)} for t in i.targets], 'category': i.category,
                'group': i.group, 'generic': i.generic,
                'latest': {'date': iso(i.latest[0]), 'amount': i.latest[1]} if i.latest else None}
    return {'monthly_total': s.monthly_total, 'yearly_total': s.yearly_total, 'price_rises': s.price_rises,
            'bills_monthly': s.bills_monthly, 'subscriptions_monthly': s.subscriptions_monthly,
            'items': [item(i) for i in s.items], 'hidden': [item(i) for i in s.hidden]}


def calendar(budget: Budget, f: Forecast, month: date, *, today: date) -> J:
    """The cash-flow calendar for one month: every day with what moves (forecast items, expected and virtual ones
    included) and the balance at the end of the day. Late rows count on the balance date, as on the chart."""
    from calendar import monthrange

    from hbudget.core.dates import add_months
    first = month.replace(day=1)
    last = first.replace(day=monthrange(first.year, first.month)[1])
    rows = {r.id: r for r in budget.rows}
    cushion = budget.settings.alerts.cushion
    balance = f.start
    by_day: dict[date, list[J]] = {}
    end_of: dict[date, int] = {}
    for p in f.points:
        d = max(p.order_date, f.as_of)
        if d < first:
            balance = p.balance
            continue
        if d > last:
            break
        r = rows.get(p.row_id or '')
        by_day.setdefault(d, []).append({
            'label': p.label, 'amount': p.amount, 'row_id': p.row_id, 'due': iso(p.due_date),
            'kind': str(r.kind) if r else ('virtual' if p.label.startswith('Move to ') else 'expected'),
            'late': p.due_date < today and r is not None, 'card': r.card if r else None})
        end_of[d] = p.balance
    opening = balance
    days: list[J] = []
    for n in range(last.day):
        d = first.replace(day=n + 1)
        balance = end_of.get(d, balance)
        days.append({'date': d.isoformat(), 'entries': by_day.get(d, []), 'balance': balance,
                     'below_cushion': balance < cushion, 'below_zero': balance < 0, 'past': d < f.as_of})
    future = [x for x in days if not x['past']]
    low = min(future, key=lambda x: x['balance']) if future else None
    horizon_last = max((p.order_date for p in f.points), default=f.as_of)
    prev_m, next_m = add_months(first, -1), add_months(first, 1)
    return {'month': first.isoformat()[:7], 'today': today.isoformat(), 'as_of': f.as_of.isoformat(),
            'week_starts': budget.settings.week_starts, 'cushion': cushion, 'opening': opening,
            'closing': days[-1]['balance'],
            'money_in': -sum(e['amount'] for x in days for e in x['entries'] if e['amount'] < 0),
            'money_out': sum(e['amount'] for x in days for e in x['entries'] if e['amount'] > 0),
            'low': {'date': low['date'], 'balance': low['balance']} if low else None,
            'prev': prev_m.isoformat()[:7] if prev_m >= f.as_of.replace(day=1) else None,
            'next': next_m.isoformat()[:7] if next_m <= horizon_last else None,
            'days': days}


def recap(r: Recap, months: list[date]) -> J:
    """The monthly recap or year in review (D-090), with the periods that have bank history for the picker."""
    def tot(t: RecapTotals) -> J:
        return {'money_in': t.money_in, 'money_out': t.money_out, 'net': t.net, 'pay': t.pay,
                'card_spent': t.card_spent}
    n = 4 if r.span == 'year' else 7
    keys = [d.isoformat()[:n] for d in months]
    here = r.start.isoformat()[:n]
    return {'span': r.span, 'period': here, 'start': iso(r.start), 'end': iso(r.end - timedelta(days=1)),
            'complete': r.complete, 'has_history': r.has_history, 'totals': tot(r.totals), 'before': tot(r.before),
            'last_balance': r.last_balance,
            'top_payees': [{'name': n, 'amount': a} for n, a in r.top_payees],
            'card': [{'category': c, 'spent': s, 'budget': b} for c, s, b in r.card],
            'bills_paid': [{'name': n, 'due': iso(d), 'amount': a} for n, d, a in r.bills_paid],
            'bills_open': [{'name': n, 'due': iso(d), 'amount': a} for n, d, a in r.bills_open],
            'new_recurring': [{'payee': x.payee, 'amount': x.amount, 'cadence': x.cadence, 'first': iso(x.first)}
                              for x in r.new_recurring],
            'price_changes': [{'payee': x.payee, 'from': iso(d), 'old': o, 'new': n} for x, d, o, n in r.price_changes],
            'wins': [{'kind': w.kind, 'text': w.text, 'on': iso(w.on)} for w in r.wins],
            'periods': keys,
            'prev': max((k for k in keys if k < here), default=None),
            'next': min((k for k in keys if k > here), default=None)}
