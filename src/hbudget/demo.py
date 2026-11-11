"""The demo household: a made-up two-earner family with round amounts, dated relative to today.

`demo_budget(today)` gives the budget alone; `install(conn, hid, today=..., user_id=...)` also adds about three months
of bank and card history (already reconciled) and one bank export waiting to be reconciled, so every tab has data and
a reconciliation can be tried at once. Every name, amount and description here is invented.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta

from hbudget.core import ledger
from hbudget.core.models import (
    Account,
    AccountKind,
    AlertSettings,
    BankTxn,
    Budget,
    Cadence,
    CapRule,
    CardCategory,
    CardTxn,
    Direction,
    ExpectedItem,
    FlowGroup,
    Horizon,
    Kind,
    PayeeRule,
    RowState,
    RuleKind,
    Schedule,
    Series,
    Settings,
    Status,
    Weekend,
)
from hbudget.core.recurrence import occurrences
from hbudget.io.bank import txn_id

__all__ = ['START_BALANCE', 'as_of_for', 'demo_budget', 'install']

START_BALANCE = 250000          # $2,500.00 in checking on the as-of date
HISTORY_DAYS = 91
CARD = 'card-a'
NOTES = ('Demo household: every name and amount here is made up.\n'
         'Try it: Reconcile > Preview on the waiting export, accept the proposals and apply.\n'
         'Then wipe it (Export > Delete all budget data) or create your own household.')

# bank descriptions of the made-up payees (also used by the payee rules below)
D_PAY_A, D_PAY_B = 'EMPLOYER A PAYROLL', 'EMPLOYER B PAYROLL'
D_MORTGAGE, D_ELECTRIC, D_INTERNET, D_CAR = 'HOME LOAN SERVICER', 'ELECTRIC CO ACH', 'INTERNET PROVIDER', 'AUTO LOAN'
D_WATER, D_CARD = 'CITY WATER', 'TRANSFER TO CARD 1234'
D_SAVINGS, D_ONLINE, D_KIOSK = 'TRANSFER TO SAVINGS', 'ONLINE MARKETPLACE', 'CORNER KIOSK'


def as_of_for(today: date) -> date:
    """The demo's checkbook date: ten days ago, so a bank export of the days since is waiting."""
    return today - timedelta(days=10)


def _saturday_on_or_after(d: date) -> date:
    return d + timedelta(days=(5 - d.weekday()) % 7)


def _series(as_of: date) -> tuple[Series, ...]:
    def monthly(day: int) -> Schedule:
        return Schedule(Cadence.MONTHLY, as_of, day=day)
    sat = _saturday_on_or_after(as_of)
    return (
        Series('pay-a', 'Pay A', Kind.INCOME, 240000,
               Schedule(Cadence.SEMIMONTHLY, as_of, days=(1, 15), weekend=Weekend.BEFORE)),
        Series('pay-b', 'Pay B', Kind.INCOME, 150000, Schedule(Cadence.MONTHLY, as_of, day=31, weekend=Weekend.BEFORE)),
        Series('mortgage', 'Mortgage', Kind.BILL, 180000, monthly(3), status=Status.AUTOPAY, group='Home'),
        Series('electric', 'Electric', Kind.BILL, 15000, monthly(6), status=Status.MANUAL, group='Utilities'),
        Series('internet', 'Internet', Kind.BILL, 8000, monthly(12), status=Status.AUTOPAY, group='Utilities'),
        Series('car-loan', 'Car loan', Kind.BILL, 35000, monthly(10), funded_by='pay-a', group='Debt payments'),
        Series('lawn-care', 'Lawn care', Kind.BILL, 12000, monthly(14), group='Home',
               month_amounts=tuple((m, 12000 if 5 <= m <= 9 else 4000) for m in range(1, 13))),
        Series('card-payment', 'Card A payment', Kind.BILL, 40000, monthly(25), status=Status.AUTOPAY, card=CARD,
               group='Debt payments'),
        Series('holidays', 'Holiday gifts', Kind.BILL, 50000, Schedule(Cadence.YEARLY, as_of, month=12, day=1),
               card=CARD, group='Family'),
        Series('grocery', 'Grocery', Kind.SPENDING, 16000, Schedule(Cadence.WEEKLY, sat), card=CARD,
               group='Food & everyday'),
        Series('fuel', 'Fuel', Kind.SPENDING, 5000, Schedule(Cadence.WEEKLY, sat), card=CARD, group='Food & everyday',
               cycle_amounts=(5000, 3500)),
    )


