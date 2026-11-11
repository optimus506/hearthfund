"""Core data model (ARCHITECTURE section 4). Plain frozen dataclasses; no I/O."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal

from .money import Cents

__all__ = [
    'Account', 'AccountKind', 'AlertSettings', 'BankTxn', 'Budget', 'Cadence', 'CapRule', 'CardCategory', 'CardTxn',
    'Cents', 'Direction', 'ExpectedItem', 'FlowGroup', 'Goal', 'Horizon', 'IncomeAlert', 'Kind', 'LedgerRow',
    'Milestone', 'PayeeRule',
    'ReconcileSettings', 'RowState', 'RuleKind', 'Schedule', 'Series', 'Settings', 'Severity', 'Status', 'Weekend',
    'WhatIf', 'row_key', 'row_status', 'slug',
]


class Kind(StrEnum):
    INCOME = 'income'
    BILL = 'bill'
    SPENDING = 'spending'


class Cadence(StrEnum):
    ONCE = 'once'
    WEEKLY = 'weekly'
    BIWEEKLY = 'biweekly'
    SEMIMONTHLY = 'semimonthly'
    MONTHLY = 'monthly'
    YEARLY = 'yearly'


class Weekend(StrEnum):
    NONE = 'none'
    BEFORE = 'before'
    AFTER = 'after'


class Status(StrEnum):
    AUTOPAY = 'autopay'
    MANUAL = 'manual'
    NONE = 'none'


class RowState(StrEnum):
    OPEN = 'open'
    CLEARED = 'cleared'
    REMOVED = 'removed'


class RuleKind(StrEnum):
    BILL = 'bill'
    PAY = 'pay'
    ASK = 'ask'
    CARD = 'card'
    TRACK = 'track'
    IGNORE = 'ignore'
    SAVINGS_DRAW = 'savings_draw'
    LOAN_IN = 'loan_in'
    ALLOWANCE = 'allowance'   # several payments use up one row a month; the leftover is offered at month end


class Direction(StrEnum):
    IN = 'in'
    OUT = 'out'
    ANY = 'any'


class AccountKind(StrEnum):
    CHECKING = 'checking'
    SAVINGS = 'savings'
    CREDIT_CARD = 'credit_card'
    LOAN = 'loan'
    MORTGAGE = 'mortgage'
    RETIREMENT_LOAN = 'retirement_loan'
    OTHER = 'other'


class Severity(StrEnum):
    INFO = 'info'
    WARN = 'warn'
    BAD = 'bad'


@dataclass(frozen=True, slots=True)
class Schedule:
    cadence: Cadence
    start: date                      # weekly/biweekly/once: the first occurrence; others: the first possible date
    end: date | None = None          # last possible occurrence (inclusive, compared with the nominal date)
    interval: int = 1                # weekly only: every N weeks
    day: int | None = None           # monthly/yearly: day of month 1..31 (clamped to the month end)
    days: tuple[int, int] | None = None   # semimonthly: two days, e.g. (1, 15)
    month: int | None = None         # yearly: month 1..12
    weekend: Weekend = Weekend.NONE


@dataclass(frozen=True, slots=True)
class Series:
    id: str
    name: str
    kind: Kind
    amount: Cents                    # magnitude, >= 0; the row sign comes from kind
    schedule: Schedule
    funded_by: str | None = None     # income series id: rows are ordered after that payday
    status: Status = Status.NONE     # default bill status of its rows
    card: str | None = None          # credit-card account id that pays it
    debt: str | None = None          # the debt account its payments pay down (#97)
    group: str | None = None         # flow group name
    month_amounts: tuple[tuple[int, Cents], ...] = ()
    cycle_amounts: tuple[Cents, ...] = ()
    active: bool = True
    notes: str = ''
    generated_through: date | None = None   # watermark


@dataclass(frozen=True, slots=True)
class LedgerRow:
    # nominal_date is listed after the required fields (Python needs defaults last); pass fields by keyword.
    id: str
    series_id: str | None
    name: str
    kind: Kind
    amount: Cents                    # SIGNED, ledger sign (+ out, - in)
    due_date: date                   # after any weekend shift
    order_date: date                 # >= due_date for bills funded by a later payday
    seq: int                         # tie-break inside an order_date
    nominal_date: date | None = None # occurrence date before the weekend shift (series rows)
    state: RowState = RowState.OPEN
    status_override: Status | None = None
    amount_was: Cents | None = None
    overridden: bool = False
    card: str | None = None
    paid: Cents = 0
    paid_txn_ids: tuple[str, ...] = ()
    paid_at_change: Cents = 0
    cleared_txn_id: str | None = None
    cleared_on: date | None = None
    note: str = ''


@dataclass(frozen=True, slots=True)
class Horizon:
    mode: Literal['rolling', 'fixed'] = 'rolling'
    months: int = 15
    end: date | None = None


@dataclass(frozen=True, slots=True)
class ReconcileSettings:
    days_before: int = 12
    days_after: int = 21
    tolerance_bp: int = 1000
    tolerance_abs: Cents = 2500


@dataclass(frozen=True, slots=True)
class IncomeAlert:
    series_id: str
    min_avg_bp: int = 8500
    window: int = 4
    short_by: Cents | None = None


@dataclass(frozen=True, slots=True)
class CapRule:
    id: str
    name: str
    rule_ids: tuple[str, ...]
    period: Literal['week', 'month']
    amount: Cents


@dataclass(frozen=True, slots=True)
class AlertSettings:
    cushion: Cents = 30000
    bill_variance_bp: int = 1000
    unscheduled_days: int = 7
    expected_grace_days: int = 3
    unmatched_recurring: bool = True
    income: tuple[IncomeAlert, ...] = ()
    caps: tuple[CapRule, ...] = ()


DEFAULT_STATUS_COLOURS: tuple[tuple[Status, str], ...] = ((Status.AUTOPAY, '#b6d7a8'), (Status.MANUAL, '#ec9ba4'))


@dataclass(frozen=True, slots=True)
class Goal:
    """Something the household is working towards (D-081). Amounts are typed in: nothing moves money."""
    id: str
    name: str
    kind: Literal['emergency', 'savings', 'debt_free']
    target: Cents | None = None      # savings: the amount; emergency: None (months x the monthly bills)
    months: int = 3                  # emergency: how many months of bills
    saved: Cents = 0                 # emergency and savings: put aside so far
    by: date | None = None           # optional target date
    start_debt: Cents | None = None  # debt_free: what was owed when the goal was set


@dataclass(frozen=True, slots=True)
class Milestone:
    """A win the household has seen (D-082): kept so each is celebrated once and listed in the history."""
    key: str                         # what it was, uniquely: 'debt:<id>', 'goal:<id>', 'cushion:<weeks>:<start>', ...
    kind: Literal['debt_paid', 'goal_reached', 'above_cushion', 'under_budget']
    text: str
    on: date


@dataclass(frozen=True, slots=True)
class Settings:
    currency: str = 'USD'
    week_starts: int = 0
    reconcile: ReconcileSettings = field(default_factory=ReconcileSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    status_colours: tuple[tuple[Status, str], ...] = DEFAULT_STATUS_COLOURS
    goals: tuple[Goal, ...] = ()
    celebrate: bool = True           # show wins on Home (they are still kept when off)
    milestones: tuple[Milestone, ...] = ()
    not_recurring: tuple[tuple[str, str], ...] = ()   # (source, payee) the household says are not subscriptions
    # (alert code, payee key, day) an info alert about a payee is set aside until the payee posts in a later month
    alert_snoozes: tuple[tuple[str, str, date], ...] = ()
    card_transfers_on_payday: bool = False   # the forecast moves card-funded rows to checking on payday (D-087)


@dataclass(frozen=True, slots=True)
class PayeeRule:
    id: str
    name: str
    kind: RuleKind
    contains: tuple[str, ...] = ()
    regex: str | None = None
    direction: Direction = Direction.ANY
    min_amount: Cents | None = None
    max_amount: Cents | None = None
    targets: tuple[str, ...] = ()
    card: str | None = None
    note: str = ''


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    name: str
    kind: AccountKind
    balance: Cents = 0
    apr_bp: int = 0
    payment: Cents = 0
    minimum: Cents = 0
    limit: Cents | None = None
    as_of: date | None = None
    promo_ends: date | None = None
    apr_after_bp: int | None = None
    deferred_interest: bool = False
    is_mortgage: bool = False
    include_in_payoff: bool = True
    notes: str = ''
    history: tuple[tuple[date, Cents], ...] = ()   # past balances (as of, balance), oldest first (payoff.record)


@dataclass(frozen=True, slots=True)
class ExpectedItem:
    id: str
    label: str
    amount: Cents                    # magnitude, > 0
    direction: Literal['in', 'out']
    date: date
    status: Literal['open', 'done'] = 'open'


@dataclass(frozen=True, slots=True)
class WhatIf:
    label: str
    amount: Cents
    direction: Literal['in', 'out']
    date: date


@dataclass(frozen=True, slots=True)
class BankTxn:
    id: str
    posted: date
    amount: Cents                    # BANK sign: - out, + in
    description: str                 # already masked
    balance: Cents | None = None
    category: str = ''
    file_index: int = 0              # position in the file after sorting newest-first (0 = newest)
    effective: date | None = None


@dataclass(frozen=True, slots=True)
class CardTxn:
    id: str
    purchased: date
    posted: date
    amount: Cents                    # bank sign: purchases negative, payments positive
    description: str                 # masked
    category: str
    by_hand: bool = False            # moved to its category by hand: re-sorting leaves it alone (D-083)


@dataclass(frozen=True, slots=True)
class CardCategory:
    name: str
    keywords: tuple[str, ...]
    bank_categories: tuple[str, ...] = ()
    weekly_series: tuple[str, ...] = ()
    has_own_rows: bool = False
    merchants: tuple[str, ...] = ()  # stores moved here by hand (cards.store names); tried before keywords (D-083)


@dataclass(frozen=True, slots=True)
class FlowGroup:
    name: str
    colour: str = '--f-other'
    rule_ids: tuple[str, ...] = ()
    series_ids: tuple[str, ...] = ()
    row_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Budget:
    start_balance: Cents
    as_of: date
    horizon: Horizon
    series: tuple[Series, ...]
    rows: tuple[LedgerRow, ...]      # any order; use ledger.ordered()
    rules: tuple[PayeeRule, ...] = ()
    accounts: tuple[Account, ...] = ()
    expected: tuple[ExpectedItem, ...] = ()
    settings: Settings = field(default_factory=Settings)
    card_categories: tuple[CardCategory, ...] = ()
    flow_groups: tuple[FlowGroup, ...] = ()
    last_reconciled_through: date | None = None

    def series_by_id(self) -> dict[str, Series]:
        return {s.id: s for s in self.series}


def row_status(row: LedgerRow, series_by_id: dict[str, Series]) -> Status:
    """The row's bill status: its own mark, else its series' default. Income and spending rows are never marked."""
    if row.kind is not Kind.BILL:
        return Status.NONE
    if row.status_override is not None:
        return row.status_override
    series = series_by_id.get(row.series_id) if row.series_id else None
    return series.status if series else Status.NONE


def slug(text: str) -> str:
    """Lower-case ASCII slug: "Car loan (2nd)" -> "car-loan-2nd"."""
    ascii_text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', '-', ascii_text.lower()).strip('-')


def row_key(row: LedgerRow) -> str:
    """Readable key: slug(name) + "_" + due date. Not unique: URLs use row.id."""
    return '%s_%s' % (slug(row.name), row.due_date.isoformat())
