"""Row generation, ledger order and row edits (ARCHITECTURE sections 5 and 6)."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from .dates import add_months
from .models import Budget, Kind, LedgerRow, RowState, Series, Status
from .money import Cents
from .recurrence import Occurrence, amount_for, occurrences

__all__ = [
    'RowChange', 'add_row', 'change_amount', 'clear_row', 'generate', 'horizon_end', 'mark_done', 'mark_status',
    'move_row', 'order_key', 'ordered', 'parse_months', 'reduce_row', 'regenerate_series', 'remove_row',
    'set_month_amounts', 'undo_change',
]

FUNDING_LOOKAHEAD_DAYS = 35
_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class RowChange:
    row_id: str
    name: str
    due_date: date
    was: Cents
    now: Cents


# ---------------------------------------------------------------- order

def order_key(row: LedgerRow) -> tuple[date, int, str]:
    return (row.order_date, row.seq, row.id)


def ordered(rows: Iterable[LedgerRow]) -> list[LedgerRow]:
    """Rows in ledger order: (order_date, seq, id). Never by due date alone."""
    return sorted(rows, key=order_key)


def horizon_end(budget: Budget) -> date:
    h = budget.horizon
    if h.mode == 'fixed':
        if h.end is None:
            raise ValueError('a fixed horizon needs an end date')
        return h.end
    return add_months(budget.as_of, h.months)


def parse_months(spec: str) -> frozenset[int]:
    """"10-4" -> {10, 11, 12, 1, 2, 3, 4}; "1,2,3"; "5". Ranges may wrap the year end."""
    out: set[int] = set()
    for part in spec.replace(' ', '').split(','):
        if not part:
            raise ValueError('empty month in %r' % spec)
        try:
            if '-' in part:
                a, b = (int(x) for x in part.split('-', 1))
                if not (1 <= a <= 12 and 1 <= b <= 12):
                    raise ValueError
                m = a
                while True:
                    out.add(m)
                    if m == b:
                        break
                    m = m % 12 + 1
            else:
                m = int(part)
                if not 1 <= m <= 12:
                    raise ValueError
                out.add(m)
        except ValueError:
            raise ValueError('months must be 1 to 12: %r' % spec) from None
    return frozenset(out)


def _sign(kind: Kind, magnitude: Cents) -> Cents:
    return -magnitude if kind is Kind.INCOME else magnitude


def _occurrence_of(row: LedgerRow) -> date:
    return row.nominal_date or row.due_date


# ---------------------------------------------------------------- generation

def _order_date(series: Series, occ: Occurrence, by_id: dict[str, Series]) -> date:
    """After the first payday of the funding income on or after the bill's nominal date (35-day look-ahead)."""
    funder = by_id.get(series.funded_by) if series.funded_by else None
    if funder is None:
        return occ.actual
    pays = occurrences(funder.schedule, occ.nominal, occ.nominal + timedelta(days=FUNDING_LOOKAHEAD_DAYS))
    return max(pays[0].actual, occ.actual) if pays else occ.actual


def _build_rows(budget: Budget, existing: Sequence[LedgerRow],
                wanted: Sequence[tuple[Series, Occurrence]]) -> list[LedgerRow]:
    """New rows for (series, occurrence) pairs, with order_date and seq (section 6)."""
    by_id = budget.series_by_id()
    seqs: dict[date, list[int]] = defaultdict(list)
    for r in existing:
        seqs[r.order_date].append(r.seq)
    drafts: dict[date, list[tuple[Series, Occurrence, Cents]]] = defaultdict(list)
    for s, occ in wanted:
        drafts[_order_date(s, occ, by_id)].append((s, occ, amount_for(s, occ)))
    out: list[LedgerRow] = []
    for od, items in drafts.items():
        items.sort(key=lambda x: (x[1].actual, x[0].name, x[0].id))
        income = [x for x in items if x[0].kind is Kind.INCOME]
        other = [x for x in items if x[0].kind is not Kind.INCOME]
        lo = min([*seqs[od], 0])
        hi = max([*seqs[od], -1])
        numbered = [(lo - len(income) + i, x) for i, x in enumerate(income)]
        numbered += [(hi + 1 + i, x) for i, x in enumerate(other)]
        for seq, (s, occ, amount) in numbered:
            out.append(LedgerRow(id='%s:%s' % (s.id, occ.nominal.isoformat()), series_id=s.id, name=s.name,
                                 kind=s.kind, amount=_sign(s.kind, amount), due_date=occ.actual, order_date=od,
                                 seq=seq, nominal_date=occ.nominal, card=s.card))
            seqs[od].append(seq)
    return out


