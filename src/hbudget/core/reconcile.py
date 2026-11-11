"""Bank reconciliation (ARCHITECTURE section 9): a port of the original weekly script.

prepare() finds the starting point in one upload, match() proposes what the new postings clear, apply() applies
the automatic matches and the accepted proposals, report() writes the Markdown report.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from itertools import pairwise
from typing import Literal

from . import alerts as alerts_mod
from . import forecast as fc
from .ledger import clear_row, generate, horizon_end, order_key, ordered, reduce_row
from .models import BankTxn, Budget, LedgerRow, PayeeRule, RowState, RuleKind, Status, row_status
from .money import Cents, fmt
from .rules import classify

__all__ = [
    'Applied', 'AutoMatch', 'ClearedLine', 'Prepared', 'Proposal', 'ReconcileError', 'ReconcileResult', 'Tracked',
    'Unknown', 'apply', 'match', 'prepare', 'report', 'txn_label',
]

ErrorCode = Literal['chain_break', 'no_basis', 'nothing_new', 'empty', 'bad_order']


class ReconcileError(ValueError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Prepared:
    txns: tuple[BankTxn, ...]
    basis: BankTxn | None
    basis_ambiguous: bool
    new: tuple[BankTxn, ...]
    balance_checked: bool
    latest_balance: Cents
    through: date
    ignored: int = 0


@dataclass(frozen=True, slots=True)
class AutoMatch:
    txn_id: str
    row_id: str
    rule_id: str
    amount: Cents | None = None     # an allowance payment: take this much off the row (None clears it)


@dataclass(frozen=True, slots=True)
class Proposal:
    id: str
    txn_id: str
    row_ids: tuple[str, ...]
    kind: Literal['ask', 'tolerance', 'card_bundle', 'expected', 'leftover', 'month']
    note: str
    total: Cents
    group: str
    can_reduce: bool
    expected_id: str | None = None


@dataclass(frozen=True, slots=True)
class Unknown:
    txn_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Tracked:
    txn_id: str
    rule_id: str
    kind: RuleKind


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    prepared: Prepared
    auto: tuple[AutoMatch, ...]
    proposals: tuple[Proposal, ...]
    tracked: tuple[Tracked, ...]
    unknown: tuple[Unknown, ...]


@dataclass(frozen=True, slots=True)
class ClearedLine:
    row_id: str
    name: str
    due_date: date
    amount: Cents
    txn_id: str
    how: str


@dataclass(frozen=True, slots=True)
class Applied:
    budget: Budget
    cleared: tuple[ClearedLine, ...]
    still_open: tuple[str, ...]
    forecast_before: fc.Forecast
    forecast_after: fc.Forecast
    report_md: str
    # added (D-044): what was matched and accepted, so report() and the alerts can describe it
    result: ReconcileResult | None = None
    accepted: tuple[tuple[str, str], ...] = ()       # (proposal id, "clear" | "reduce")
    before: Budget | None = None                       # the budget the reconciliation started from


def txn_label(t: BankTxn) -> str:
    """"3/14 -$150.00 ELECTRIC CO" (descriptions are masked at import)."""
    return '%d/%d %s %s' % (t.posted.month, t.posted.day, fmt(t.amount), t.description[:40])


def _row_label(r: LedgerRow) -> str:
    return '%s %d/%d/%d' % (r.name, r.due_date.month, r.due_date.day, r.due_date.year)


# ---------------------------------------------------------------- prepare

def prepare(txns: Sequence[BankTxn], budget: Budget, *, seen_ids: frozenset[str] = frozenset(),
            ignored: int = 0) -> Prepared:
    """Order one upload, check the balance chain, find the basis and the new postings."""
    if not txns:
        raise ReconcileError('empty', 'The export has no posted transactions.')
    ts = tuple(sorted(txns, key=lambda t: (-t.posted.toordinal(), t.file_index)))
    if all(t.balance is not None for t in ts):
        for a, b in pairwise(ts):          # a newer, b older
            assert a.balance is not None and b.balance is not None
            if b.balance + a.amount != a.balance:
                raise ReconcileError('chain_break', 'The balance chain breaks between %s and %s: transactions are '
                                     'missing from the export.' % (txn_label(b), txn_label(a)))
        hits = [i for i, t in enumerate(ts) if t.balance == budget.start_balance]
        if not hits:
            raise ReconcileError('no_basis', 'The starting balance %s does not appear in this export. The export '
                                 'must reach back to the last reconciliation.' % fmt(budget.start_balance))
        basis_i = hits[0]
        new = tuple(reversed(ts[:basis_i]))
        if not new:
            raise ReconcileError('nothing_new', 'Nothing new: the starting balance %s is already the latest posted '
                                 'balance.' % fmt(budget.start_balance))
        latest = ts[0].balance
        assert latest is not None
        return Prepared(ts, ts[basis_i], len(hits) > 1, new, True, latest, ts[0].posted, ignored)
    since = budget.last_reconciled_through
    new = tuple(t for t in reversed(ts) if (since is None or t.posted >= since) and t.id not in seen_ids)
    if not new:
        raise ReconcileError('nothing_new', 'Nothing new since the last reconciliation.')
    latest = budget.start_balance + sum(t.amount for t in new)
    return Prepared(ts, None, False, new, False, latest, ts[0].posted, ignored)


# ---------------------------------------------------------------- match

def _within(budget: Budget, budgeted: Cents, actual: Cents) -> bool:
    s = budget.settings.reconcile
    b = abs(budgeted)
    return abs(abs(actual) - b) * 10000 <= max(s.tolerance_bp * b, s.tolerance_abs * 10000)


def _month_end(d: date) -> date:
    nxt = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def match(budget: Budget, prepared: Prepared) -> ReconcileResult:
    s = budget.settings.reconcile
    rows = [r for r in ordered(budget.rows) if r.state is RowState.OPEN]
    by_id = budget.series_by_id()
    claimed: set[str] = set()
    auto: list[AutoMatch] = []
    proposals: list[Proposal] = []
    tracked: list[Tracked] = []
    unknown: list[Unknown] = []

    def candidates(targets: Sequence[str], t: BankTxn) -> list[LedgerRow]:
        c = [r for r in rows if r.id not in claimed and (r.name in targets or r.series_id in targets)
             and -s.days_before <= (t.posted - r.due_date).days <= s.days_after]
        return sorted(c, key=lambda r: (r.due_date, order_key(r)))

    def same_month(targets: Sequence[str], t: BankTxn) -> LedgerRow | None:
        """The open row of these targets due in the posting's month, else in the next month (#96)."""
        ym = t.posted.year * 12 + t.posted.month
        for want in (ym, ym + 1):
            c = [r for r in rows if r.id not in claimed and r.amount > 0 and (r.name in targets or r.series_id in
                 targets) and r.due_date.year * 12 + r.due_date.month == want]
            if c:
                return min(c, key=lambda r: (abs((r.due_date - t.posted).days), order_key(r)))
        return None

    def target_name(target: str) -> str:
        return by_id[target].name if target in by_id else target

    pending: list[tuple[BankTxn, PayeeRule]] = []
    allowance: list[tuple[BankTxn, PayeeRule]] = []
    for t in prepared.new:
        rule = classify(t, budget.rules)
        if rule is None:
            unknown.append(Unknown(t.id, 'no rule matches this payee'))
            continue
        if rule.kind in (RuleKind.TRACK, RuleKind.IGNORE, RuleKind.SAVINGS_DRAW, RuleKind.LOAN_IN):
            tracked.append(Tracked(t.id, rule.id, rule.kind))
            continue
        if t.amount == 0 or (rule.kind is RuleKind.PAY) != (t.amount > 0):
            unknown.append(Unknown(t.id, 'unexpected direction for %s (refund?)' % rule.name))
            continue
        if rule.kind is RuleKind.ALLOWANCE:
            allowance.append((t, rule))
        elif rule.kind is RuleKind.PAY:
            c = candidates(rule.targets, t)
            if c:
                claimed.add(c[0].id)
                auto.append(AutoMatch(t.id, c[0].id, rule.id))
            else:
                names = ', '.join(target_name(x) for x in rule.targets) or rule.name
                unknown.append(Unknown(t.id, 'no open %s row near this date' % names))
        elif rule.kind is RuleKind.BILL:
            c = candidates(rule.targets, t)
            if c and _within(budget, c[0].amount, t.amount):
                claimed.add(c[0].id)
                auto.append(AutoMatch(t.id, c[0].id, rule.id))
            else:
                pending.append((t, rule))
        else:
            pending.append((t, rule))

    # allowances, oldest payment first: each takes what it costs off the month's row, the rest off the next row
    left: dict[str, Cents] = {}
    allowance_rows: dict[str, LedgerRow] = {}
    for t, rule in sorted(allowance, key=lambda x: (x[0].posted, x[0].file_index, x[0].id)):
        month = (t.posted.year, t.posted.month)
        pool = [r for r in rows if r.id not in claimed and r.amount > 0 and (r.name in rule.targets or r.series_id
                in rule.targets) and (r.due_date.year, r.due_date.month) >= month]
        pool.sort(key=lambda r: (r.due_date, order_key(r)))
        if not pool:
            names = ', '.join(target_name(x) for x in rule.targets) or rule.name
            unknown.append(Unknown(t.id, 'no open %s row this month or later' % names))
            continue
        cost = -t.amount
        for r in pool:
            allowance_rows[r.id] = r
            have = left.setdefault(r.id, r.amount)
            if cost >= have:
                claimed.add(r.id)
                auto.append(AutoMatch(t.id, r.id, rule.id))
                cost -= have
                left[r.id] = 0
            else:
                auto.append(AutoMatch(t.id, r.id, rule.id, cost))
                left[r.id] = have - cost
                cost = 0
            if cost == 0:
                break

    n = 0
    for t, rule in pending:
        if rule.kind is RuleKind.CARD:
            pool = sorted((r for r in rows if r.id not in claimed and rule.card is not None and r.card == rule.card
                           and r.amount > 0 and r.due_date <= t.posted), key=lambda r: (r.due_date, order_key(r)))
            pick: list[LedgerRow] = []
            total, payment = 0, -t.amount
            for r in pool:
                if total >= payment:
                    break
                if pick and abs(total + r.amount - payment) > abs(total - payment):
                    break
                pick.append(r)
                total += r.amount
            if not pick:
                unknown.append(Unknown(t.id, 'card payment, but no open card-funded rows dated on or before it'))
                continue
            n += 1
            proposals.append(Proposal('P%d' % n, t.id, tuple(r.id for r in pick), 'card_bundle',
                                      'card payment covers these card-funded rows (%s of %s)'
                                      % (fmt(total), fmt(payment)), total, t.id,
                                      len(pick) == 1 and pick[0].amount > payment))
            claimed.update(r.id for r in pick)
            continue
        alts: list[LedgerRow] = []
        for target in rule.targets:
            c = candidates([target], t)
            if not c:
                continue
            if rule.kind is RuleKind.ASK:
                alts.append(min(c, key=lambda r: (abs(abs(r.amount) - abs(t.amount)), r.due_date, order_key(r))))
            else:
                alts.append(c[0])
        alts = list(dict.fromkeys(alts))     # a row named by two targets is proposed once
        if not alts:
            near = same_month(rule.targets, t)
            if near is None:
                unknown.append(Unknown(t.id, 'known payee (%s) but no open row in the date window' % rule.name))
                continue
            # a bill paid in pieces (#96): offer the month's row, to take the payment off or to clear. Not claimed,
            # so the month's other pieces can be offered the same row.
            n += 1
            proposals.append(Proposal('P%d' % n, t.id, (near.id,), 'month',
                                      'paid outside the date window; %s is due %s/%d (%s): take this payment off it?'
                                      % (near.name, near.due_date.month, near.due_date.day, fmt(near.amount)),
                                      near.amount, t.id, near.amount > abs(t.amount)))
            continue
        for r in alts:
            n += 1
            note = 'bank amount differs from budget by %s' % fmt(abs(t.amount) - abs(r.amount))
            if rule.kind is RuleKind.ASK and rule.note:
                note = '%s; %s' % (rule.note, note)
            proposals.append(Proposal('P%d' % n, t.id, (r.id,), 'ask' if rule.kind is RuleKind.ASK else 'tolerance',
                                      note, r.amount, t.id, r.amount > abs(t.amount)))
            claimed.add(r.id)

    # allowance rows whose month the bank data has passed: offer to release what is left (never automatic)
    targets = {x for rule in budget.rules if rule.kind is RuleKind.ALLOWANCE for x in rule.targets}
    for r in rows:
        if r.id in claimed or r.amount <= 0 or not (r.name in targets or r.series_id in targets):
            continue
        if _month_end(r.due_date) >= prepared.through:
            continue
        rest = left.get(r.id, r.amount)
        if rest <= 0:
            continue
        n += 1
        proposals.append(Proposal('P%d' % n, '', (r.id,), 'leftover', 'the month is over: release the %s left '
                                  'of %s' % (fmt(rest), fmt(allowance_rows[r.id].amount if r.id in allowance_rows
                                                            else r.amount)), rest, 'left:' + r.id, False))

    # pass 3: expected items against postings nothing else used
    used = {a.txn_id for a in auto} | {p.txn_id for p in proposals}
    free = [t for t in prepared.new if t.id not in used]
    for e in sorted(budget.expected, key=lambda e: (e.date, e.id)):
        if e.status != 'open':
            continue
        fits = [t for t in free if (t.amount < 0) == (e.direction == 'out') and _within(budget, e.amount, t.amount)
                and -s.days_before <= (t.posted - e.date).days <= s.days_after]
        if not fits:
            continue
        t = min(fits, key=lambda t: (abs(abs(t.amount) - e.amount), t.posted, t.file_index))
        free.remove(t)
        n += 1
        proposals.append(Proposal('P%d' % n, t.id, (), 'expected', 'expected item "%s" posted' % e.label,
                                  e.amount if e.direction == 'out' else -e.amount, t.id, False, e.id))
    return ReconcileResult(prepared, tuple(auto), tuple(proposals), tuple(tracked), tuple(unknown))


