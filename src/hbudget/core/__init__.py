"""Pure budget logic: standard library only, no I/O, no clock (ARCHITECTURE section 1).

The public API is re-exported here; each module lists its own names in __all__.
"""
from . import (
    alerts,
    cards,
    checkup,
    dates,
    flow,
    forecast,
    ledger,
    money,
    payoff,
    reconcile,
    recurrence,
    rules,
    textmask,
)
from .alerts import Alert
from .cards import CardSummary, CardWeek
from .checkup import BillCheck, CheckUp
from .flow import Flow, FlowGroupTotal, FlowItem
from .forecast import BalancePoint, Forecast, ForecastDelta
from .ledger import RowChange
from .models import *  # noqa: F403
from .models import __all__ as _models_all
from .payoff import DebtOutcome, PayoffPlan, PayoffResult, PromoVerdict
from .reconcile import (
    Applied,
    AutoMatch,
    ClearedLine,
    Prepared,
    Proposal,
    ReconcileError,
    ReconcileResult,
    Tracked,
    Unknown,
)
from .recurrence import Occurrence, amount_for, occurrences

__all__ = [
    *_models_all, 'Alert', 'Applied', 'AutoMatch', 'BalancePoint', 'BillCheck', 'CardSummary', 'CardWeek', 'CheckUp',
    'ClearedLine', 'DebtOutcome', 'Flow', 'FlowGroupTotal', 'FlowItem', 'Forecast', 'ForecastDelta', 'Occurrence',
    'PayoffPlan', 'PayoffResult', 'Prepared', 'PromoVerdict', 'Proposal', 'ReconcileError', 'ReconcileResult',
    'RowChange', 'Tracked', 'Unknown', 'alerts', 'amount_for', 'cards', 'checkup', 'dates', 'flow', 'forecast',
    'ledger', 'money', 'occurrences', 'payoff', 'reconcile', 'recurrence', 'rules', 'textmask',
]