def generate(budget: Budget, until: date) -> Budget:
    """Add rows for every active series after its watermark, up to `until`. Rows are never deleted."""
    have = {(r.series_id, _occurrence_of(r)) for r in budget.rows if r.series_id}
    ids = {r.id for r in budget.rows}
    wanted: list[tuple[Series, Occurrence]] = []
    series_out: list[Series] = []
    floor = budget.as_of - _DAY
    for s in budget.series:
        watermark = s.generated_through or floor
        if not s.active:
            series_out.append(replace(s, generated_through=max(watermark, floor)))
            continue
        for occ in occurrences(s.schedule, watermark + _DAY, until):
            key = (s.id, occ.nominal)
            if key in have or '%s:%s' % (s.id, occ.nominal.isoformat()) in ids:
                continue
            have.add(key)
            wanted.append((s, occ))
        series_out.append(replace(s, generated_through=max(watermark, until)))
    new_rows = _build_rows(budget, budget.rows, wanted)
    return replace(budget, series=tuple(series_out), rows=budget.rows + tuple(new_rows))


def regenerate_series(budget: Budget, series_id: str, from_date: date) -> Budget:
    """Recompute open, not-overridden rows of one series whose occurrence (nominal date) is on or after from_date,
    after the series was edited. Nominal dates are compared, so a weekend-shift edit never loses a row. Open rows
    changed by hand keep their amounts and dates but take the series' name and card (#60)."""
    by_id = budget.series_by_id()
    if series_id not in by_id:
        raise KeyError(series_id)
    s = by_id[series_id]
    through = s.generated_through or (budget.as_of - _DAY)
    occs = occurrences(s.schedule, s.schedule.start, through) if s.active else []
    wanted = {o.nominal: o for o in occs if o.nominal >= from_date}
    mine = [r for r in budget.rows if r.series_id == series_id]
    taken = {_occurrence_of(r) for r in mine}
    keep: list[LedgerRow] = []
    for r in budget.rows:
        in_scope = (r.series_id == series_id and r.state is RowState.OPEN and not r.overridden
                    and _occurrence_of(r) >= from_date)
        if not in_scope:
            hand_edited = (r.series_id == series_id and r.state is RowState.OPEN and r.overridden
                           and _occurrence_of(r) >= from_date)
            keep.append(replace(r, name=s.name, card=s.card) if hand_edited else r)
            continue
        occ = wanted.get(_occurrence_of(r))
        if occ is None:
            continue    # the occurrence no longer exists: an open, generated row goes away
        amount = _sign(s.kind, amount_for(s, occ))
        # a row listed on its own due date moves with it; one listed after a later payday stays there
        order_date = occ.actual if r.order_date == r.due_date else max(r.order_date, occ.actual)
        keep.append(replace(r, amount=amount, due_date=occ.actual, order_date=order_date, name=s.name, card=s.card))
    missing = [(s, o) for n, o in sorted(wanted.items()) if n not in taken]
    budget = replace(budget, rows=tuple(keep))
    return replace(budget, rows=budget.rows + tuple(_build_rows(budget, budget.rows, missing)))


def relabel_series(budget: Budget, series_id: str, from_date: date) -> Budget:
    """After an edit that left the amount and schedule alone: give the series' open rows on or after from_date its
    name and card, keeping every row's amount (rows imported from a workbook often differ from the series amount)."""
    s = budget.series_by_id().get(series_id)
    if s is None:
        raise KeyError(series_id)
    return replace(budget, rows=tuple(
        replace(r, name=s.name, card=s.card)
        if r.series_id == series_id and r.state is RowState.OPEN and _occurrence_of(r) >= from_date else r
        for r in budget.rows))