# ---------------------------------------------------------------- apply

def apply(budget: Budget, result: ReconcileResult, accept: Mapping[str, Literal['clear', 'reduce']], *,
          today: date) -> Applied:
    by_pid = {p.id: p for p in result.proposals}
    bad = [pid for pid in accept if pid not in by_pid]
    if bad:
        raise ValueError('unknown proposal id(s): %s' % ', '.join(sorted(bad)))
    groups = [by_pid[pid].group for pid in accept]
    if len(set(groups)) < len(groups):
        raise ValueError('two accepted proposals use the same bank transaction; pick one')
    for pid, how in accept.items():
        if how not in ('clear', 'reduce'):
            raise ValueError('%s: choose clear or reduce' % pid)
        if how == 'reduce' and not by_pid[pid].can_reduce:
            raise ValueError('%s cannot be a partial payment' % pid)
    txns = {t.id: t for t in result.prepared.txns}
    rows = {r.id: r for r in budget.rows}
    before = fc.compute(budget, today=today)

    b = budget
    cleared: list[ClearedLine] = []
    for m in result.auto:
        t, r = txns[m.txn_id], rows[m.row_id]
        if m.amount is not None:
            b = reduce_row(b, r.id, m.amount, t.id)
            cleared.append(ClearedLine(r.id, r.name, r.due_date, m.amount, t.id, 'allowance'))
            continue
        now = next(x for x in b.rows if x.id == r.id)
        b = clear_row(b, r.id, t.id, t.posted)
        cleared.append(ClearedLine(r.id, r.name, r.due_date, now.amount, t.id, 'auto'))
    done_expected: set[str] = set()
    for pid in sorted(accept, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0):
        p, how = by_pid[pid], accept[pid]
        if p.kind == 'leftover':
            r = next(x for x in b.rows if x.id == p.row_ids[0])
            b = clear_row(b, r.id, None, result.prepared.through)
            cleared.append(ClearedLine(r.id, r.name, r.due_date, r.amount, '', 'released %s' % pid))
            continue
        t = txns[p.txn_id]
        if p.kind == 'expected':
            if p.expected_id:
                done_expected.add(p.expected_id)
            continue
        for rid in p.row_ids:
            r = rows[rid]
            now = next(x for x in b.rows if x.id == rid)
            if p.kind == 'month':
                # pieces of one month's bill (#96): an earlier piece may have paid the row already, or left less
                # than this piece, which then pays it
                if now.state is not RowState.OPEN:
                    continue
                if how == 'reduce' and abs(t.amount) >= now.amount:
                    how = 'clear'
            if how == 'reduce':
                b = reduce_row(b, rid, abs(t.amount), t.id)
                cleared.append(ClearedLine(rid, r.name, r.due_date, abs(t.amount), t.id, 'reduced %s' % pid))
            else:
                b = clear_row(b, rid, t.id, t.posted)
                cleared.append(ClearedLine(rid, r.name, r.due_date, r.amount, t.id, 'accepted %s' % pid))
    prep = result.prepared
    b = replace(b, start_balance=prep.latest_balance, as_of=prep.through, last_reconciled_through=prep.through,
                expected=tuple(replace(e, status='done') if e.id in done_expected else e for e in b.expected))
    if b.horizon.mode == 'rolling':
        b = generate(b, horizon_end(b))
    still = tuple(r.id for r in ordered(b.rows) if r.state is RowState.OPEN and r.amount != 0
                  and r.due_date <= prep.through)
    after = fc.compute(b, today=today)
    applied = Applied(b, tuple(cleared), still, before, after, '', result,
                      tuple((pid, accept[pid]) for pid in sorted(accept)), budget)
    return replace(applied, report_md=report(applied, dry_run=False, today=today))