def _rules() -> tuple[PayeeRule, ...]:
    def rule(rid: str, name: str, kind: RuleKind, words: str, targets: tuple[str, ...] = (), **kw: object) -> PayeeRule:
        return PayeeRule(rid, name, kind, contains=(words,), targets=targets, **kw)  # type: ignore[arg-type]
    return (
        rule('payroll-a', 'Pay A', RuleKind.PAY, D_PAY_A, ('pay-a',), direction=Direction.IN),
        rule('payroll-b', 'Pay B', RuleKind.PAY, D_PAY_B, ('pay-b',), direction=Direction.IN),
        rule('mortgage', 'Mortgage', RuleKind.BILL, D_MORTGAGE, ('mortgage',)),
        rule('electric', 'Electric', RuleKind.BILL, 'ELECTRIC CO', ('electric',)),
        rule('internet', 'Internet', RuleKind.BILL, D_INTERNET, ('internet',)),
        rule('car-loan', 'Car loan', RuleKind.BILL, D_CAR, ('car-loan',)),
        rule('water', 'Water', RuleKind.BILL, D_WATER, ('Water',)),          # targets a one-off row by its name
        rule('card-a', 'Card A payment', RuleKind.CARD, D_CARD, card=CARD),
        rule('savings', 'To savings', RuleKind.TRACK, D_SAVINGS, direction=Direction.OUT),
        rule('online', 'Online shopping', RuleKind.TRACK, D_ONLINE, direction=Direction.OUT),
    )


def _accounts(today: date) -> tuple[Account, ...]:
    return (
        Account('checking', 'Checking', AccountKind.CHECKING, balance=START_BALANCE, as_of=today),
        Account(CARD, 'Card A', AccountKind.CREDIT_CARD, 300000, 2499, 40000, 9000, 800000, today),
        Account('car', 'Car loan', AccountKind.LOAN, 900000, 650, 35000, 35000, None, today),
        Account('furniture', 'Furniture store plan', AccountKind.CREDIT_CARD, 150000, 0, 12500, 4000, 250000, today,
                promo_ends=today + timedelta(days=200), apr_after_bp=2699, deferred_interest=True),
        Account('home', 'Mortgage', AccountKind.MORTGAGE, 24000000, 550, 180000, 180000, None, today, is_mortgage=True),
    )


def _categories() -> tuple[CardCategory, ...]:
    return (
        CardCategory('Grocery', ('MARKET', 'GROCERY'), weekly_series=('grocery',)),
        CardCategory('Fuel', ('FUEL',), weekly_series=('fuel',)),
        CardCategory('Eating out', ('BISTRO', 'CAFE', 'PIZZA')),
        CardCategory('Pharmacy', ('PHARMACY',)),
    )


def _groups() -> tuple[FlowGroup, ...]:
    return (
        FlowGroup('Home', '--f-home', ('mortgage',), ('mortgage', 'lawn-care')),
        FlowGroup('Debt payments', '--f-debt', ('car-loan',), ('car-loan', 'card-payment')),
        FlowGroup('Utilities', '--f-util', ('electric', 'internet', 'water'), ('electric', 'internet')),
        FlowGroup('Food & everyday', '--f-food', ('online',), ('grocery', 'fuel')),
        FlowGroup('Savings', '--f-savings', ('savings',)),
        FlowGroup('Family', '--f-family', (), ('holidays',)),
    )