def set_series_amount(budget: Budget, series_id: str, amount: Cents, from_date: date,
                      to_date: date | None = None) -> tuple[Budget, list[RowChange]]:
    """A new amount from a date on: the series amount (used for rows made later) and every open row due on or after
    from_date that the series amount sets (not months with a seasonal amount, not rows changed by hand).

    With to_date (inclusive) only the open rows due up to it change, and they are marked as changed by hand so
    a later regeneration keeps them; the series amount and the rows after to_date stay as they are (#94)."""
    if to_date is not None and to_date < from_date:
        raise ValueError('the end date is before the start date')
    s = budget.series_by_id().get(series_id)
    if s is None:
        raise KeyError(series_id)
    if amount < 0:
        raise ValueError('amounts are magnitudes (>= 0)')
    if s.cycle_amounts:
        raise ValueError('this item repeats a list of amounts; change them on the page')
    seasonal = {m for m, _ in s.month_amounts}
    changes: list[RowChange] = []
    rows: list[LedgerRow] = []
    for r in budget.rows:
        if (r.series_id == series_id and r.state is RowState.OPEN and not r.overridden and r.due_date >= from_date
                and (to_date is None or r.due_date <= to_date) and _occurrence_of(r).month not in seasonal):
            now = _sign(s.kind, amount)
            if now != r.amount:
                changes.append(RowChange(r.id, r.name, r.due_date, r.amount, now))
                r = replace(r, amount=now, overridden=to_date is not None)
        rows.append(r)
    if to_date is not None:
        return replace(budget, rows=tuple(rows)), sorted(changes, key=lambda c: (c.due_date, c.row_id))
    series = tuple(replace(x, amount=amount) if x.id == series_id else x for x in budget.series)
    return replace(budget, series=series, rows=tuple(rows)), sorted(changes, key=lambda c: (c.due_date, c.row_id))


def set_month_amounts(budget: Budget, series_id: str, rules: Sequence[tuple[frozenset[int], Cents]],
                      from_date: date) -> tuple[Budget, list[RowChange]]:
    """Seasonal amounts: store them on the series and change open rows due on/after from_date in those months."""
    by_id = budget.series_by_id()
    if series_id not in by_id:
        raise KeyError(series_id)
    s = by_id[series_id]
    month_amount: dict[int, Cents] = {}
    for months, amount in rules:
        if amount < 0:
            raise ValueError('amounts are magnitudes (>= 0)')
        for m in months:
            month_amount.setdefault(m, amount)     # the first rule that names a month wins
    new_series = replace(s, month_amounts=tuple(sorted(month_amount.items())))
    changes: list[RowChange] = []
    rows: list[LedgerRow] = []
    for r in ordered(budget.rows):
        month = _occurrence_of(r).month
        if (r.series_id == series_id and r.state is RowState.OPEN and r.due_date >= from_date
                and month in month_amount):
            now = _sign(s.kind, month_amount[month])
            if now != r.amount:
                changes.append(RowChange(r.id, r.name, r.due_date, r.amount, now))
                r = replace(r, amount=now)
        rows.append(r)
    series = tuple(new_series if x.id == series_id else x for x in budget.series)
    return replace(budget, series=series, rows=tuple(rows)), changes


# ---------------------------------------------------------------- row edits

def _find(budget: Budget, row_id: str) -> LedgerRow:
    for r in budget.rows:
        if r.id == row_id:
            return r
    raise KeyError(row_id)


def _put(budget: Budget, row: LedgerRow) -> Budget:
    return replace(budget, rows=tuple(row if r.id == row.id else r for r in budget.rows))


def _open(budget: Budget, row_id: str, action: str) -> LedgerRow:
    r = _find(budget, row_id)
    if r.state is not RowState.OPEN:
        raise ValueError('cannot %s a %s row' % (action, r.state.value))
    return r


def change_amount(budget: Budget, row_id: str, new_amount: Cents) -> Budget:
    """Set an open row's amount. `new_amount` is a magnitude; the sign comes from the row's kind."""
    if new_amount < 0:
        raise ValueError('amount must be >= 0')
    r = _open(budget, row_id, 'change')
    now = _sign(r.kind, new_amount)
    if r.amount_was is None:
        r = replace(r, amount_was=r.amount, paid_at_change=r.paid)
    return _put(budget, replace(r, amount=now, overridden=True))


def remove_row(budget: Budget, row_id: str) -> Budget:
    """The user says this row will not be paid: it stays in the budget as removed, so it can be undone."""
    return _put(budget, replace(_open(budget, row_id, 'remove'), state=RowState.REMOVED))


