"""How a bill-system file (not a full backup) is combined with a household's budget. Shared by the web and the CLI.

The file's checkbook balance and as-of date are taken when the household has no balance yet (new or emptied), or
when the user asks for it explicitly; otherwise the household keeps its own, because after a reconciliation the
household's balance is the bank's and the file's is older.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Literal

from hbudget.core import ledger
from hbudget.core.models import Budget, LedgerRow, RowState
from hbudget.db import repo
from hbudget.io.billsystem import BillSystem


def has_no_balance(b: Budget) -> bool:
    """A new or emptied household: no checkbook balance and never reconciled."""
    return b.start_balance == 0 and b.last_reconciled_through is None


def combine(old: Budget, bs: BillSystem, mode: Literal['replace', 'merge'], *, set_balance: bool,
            today: date) -> tuple[Budget, bool]:
    """The new budget, and whether the file's balance and as-of date were taken."""
    take = set_balance or has_no_balance(old)
    incoming = bs.budget
    if mode == 'replace':
        nb = incoming
        if not take:
            nb = replace(nb, start_balance=old.start_balance, as_of=old.as_of,
                         last_reconciled_through=old.last_reconciled_through)
    else:
        base = replace(old, start_balance=incoming.start_balance, as_of=incoming.as_of) if take else old
        nb = repo._merge(base, incoming, today=today)
    return ledger.generate(nb, ledger.horizon_end(nb)), take


MAX_LISTED = 200   # rows listed per kind of change in a preview


def row_changes(old: Budget, new: Budget) -> dict[str, Any]:
    """What an import does to the open rows (#64): added, removed and changed (amount or due date), each with name,
    due date and amount, at most MAX_LISTED of each, plus the counts. An added row of a series the household
    already had, dated before that series' latest row, is flagged `check`: it may already be paid."""
    before = {r.id: r for r in old.rows if r.state is RowState.OPEN}
    after = {r.id: r for r in new.rows if r.state is RowState.OPEN}
    latest: dict[str, date] = {}
    for r in old.rows:
        if r.series_id:
            latest[r.series_id] = max(r.due_date, latest.get(r.series_id, r.due_date))

    def rj(r: LedgerRow) -> dict[str, Any]:
        return {'id': r.id, 'name': r.name, 'due': r.due_date.isoformat(), 'amount': r.amount}
    added = [{**rj(r), 'check': bool(r.series_id in latest and r.due_date < latest[r.series_id])}
             for r in ledger.ordered(after.values()) if r.id not in before]
    removed = [rj(r) for r in ledger.ordered(before.values()) if r.id not in after]
    changed = [{**rj(r), 'amount_was': before[r.id].amount, 'due_was': before[r.id].due_date.isoformat()}
               for r in ledger.ordered(after.values())
               if r.id in before and (r.amount, r.due_date) != (before[r.id].amount, before[r.id].due_date)]
    return {'added': added[:MAX_LISTED], 'removed': removed[:MAX_LISTED], 'changed': changed[:MAX_LISTED],
            'counts': {'added': len(added), 'removed': len(removed), 'changed': len(changed),
                       'check': sum(1 for a in added if a['check'])}}