def demo_budget(*, today: date) -> Budget:
    as_of = as_of_for(today)
    b = Budget(
        start_balance=START_BALANCE, as_of=as_of, horizon=Horizon('rolling', 15), series=_series(as_of), rows=(),
        rules=_rules(), accounts=_accounts(today),
        expected=(ExpectedItem('refund', 'Refund from a store', 2000, 'in', today + timedelta(days=5)),),
        settings=Settings(alerts=AlertSettings(cushion=50000, caps=(CapRule('online-weekly', 'Online shopping',
                                                                            ('online',), 'week', 5000),))),
        card_categories=_categories(), flow_groups=_groups(), last_reconciled_through=as_of - timedelta(days=1))
    b = ledger.generate(b, ledger.horizon_end(b))
    # a one-off water bill shortly after the as-of date: its posting in the waiting export is over the budget
    b = ledger.add_row(b, 'water-once', 'Water', Kind.BILL, 6000, as_of + timedelta(days=2))
    return b


# ---------------------------------------------------------------- made-up bank and card history

def _variation(i: int, base: int) -> int:
    """Small round variations so the check-up has something to show."""
    return base + (0, 1500, -1000, 2500)[i % 4]


def _history(budget: Budget, start: date, end: date) -> list[tuple[date, int, str]]:
    """Bank postings (bank sign: + in, - out) for the series' occurrences between start and end, plus card payments
    and transfers to savings."""
    out: list[tuple[date, int, str]] = []
    payee = {'pay-a': D_PAY_A, 'pay-b': D_PAY_B, 'mortgage': D_MORTGAGE, 'electric': D_ELECTRIC,
             'internet': D_INTERNET, 'car-loan': D_CAR}
    for s in budget.series:
        if s.id not in payee:
            continue
        sched = replace(s.schedule, start=start)
        for i, occ in enumerate(occurrences(sched, start, end)):
            amount = s.amount if s.id != 'electric' else _variation(i, s.amount)
            out.append((occ.actual, amount if s.kind is Kind.INCOME else -amount, payee[s.id]))
    d = date(start.year, start.month, 20)
    while d <= end:
        if d >= start:
            out.append((d, -60000, D_CARD))
            out.append((d + timedelta(days=1), -100000, D_SAVINGS))
            out.append((d + timedelta(days=2), -3000, D_ONLINE))
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=20)
    out.sort(key=lambda x: (x[0], x[2]))
    return out


def _with_balances(items: list[tuple[date, int, str]], end_balance: int) -> list[BankTxn]:
    """Oldest first in, newest-first file order out, with a running balance that ends at end_balance."""
    balance = end_balance - sum(a for _, a, _ in items)
    seen: Counter[tuple[date, int, str]] = Counter()
    rows = []
    for d, a, desc in items:
        balance += a
        n = seen[(d, a, desc)]
        seen[(d, a, desc)] += 1
        rows.append((d, a, desc, balance, n))
    rows.reverse()
    return [BankTxn(txn_id(d, a, desc, n), d, a, desc, bal, file_index=i)
            for i, (d, a, desc, bal, n) in enumerate(rows)]


def _card_history(start: date, end: date) -> list[CardTxn]:
    out = []
    d = _saturday_on_or_after(start)
    i = 0
    while d <= end:
        for desc, cat, amount in (('CORNER MARKET', 'Grocery', _variation(i, 15000)), ('FUEL STOP', 'Fuel', 4500),
                                  ('BISTRO', 'Eating out', 3000 if i % 2 else 0)):
            if amount:
                out.append(CardTxn(txn_id(d, -amount, desc, 0), d, d + timedelta(days=1), -amount, desc, cat))
        if i % 4 == 2:
            out.append(CardTxn(txn_id(d, -2500, 'PHARMACY', 0), d, d + timedelta(days=1), -2500, 'PHARMACY',
                               'Pharmacy'))
        d += timedelta(days=7)
        i += 1
    return out