def mark_done(budget: Budget, row_id: str, on: date) -> Budget:
    """The user says this row was already paid (or received) and is in the bank: it leaves the forecast as cleared,
    with no bank posting tied to it, so it can be undone. Unlike Remove, nothing goes back into the budget."""
    return clear_row(budget, row_id, None, on)


def undo_change(budget: Budget, row_id: str) -> Budget:
    """Undo Remove or Already paid (back to open), or Change (back to the earlier amount, minus partial payments made
    since). A row cleared against a bank posting is not undone here; undo the reconciliation instead."""
    r = _find(budget, row_id)
    if r.state is RowState.REMOVED:
        return _put(budget, replace(r, state=RowState.OPEN))
    if r.state is RowState.CLEARED and r.cleared_txn_id is None:
        return _put(budget, replace(r, state=RowState.OPEN, cleared_on=None))
    if r.state is RowState.OPEN and r.amount_was is not None:
        paid_since = r.paid - r.paid_at_change
        restored = r.amount_was - _sign(r.kind, paid_since)
        return _put(budget, replace(r, amount=restored, amount_was=None, paid_at_change=0))
    raise ValueError('nothing to undo on this row')


def mark_status(budget: Budget, row_id: str, status: Status | None) -> Budget:
    """Per-row bill status; None goes back to the series default."""
    return _put(budget, replace(_find(budget, row_id), status_override=status))


def _place_after(budget: Budget, after: LedgerRow, moving_id: str | None) -> tuple[Budget, int]:
    """Make room right after `after` on its order_date; returns the budget and the free seq."""
    seq = after.seq + 1
    rows = tuple(replace(r, seq=r.seq + 1) if (r.order_date == after.order_date and r.seq >= seq
                                                 and r.id != moving_id) else r for r in budget.rows)
    return replace(budget, rows=rows), seq


def add_row(budget: Budget, row_id: str, name: str, kind: Kind, amount: Cents, due: date, *,
            card: str | None = None, order_after: str | None = None) -> Budget:
    """A one-off row (no series). `amount` is a magnitude. Placed after `order_after`, else at its due date."""
    if any(r.id == row_id for r in budget.rows):
        raise ValueError('row id %r already exists' % row_id)
    if amount < 0:
        raise ValueError('amount must be >= 0')
    if order_after is not None:
        after = _find(budget, order_after)
        if after.order_date < due:
            raise ValueError('a row cannot be listed before its due date')
        budget, seq = _place_after(budget, after, None)
        order_date = after.order_date
    else:
        order_date = due
        same = [r.seq for r in budget.rows if r.order_date == due]
        seq = min([*same, 0]) - 1 if kind is Kind.INCOME else max([*same, -1]) + 1
    row = LedgerRow(id=row_id, series_id=None, name=name, kind=kind, amount=_sign(kind, amount), due_date=due,
                    order_date=order_date, seq=seq, card=card)
    return replace(budget, rows=(*budget.rows, row))


def move_row(budget: Budget, row_id: str, after_row_id: str) -> Budget:
    """List a row right after another one (e.g. after the payday that funds it)."""
    r = _find(budget, row_id)
    after = _find(budget, after_row_id)
    if row_id == after_row_id:
        raise ValueError('a row cannot follow itself')
    if after.order_date < r.due_date:
        raise ValueError('a row cannot be listed before its due date')
    budget, seq = _place_after(budget, after, row_id)
    return _put(budget, replace(r, order_date=after.order_date, seq=seq, overridden=True))


def clear_row(budget: Budget, row_id: str, txn_id: str | None, on: date) -> Budget:
    r = _open(budget, row_id, 'clear')
    return _put(budget, replace(r, state=RowState.CLEARED, cleared_txn_id=txn_id, cleared_on=on))


def reduce_row(budget: Budget, row_id: str, by: Cents, txn_id: str) -> Budget:
    """A partial payment: take `by` off a positive row, which stays open with the rest (D-030)."""
    r = _open(budget, row_id, 'reduce')
    if r.amount <= 0 or not 0 < by < r.amount:
        raise ValueError('a reduction must be more than 0 and less than the row amount')
    return _put(budget, replace(r, amount=r.amount - by, paid=r.paid + by, paid_txn_ids=(*r.paid_txn_ids, txn_id)))