# ---------------------------------------------------------------- report

STATUS_TEXT = {Status.AUTOPAY: 'autopay', Status.MANUAL: 'scheduled', Status.NONE: 'no'}


def _table(head: Sequence[str], body: Sequence[Sequence[str]]) -> list[str]:
    out = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    out += ['| ' + ' | '.join(c.replace('|', '/') for c in row) + ' |' for row in body]
    return out


def _d(d: date) -> str:
    return '%02d/%02d/%d' % (d.month, d.day, d.year)


def report(applied: Applied, *, dry_run: bool, today: date) -> str:
    """The reconciliation report in Markdown (same sections as the original weekly report)."""
    res = applied.result
    b = applied.budget
    old = applied.before or b
    f, f0 = applied.forecast_after, applied.forecast_before
    rows_all = {r.id: r for r in (old.rows + b.rows)}
    txns = {t.id: t for t in res.prepared.txns} if res else {}
    rules = {r.id: r for r in b.rules}
    L: list[str] = ['# Budget reconciliation%s' % (' (dry run)' if dry_run else ''), '']
    if res:
        p = res.prepared
        first = p.new[0].posted if p.new else p.through
        L.append('- Bank export: %d/%d to %d/%d (posted items only)' % (first.month, first.day, p.through.month,
                                                                       p.through.day))
        if p.basis is not None:
            L.append('- Starting point: %s matched the bank balance after %s' % (fmt(old.start_balance),
                                                                               txn_label(p.basis)))
            if p.basis_ambiguous:
                L.append('  - That balance appears more than once in the export; the newest one was used.')
        else:
            L.append('- The export has no balance column: new postings were found by date, and the balance was '
                     'computed, not checked against the bank.')
        L.append('- New checkbook balance: **%s** (posted %d/%d)' % (fmt(p.latest_balance), p.through.month,
                                                                     p.through.day))
        if p.ignored:
            L.append('- %d non-posted row(s) in the export were ignored.' % p.ignored)
    L += ['- Pending items are not in the export. Add any you know about as expected items.', '',
          '## Forecast after this update', '',
          '- Lowest balance: **%s on %s**' % (fmt(f.low), _d(f.low_date)),
          '- Highest balance: %s on %s' % (fmt(f.high), _d(f.high_date)),
          '- Ending balance: %s on %s' % (fmt(f.end), _d(f.end_date)),
          '- Change in lowest balance: %s (was %s)' % (fmt(f.low - f0.low), fmt(f0.low)), '', '## Alerts', '']
    found = alerts_mod.evaluate(b, f, today=today, recent=applied, history=res.prepared.txns if res else ())
    L += ['- ' + a.message for a in found] or ['- None.']
    L += ['', '## Cleared rows', '']
    if applied.cleared:
        L += _table(['Budget row', 'Budget', 'Bank transaction', 'How'],
                    [('%s %s' % (c.name, _d(c.due_date)), fmt(c.amount),
                      txn_label(txns[c.txn_id]) if c.txn_id in txns else '', c.how) for c in applied.cleared])
    else:
        L.append('None.')
    L += ['', '## Needs your decision', '']
    taken = {pid for pid, _ in applied.accepted}
    open_props = [p for p in res.proposals if p.id not in taken] if res else []
    if open_props:
        L += ['Not applied yet. Proposals that share a bank transaction are alternatives: pick at most one.', '']
        L += _table(['ID', 'Bank transaction', 'Would clear', 'Why'],
                    [(p.id, txn_label(txns[p.txn_id]) if p.txn_id in txns else 'month over',
                      '; '.join('%s %s' % (_row_label(rows_all[i]), fmt(rows_all[i].amount)) for i in p.row_ids)
                      or 'expected item', p.note) for p in open_props])
    else:
        L.append('None.')
    L += ['', '## Bank items with no budget row', '']
    items = [(txn_label(txns[u.txn_id]), 'unmatched: ' + u.reason) for u in (res.unknown if res else ())]
    items += [(txn_label(txns[x.txn_id]), 'not budgeted: %s' % (rules[x.rule_id].name if x.rule_id in rules
                                                                 else x.rule_id)) for x in (res.tracked if res else ())]
    L += _table(['Transaction', 'Status'], items) if items else ['None.']
    through = res.prepared.through if res else today
    L += ['', '## Budget rows still open (due on or before %d/%d)' % (through.month, through.day), '']
    by_id = b.series_by_id()
    still = [rows_all[i] for i in applied.still_open]
    L += _table(['Row', 'Budget', 'Scheduled'], [(_row_label(r), fmt(r.amount), STATUS_TEXT[row_status(r, by_id)])
                                                 for r in still]) if still else ['None.']
    caps = alerts_mod.cap_totals(b, res.prepared.txns if res else (), today=today)
    if caps:
        L += ['', '## Spending caps', '']
        L += ['- %s: %s this %s (cap %s; average %s)' % (c.name, fmt(total), c.period, fmt(c.amount), fmt(avg))
              for c, total, avg in caps]
    return '\n'.join(L) + '\n'