def _waiting_export(budget: Budget, basis: BankTxn, today: date) -> list[BankTxn]:
    """The days since the as-of date: payments of the open rows due by yesterday (one over budget), a card payment
    that covers the card-funded rows, one tracked and one unknown posting."""
    yesterday = today - timedelta(days=1)
    payee = {'pay-a': D_PAY_A, 'pay-b': D_PAY_B, 'mortgage': D_MORTGAGE, 'electric': D_ELECTRIC,
             'internet': D_INTERNET, 'car-loan': D_CAR}
    items: list[tuple[date, int, str]] = []
    card_total = 0
    for r in ledger.ordered(budget.rows):
        if r.state is not RowState.OPEN or not budget.as_of <= r.due_date <= yesterday:
            continue
        if r.card == CARD:
            card_total += r.amount
        elif r.id == 'water-once':
            items.append((r.due_date, -9500, D_WATER))           # $95.00 against $60.00: a proposal
        elif r.series_id in payee:
            items.append((max(r.due_date, budget.as_of), -r.amount, payee[r.series_id]))
    if card_total:
        items.append((yesterday, -card_total, D_CARD))           # a card payment bundle
    items.append((budget.as_of + timedelta(days=3), -2500, D_ONLINE))   # tracked, no budget row
    items.append((budget.as_of + timedelta(days=4), -1200, D_KIOSK))    # no rule: unmatched
    items.sort(key=lambda x: (x[0], x[2]))
    new = _with_balances(items, basis.balance + sum(a for _, a, _ in items))  # type: ignore[operator]
    # the basis (the posting that left the starting balance) closes the file, oldest
    return [*new, replace(basis, file_index=len(new))]


def install(conn: sqlite3.Connection, hid: int, *, today: date, user_id: int | None) -> None:
    """Fill household `hid` with the demo budget, its history and a waiting export."""
    from hbudget.db import repo
    from hbudget.db.connection import transaction

    budget = demo_budget(today=today)
    as_of = budget.as_of
    first = as_of - timedelta(days=HISTORY_DAYS)
    history = _with_balances(_history(budget, first, as_of - timedelta(days=1)), START_BALANCE)
    cards = _card_history(first, as_of - timedelta(days=1))
    export = _waiting_export(budget, history[0], today)
    with transaction(conn, immediate=True):
        repo.save_budget(conn, hid, budget, user_id=user_id, action='household.demo')
        repo.create_upload(conn, hid, upload_id='demo-history', kind='bank', filename='demo-history.csv',
                           uploaded_by='demo', rows=len(history), date_from=first, date_to=as_of - timedelta(days=1),
                           latest_balance=START_BALANCE, has_balance=True, status='imported',
                           note='made-up history, already reconciled')
        repo.insert_txns(conn, hid, 'demo-history', history)
        repo.save_reconciliation(conn, hid, rec_id='demo-rec', by_user='demo', upload_id='demo-history',
                                 through=as_of - timedelta(days=1), new_balance=START_BALANCE, result={},
                                 report_md='The demo history was reconciled when the demo was created.')
        repo.create_upload(conn, hid, upload_id='demo-card', kind='card', filename='demo-card.csv',
                           uploaded_by='demo', rows=len(cards), date_from=first, date_to=as_of, latest_balance=None,
                           has_balance=False, status='imported', note='made-up card purchases')
        repo.insert_card_txns(conn, hid, 'demo-card', cards)
        repo.create_upload(conn, hid, upload_id='demo-waiting', kind='bank', filename='demo-bank-export.csv',
                           uploaded_by='demo', rows=len(export), date_from=export[-1].posted,
                           date_to=export[0].posted, latest_balance=export[0].balance, has_balance=True,
                           note='try me: Preview, accept the proposals, Apply')
        repo.insert_txns(conn, hid, 'demo-waiting', export)
        repo.save_notes(conn, hid, NOTES, user_id=user_id)
