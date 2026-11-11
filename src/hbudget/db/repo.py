"""Repository: core objects <-> SQLite rows, always scoped by household (ARCHITECTURE 14.2, 14.3).

Every function takes the household id from the caller (the authorised URL) and every query filters on it, so an id
from another household is simply not found. SQL is built only from the constant table and column names below;
values are always parameters.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from typing import Any, Literal

from hbudget.core.ledger import generate, horizon_end, regenerate_series
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
    Goal,
    Horizon,
    IncomeAlert,
    Kind,
    LedgerRow,
    Milestone,
    PayeeRule,
    ReconcileSettings,
    RowState,
    RuleKind,
    Schedule,
    Series,
    Settings,
    Status,
    Weekend,
)
from hbudget.core.recurrence import occurrences
from hbudget.io import ImportProblem, billsystem
from hbudget.io.bank import MappingProfile

from .connection import transaction

__all__ = [
    'AUDIT_VIA',
    'ROLES',
    'Conflict',
    'Household',
    'LastOwner',
    'Member',
    'Reconciliation',
    'Upload',
    'UploadRemoval',
    'add_member',
    'audit',
    'audit_via',
    'backup',
    'budget_version',
    'create_household',
    'create_upload',
    'delete_household',
    'delete_profile',
    'delete_setting',
    'delete_upload',
    'edit_budget',
    'get_household',
    'get_profile',
    'get_reconciliation',
    'get_role',
    'get_setting',
    'get_upload',
    'history_counts',
    'households_for_user',
    'import_bill_system',
    'insert_card_txns',
    'insert_txns',
    'list_audit',
    'list_card_txns',
    'list_history',
    'list_households',
    'list_members',
    'list_profiles',
    'list_reconciliations',
    'list_uploads',
    'load_budget',
    'load_extras',
    'load_notes',
    'merge_duplicate_txns',
    'purge_audit',
    'redate_card_txns',
    'remove_member',
    'rename_household',
    'save_budget',
    'save_extras',
    'save_notes',
    'save_profile',
    'save_reconciliation',
    'seen_txn_ids',
    'set_card_category',
    'set_expected_status',
    'set_role',
    'set_setting',
    'set_upload_status',
    'upload_removal',
    'upload_txns',
    'wipe_household',
]

ROLES = ('owner', 'editor', 'contributor', 'viewer')
Role = Literal['owner', 'editor', 'contributor', 'viewer']


def _now(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec='seconds')


def _d(v: str | None) -> date | None:
    return date.fromisoformat(v) if v else None


def _iso(v: date | None) -> str | None:
    return v.isoformat() if v else None


def _j(v: object) -> str:
    return json.dumps(v, separators=(',', ':'), sort_keys=True)


# ---------------------------------------------------------------- households and members
@dataclass(frozen=True, slots=True)
class Household:
    id: int
    name: str
    currency: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Member:
    user_id: int
    username: str
    role: str


def _household(row: sqlite3.Row | None) -> Household | None:
    if row is None:
        return None
    return Household(row['id'], row['name'], row['currency'], row['created_at'], row['updated_at'])


def create_household(conn: sqlite3.Connection, name: str, *, currency: str = 'USD', as_of: date | None = None,
                     now: datetime | None = None) -> int:
    at = _now(now)
    day = as_of or (now or datetime.now(UTC)).date()
    with transaction(conn):
        cur = conn.execute('INSERT INTO households (name, currency, as_of, created_at, updated_at) '
                           'VALUES (?, ?, ?, ?, ?)', (name.strip() or 'Household', currency, day.isoformat(), at, at))
    assert cur.lastrowid is not None
    return cur.lastrowid


def get_household(conn: sqlite3.Connection, hid: int) -> Household | None:
    return _household(conn.execute('SELECT * FROM households WHERE id = ?', (hid,)).fetchone())


def list_households(conn: sqlite3.Connection) -> list[Household]:
    return [h for h in (_household(r) for r in conn.execute('SELECT * FROM households ORDER BY id')) if h]


def rename_household(conn: sqlite3.Connection, hid: int, name: str, *, now: datetime | None = None) -> None:
    with transaction(conn):
        if conn.execute('UPDATE households SET name = ?, updated_at = ? WHERE id = ?',
                        (name.strip() or 'Household', _now(now), hid)).rowcount != 1:
            raise KeyError(hid)


def delete_household(conn: sqlite3.Connection, hid: int) -> None:
    """Delete a household and everything in it (the audit log stays)."""
    with transaction(conn):
        if conn.execute('DELETE FROM households WHERE id = ?', (hid,)).rowcount != 1:
            raise KeyError(hid)


def households_for_user(conn: sqlite3.Connection, user_id: int) -> list[tuple[Household, str]]:
    rows = conn.execute('SELECT h.*, m.role FROM households h JOIN memberships m ON m.household_id = h.id '
                        'WHERE m.user_id = ? ORDER BY h.id', (user_id,)).fetchall()
    return [(h, r['role']) for r in rows if (h := _household(r))]


def _role(role: str) -> str:
    if role not in ROLES:
        raise ValueError('unknown role %r' % role)
    return role


def add_member(conn: sqlite3.Connection, hid: int, user_id: int, role: str) -> None:
    """Add a member, or change an existing member's role (the last owner cannot be demoted this way either)."""
    with transaction(conn, immediate=True):
        if _role(role) != 'owner' and get_role(conn, user_id, hid) == 'owner':
            _keep_an_owner(conn, hid, user_id)
        conn.execute('INSERT INTO memberships (user_id, household_id, role) VALUES (?, ?, ?) '
                     'ON CONFLICT (user_id, household_id) DO UPDATE SET role = excluded.role',
                     (user_id, hid, _role(role)))


class LastOwner(ValueError):
    """The change would leave the household without an owner."""


def _keep_an_owner(conn: sqlite3.Connection, hid: int, user_id: int) -> None:
    """Inside the caller's transaction: refuse when user_id is the household's only owner."""
    row = conn.execute('SELECT role FROM memberships WHERE household_id = ? AND user_id = ?', (hid, user_id)).fetchone()
    if row is None:
        raise KeyError(user_id)
    owners = conn.execute("SELECT COUNT(*) FROM memberships WHERE household_id = ? AND role = 'owner'",
                          (hid,)).fetchone()[0]
    if row[0] == 'owner' and owners == 1:
        raise LastOwner('A household needs at least one owner.')


def set_role(conn: sqlite3.Connection, hid: int, user_id: int, role: str) -> None:
    with transaction(conn, immediate=True):
        if _role(role) != 'owner':
            _keep_an_owner(conn, hid, user_id)
        if conn.execute('UPDATE memberships SET role = ? WHERE household_id = ? AND user_id = ?',
                        (_role(role), hid, user_id)).rowcount != 1:
            raise KeyError(user_id)


def remove_member(conn: sqlite3.Connection, hid: int, user_id: int) -> None:
    with transaction(conn, immediate=True):
        _keep_an_owner(conn, hid, user_id)            # also raises KeyError for a non-member
        conn.execute('DELETE FROM memberships WHERE household_id = ? AND user_id = ?', (hid, user_id))


def get_role(conn: sqlite3.Connection, user_id: int, hid: int) -> str | None:
    """The user's role in the household, or None (not a member: the web layer answers 404)."""
    row = conn.execute('SELECT role FROM memberships WHERE user_id = ? AND household_id = ?', (user_id, hid)).fetchone()
    return row[0] if row else None


def list_members(conn: sqlite3.Connection, hid: int) -> list[Member]:
    rows = conn.execute('SELECT m.user_id, u.username, m.role FROM memberships m JOIN users u ON u.id = m.user_id '
                        'WHERE m.household_id = ? ORDER BY u.username', (hid,)).fetchall()
    return [Member(r[0], r[1], r[2]) for r in rows]


# ---------------------------------------------------------------- settings, schedule <-> JSON
def _settings_json(s: Settings) -> str:
    return _j({'currency': s.currency, 'week_starts': s.week_starts, 'reconcile': asdict(s.reconcile),
               'alerts': asdict(s.alerts), 'status_colours': [[st.value, c] for st, c in s.status_colours],
               'goals': [{**asdict(g), 'by': _iso(g.by)} for g in s.goals], 'celebrate': s.celebrate,
               'milestones': [{**asdict(m), 'on': m.on.isoformat()} for m in s.milestones],
               'not_recurring': [list(x) for x in s.not_recurring],
               'alert_snoozes': [[c, k, d.isoformat()] for c, k, d in s.alert_snoozes],
               'card_transfers_on_payday': s.card_transfers_on_payday})


def _settings(raw: str, currency: str) -> Settings:
    d = json.loads(raw or '{}')
    if not d:
        return replace(Settings(), currency=currency)
    al = d.get('alerts', {})
    alerts = AlertSettings(**{k: v for k, v in al.items() if k not in ('income', 'caps')},
                           income=tuple(IncomeAlert(**i) for i in al.get('income', [])),
                           caps=tuple(CapRule(**{**c, 'rule_ids': tuple(c['rule_ids'])}) for c in al.get('caps', [])))
    return Settings(currency=currency, week_starts=int(d.get('week_starts', 0)),
                    reconcile=ReconcileSettings(**d.get('reconcile', {})), alerts=alerts,
                    status_colours=tuple((Status(st), c) for st, c in d.get('status_colours', [])) or
                    Settings().status_colours,
                    goals=tuple(Goal(**{**g, 'by': _d(g.get('by'))}) for g in d.get('goals', [])),
                    celebrate=bool(d.get('celebrate', True)),
                    milestones=tuple(Milestone(**{**x, 'on': date.fromisoformat(x['on'])})
                                     for x in d.get('milestones', [])),
                    not_recurring=tuple((str(s), str(p)) for s, p in d.get('not_recurring', [])),
                    alert_snoozes=tuple((str(c), str(k), date.fromisoformat(on))
                                        for c, k, on in d.get('alert_snoozes', [])),
                    card_transfers_on_payday=bool(d.get('card_transfers_on_payday', False)))


def _schedule_json(s: Schedule) -> str:
    return _j({'cadence': s.cadence.value, 'start': s.start.isoformat(), 'end': _iso(s.end), 'interval': s.interval,
               'day': s.day, 'days': list(s.days) if s.days else None, 'month': s.month, 'weekend': s.weekend.value})


def _schedule(raw: str) -> Schedule:
    d = json.loads(raw)
    days = d.get('days')
    return Schedule(cadence=Cadence(d['cadence']), start=date.fromisoformat(d['start']), end=_d(d.get('end')),
                    interval=int(d.get('interval') or 1), day=d.get('day'),
                    days=(int(days[0]), int(days[1])) if days else None, month=d.get('month'),
                    weekend=Weekend(d.get('weekend') or 'none'))


# ---------------------------------------------------------------- budget tables
# (table, key columns, columns) - constants, never user input
_TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    'series': (('id',), ('id', 'name', 'kind', 'amount', 'schedule_json', 'funded_by', 'status', 'card', 'debt', 'grp',
                         'month_amounts_json', 'cycle_amounts_json', 'active', 'notes', 'position',
                         'generated_through')),
    'ledger_rows': (('id',), ('id', 'series_id', 'name', 'kind', 'amount', 'due_date', 'nominal_date', 'order_date',
                              'seq', 'state', 'status_override', 'amount_was', 'overridden', 'card', 'paid',
                              'paid_txn_ids_json', 'paid_at_change', 'cleared_txn_id', 'cleared_on', 'note')),
    'payee_rules': (('id',), ('id', 'position', 'name', 'kind', 'contains_json', 'regex', 'direction', 'min_amount',
                              'max_amount', 'targets_json', 'card', 'note')),
    'accounts': (('id',), ('id', 'position', 'name', 'kind', 'balance', 'apr_bp', 'payment', 'minimum', 'credit_limit',
                           'as_of', 'promo_ends', 'apr_after_bp', 'deferred_interest', 'is_mortgage',
                           'include_in_payoff', 'notes', 'history_json')),
    'expected_items': (('id',), ('id', 'position', 'label', 'amount', 'direction', 'date', 'status')),
    'card_categories': (('name',), ('position', 'name', 'keywords_json', 'bank_categories_json', 'weekly_series_json',
                                    'has_own_rows', 'merchants_json')),
    'flow_groups': (('name',), ('position', 'name', 'colour', 'rule_ids_json', 'series_ids_json', 'row_names_json')),
}


def _series_rec(s: Series, pos: int) -> dict[str, Any]:
    return {'id': s.id, 'name': s.name, 'kind': s.kind.value, 'amount': s.amount,
            'schedule_json': _schedule_json(s.schedule), 'funded_by': s.funded_by, 'status': s.status.value,
            'card': s.card, 'debt': s.debt, 'grp': s.group,
            'month_amounts_json': _j([list(x) for x in s.month_amounts]),
            'cycle_amounts_json': _j(list(s.cycle_amounts)), 'active': int(s.active), 'notes': s.notes,
            'position': pos, 'generated_through': _iso(s.generated_through)}


def _row_rec(r: LedgerRow) -> dict[str, Any]:
    return {'id': r.id, 'series_id': r.series_id, 'name': r.name, 'kind': r.kind.value, 'amount': r.amount,
            'due_date': r.due_date.isoformat(), 'nominal_date': _iso(r.nominal_date),
            'order_date': r.order_date.isoformat(), 'seq': r.seq, 'state': r.state.value,
            'status_override': r.status_override.value if r.status_override else None,
            'amount_was': r.amount_was, 'overridden': int(r.overridden), 'card': r.card, 'paid': r.paid,
            'paid_txn_ids_json': _j(list(r.paid_txn_ids)), 'paid_at_change': r.paid_at_change,
            'cleared_txn_id': r.cleared_txn_id, 'cleared_on': _iso(r.cleared_on), 'note': r.note}


row_rec = _row_rec      # a row as plain values (the reconciliation keeps them to undo it)


def row_from(r: Any) -> LedgerRow:     # a sqlite3.Row or a row_rec dict
    return LedgerRow(
        id=r['id'], series_id=r['series_id'], name=r['name'], kind=Kind(r['kind']), amount=r['amount'],
        due_date=date.fromisoformat(r['due_date']), order_date=date.fromisoformat(r['order_date']), seq=r['seq'],
        nominal_date=_d(r['nominal_date']), state=RowState(r['state']),
        status_override=Status(r['status_override']) if r['status_override'] else None, amount_was=r['amount_was'],
        overridden=bool(r['overridden']), card=r['card'], paid=r['paid'],
        paid_txn_ids=tuple(str(x) for x in _loads(r['paid_txn_ids_json'])), paid_at_change=r['paid_at_change'],
        cleared_txn_id=r['cleared_txn_id'], cleared_on=_d(r['cleared_on']), note=r['note'])


def _rule_rec(r: PayeeRule, pos: int) -> dict[str, Any]:
    return {'id': r.id, 'position': pos, 'name': r.name, 'kind': r.kind.value, 'contains_json': _j(list(r.contains)),
            'regex': r.regex, 'direction': r.direction.value, 'min_amount': r.min_amount, 'max_amount': r.max_amount,
            'targets_json': _j(list(r.targets)), 'card': r.card, 'note': r.note}


def _account_rec(a: Account, pos: int) -> dict[str, Any]:
    return {'id': a.id, 'position': pos, 'name': a.name, 'kind': a.kind.value, 'balance': a.balance,
            'apr_bp': a.apr_bp, 'payment': a.payment, 'minimum': a.minimum, 'credit_limit': a.limit,
            'as_of': _iso(a.as_of), 'promo_ends': _iso(a.promo_ends), 'apr_after_bp': a.apr_after_bp,
            'deferred_interest': int(a.deferred_interest),
            'is_mortgage': int(a.is_mortgage), 'include_in_payoff': int(a.include_in_payoff), 'notes': a.notes,
            'history_json': _j([[d.isoformat(), v] for d, v in a.history])}


def _expected_rec(e: ExpectedItem, pos: int) -> dict[str, Any]:
    return {'id': e.id, 'position': pos, 'label': e.label, 'amount': e.amount, 'direction': e.direction,
            'date': e.date.isoformat(), 'status': e.status}


def _category_rec(c: CardCategory, pos: int) -> dict[str, Any]:
    return {'position': pos, 'name': c.name, 'keywords_json': _j(list(c.keywords)),
            'bank_categories_json': _j(list(c.bank_categories)), 'weekly_series_json': _j(list(c.weekly_series)),
            'has_own_rows': int(c.has_own_rows), 'merchants_json': _j(list(c.merchants))}


def _group_rec(g: FlowGroup, pos: int) -> dict[str, Any]:
    return {'position': pos, 'name': g.name, 'colour': g.colour, 'rule_ids_json': _j(list(g.rule_ids)),
            'series_ids_json': _j(list(g.series_ids)), 'row_names_json': _j(list(g.row_names))}


def _records(budget: Budget) -> dict[str, list[dict[str, Any]]]:
    return {
        'series': [_series_rec(s, i) for i, s in enumerate(budget.series)],
        'ledger_rows': [_row_rec(r) for r in budget.rows],
        'payee_rules': [_rule_rec(r, i) for i, r in enumerate(budget.rules)],
        'accounts': [_account_rec(a, i) for i, a in enumerate(budget.accounts)],
        'expected_items': [_expected_rec(e, i) for i, e in enumerate(budget.expected)],
        'card_categories': [_category_rec(c, i) for i, c in enumerate(budget.card_categories)],
        'flow_groups': [_group_rec(g, i) for i, g in enumerate(budget.flow_groups)],
    }


def _sync(conn: sqlite3.Connection, hid: int, table: str, records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Make the table hold exactly `records` for this household; only changed rows are written."""
    keys, cols = _TABLES[table]
    existing = {tuple(r[k] for k in keys): dict(r) for r in
                conn.execute('SELECT * FROM %s WHERE household_id = ?' % table, (hid,))}  # noqa: S608 - constant names
    want = {tuple(rec[k] for k in keys): rec for rec in records}
    if len(want) != len(records):
        raise ValueError('duplicate %s ids' % table)
    added = changed = 0
    placeholders = ', '.join('?' for _ in range(len(cols) + 1))
    updates = ', '.join('%s = excluded.%s' % (c, c) for c in cols if c not in keys)
    upsert = 'INSERT INTO %s (household_id, %s) VALUES (%s) ON CONFLICT (household_id, %s) DO UPDATE SET %s' % (  # noqa: S608
        table, ', '.join(cols), placeholders, ', '.join(keys), updates)   # constant names only, values are parameters
    for key, rec in want.items():
        old = existing.get(key)
        if old is not None and all(old[c] == rec[c] for c in cols):
            continue
        conn.execute(upsert, (hid, *(rec[c] for c in cols)))
        if old is None:
            added += 1
        else:
            changed += 1
    gone = [k for k in existing if k not in want]
    where = ' AND '.join('%s = ?' % k for k in keys)
    for key in gone:
        conn.execute('DELETE FROM %s WHERE household_id = ? AND %s' % (table, where), (hid, *key))  # noqa: S608
    return {'added': added, 'changed': changed, 'removed': len(gone)}


def save_budget(conn: sqlite3.Connection, hid: int, budget: Budget, *, user_id: int | None, action: str,
                row_id: str | None = None, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Store the budget (diff-based) and write one audit entry with the counts. Returns the counts per table."""
    with transaction(conn):
        cur = conn.execute(
            'UPDATE households SET currency = ?, start_balance = ?, as_of = ?, last_reconciled_through = ?, '
            'horizon_mode = ?, horizon_months = ?, horizon_end = ?, settings_json = ?, updated_at = ?, '
            'version = version + 1 WHERE id = ?',
            (budget.settings.currency, budget.start_balance, budget.as_of.isoformat(),
             _iso(budget.last_reconciled_through), budget.horizon.mode, budget.horizon.months,
             _iso(budget.horizon.end), _settings_json(budget.settings), _now(now), hid))
        if cur.rowcount != 1:
            raise KeyError(hid)
        counts = {t: _sync(conn, hid, t, recs) for t, recs in _records(budget).items()}
        detail = {t: c for t, c in counts.items() if any(c.values())}
        audit(conn, household_id=hid, user_id=user_id, action=action, row_id=row_id, detail=detail, now=now)
    return counts


class Conflict(RuntimeError):
    """The budget changed since the caller read it (the web layer answers 409)."""


def budget_version(conn: sqlite3.Connection, hid: int) -> int:
    """Grows by one with every save_budget; for optimistic edits from the web page."""
    row = conn.execute('SELECT version FROM households WHERE id = ?', (hid,)).fetchone()
    if row is None:
        raise KeyError(hid)
    return int(row[0])


def edit_budget(conn: sqlite3.Connection, hid: int, fn: Callable[[Budget], Budget], *, user_id: int | None, action: str,
                row_id: str | None = None, expected_version: int | None = None,
                now: datetime | None = None) -> tuple[Budget, int]:
    """Load, change and save the budget in one BEGIN IMMEDIATE transaction, so two edits cannot interleave.

    `fn` gets the current budget and returns the new one (it may raise; nothing is saved then). With
    `expected_version` the edit is refused with Conflict when someone saved in between. Returns the saved budget
    and its new version.
    """
    with transaction(conn, immediate=True):
        version = budget_version(conn, hid)
        if expected_version is not None and expected_version != version:
            raise Conflict('The budget was changed by someone else; reload and try again.')
        new = fn(load_budget(conn, hid))
        save_budget(conn, hid, new, user_id=user_id, action=action, row_id=row_id, now=now)
        return new, version + 1


def _loads(raw: str) -> list[Any]:
    v = json.loads(raw or '[]')
    return v if isinstance(v, list) else []


def load_budget(conn: sqlite3.Connection, hid: int) -> Budget:
    h = conn.execute('SELECT * FROM households WHERE id = ?', (hid,)).fetchone()
    if h is None:
        raise KeyError(hid)

    def rows(table: str, order: str) -> list[sqlite3.Row]:
        return conn.execute('SELECT * FROM %s WHERE household_id = ? ORDER BY %s' % (table, order),  # noqa: S608
                            (hid,)).fetchall()
    series = tuple(Series(
        id=r['id'], name=r['name'], kind=Kind(r['kind']), amount=r['amount'], schedule=_schedule(r['schedule_json']),
        funded_by=r['funded_by'], status=Status(r['status']), card=r['card'], debt=r['debt'], group=r['grp'],
        month_amounts=tuple((int(m), int(a)) for m, a in _loads(r['month_amounts_json'])),
        cycle_amounts=tuple(int(x) for x in _loads(r['cycle_amounts_json'])), active=bool(r['active']),
        notes=r['notes'], generated_through=_d(r['generated_through'])) for r in rows('series', 'position, id'))
    ledger = tuple(row_from(r) for r in rows('ledger_rows', 'order_date, seq, id'))
    rules = tuple(PayeeRule(
        id=r['id'], name=r['name'], kind=RuleKind(r['kind']), contains=tuple(_loads(r['contains_json'])),
        regex=r['regex'], direction=Direction(r['direction']), min_amount=r['min_amount'], max_amount=r['max_amount'],
        targets=tuple(_loads(r['targets_json'])), card=r['card'], note=r['note'])
        for r in rows('payee_rules', 'position, id'))
    accounts = tuple(Account(
        id=r['id'], name=r['name'], kind=AccountKind(r['kind']), balance=r['balance'], apr_bp=r['apr_bp'],
        payment=r['payment'], minimum=r['minimum'], limit=r['credit_limit'], as_of=_d(r['as_of']),
        promo_ends=_d(r['promo_ends']), apr_after_bp=r['apr_after_bp'], deferred_interest=bool(r['deferred_interest']),
        is_mortgage=bool(r['is_mortgage']), include_in_payoff=bool(r['include_in_payoff']), notes=r['notes'],
        history=tuple((date.fromisoformat(d), int(v)) for d, v in _loads(r['history_json'])))
        for r in rows('accounts', 'position, id'))
    expected = tuple(ExpectedItem(id=r['id'], label=r['label'], amount=r['amount'], direction=r['direction'],
                                  date=date.fromisoformat(r['date']), status=r['status'])
                     for r in rows('expected_items', 'position, id'))
    categories = tuple(CardCategory(
        name=r['name'], keywords=tuple(_loads(r['keywords_json'])),
        bank_categories=tuple(_loads(r['bank_categories_json'])),
        weekly_series=tuple(_loads(r['weekly_series_json'])), has_own_rows=bool(r['has_own_rows']),
        merchants=tuple(_loads(r['merchants_json'])))
        for r in rows('card_categories', 'position, name'))
    groups = tuple(FlowGroup(
        name=r['name'], colour=r['colour'], rule_ids=tuple(_loads(r['rule_ids_json'])),
        series_ids=tuple(_loads(r['series_ids_json'])), row_names=tuple(_loads(r['row_names_json'])))
        for r in rows('flow_groups', 'position, name'))
    return Budget(
        start_balance=h['start_balance'], as_of=date.fromisoformat(h['as_of']),
        horizon=Horizon(mode=h['horizon_mode'], months=h['horizon_months'], end=_d(h['horizon_end'])),
        series=series, rows=ledger, rules=rules, accounts=accounts, expected=expected,
        settings=_settings(h['settings_json'], h['currency']), card_categories=categories, flow_groups=groups,
        last_reconciled_through=_d(h['last_reconciled_through']))


def set_expected_status(conn: sqlite3.Connection, hid: int, item_id: str, status: Literal['open', 'done'], *,
                        user_id: int | None, now: datetime | None = None) -> None:
    """Only the status of an expected item (the contributor's endpoint: no other field can change)."""
    if status not in ('open', 'done'):
        raise ValueError('status is open or done')
    with transaction(conn):
        if conn.execute('UPDATE expected_items SET status = ? WHERE household_id = ? AND id = ?',
                        (status, hid, item_id)).rowcount != 1:
            raise KeyError(item_id)
        audit(conn, household_id=hid, user_id=user_id, action='expected.status', row_id=item_id,
              detail={'status': status}, now=now)


# ---------------------------------------------------------------- notes, app settings
def load_notes(conn: sqlite3.Connection, hid: int) -> str:
    row = conn.execute('SELECT notes FROM households WHERE id = ?', (hid,)).fetchone()
    if row is None:
        raise KeyError(hid)
    return str(row[0])


def save_notes(conn: sqlite3.Connection, hid: int, text: str, *, user_id: int | None,
               now: datetime | None = None) -> None:
    if len(text) > 100_000:
        raise ValueError('Notes are limited to 100,000 characters.')
    with transaction(conn):
        if conn.execute('UPDATE households SET notes = ?, updated_at = ? WHERE id = ?',
                        (text, _now(now), hid)).rowcount != 1:
            raise KeyError(hid)
        audit(conn, household_id=hid, user_id=user_id, action='notes.save', now=now)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute('SELECT value FROM app_settings WHERE key = ?', (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        conn.execute('INSERT INTO app_settings (key, value) VALUES (?, ?) '
                     'ON CONFLICT (key) DO UPDATE SET value = excluded.value', (key, value))


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    with transaction(conn):
        conn.execute('DELETE FROM app_settings WHERE key = ?', (key,))


# ---------------------------------------------------------------- audit
# Set while a request made with an access token runs (web/routes/api_v1.py): every audit entry written meanwhile
# names the token, so the log shows "by <member> via token <name>". A context variable, because the writes happen
# deep in edit_budget / save_budget and the request handler runs them in its own thread.
AUDIT_VIA: ContextVar[str | None] = ContextVar('AUDIT_VIA', default=None)


@contextmanager
def audit_via(label: str) -> Iterator[None]:
    reset = AUDIT_VIA.set(label)
    try:
        yield
    finally:
        AUDIT_VIA.reset(reset)


def audit(conn: sqlite3.Connection, *, household_id: int | None, user_id: int | None, action: str,
          row_id: str | None = None, detail: Mapping[str, Any] | None = None, imported: bool = False,
          now: datetime | None = None) -> None:
    """One audit entry. `detail` holds ids and field names, never descriptions or amounts (D-037)."""
    d = dict(detail or {})
    if AUDIT_VIA.get():
        d['via'] = AUDIT_VIA.get()
    with transaction(conn):
        conn.execute('INSERT INTO audit_log (household_id, at, user_id, action, row_id, detail_json, imported) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (household_id, _now(now), user_id, action[:60], row_id, _j(d), int(imported)))


def list_audit(conn: sqlite3.Connection, hid: int | None, *, limit: int = 200,
               after_id: int | None = None) -> list[dict[str, Any]]:
    """Newest first; with after_id, the entries after that one, oldest first. hid=None lists the server-wide
    entries (logins, user admin)."""
    sql = ('SELECT a.*, COALESCE(u.username, a.by_name) AS username FROM audit_log a LEFT JOIN users u '  # noqa: S608
           'ON u.id = a.user_id WHERE a.household_id %s %s '
           'ORDER BY a.id %s LIMIT ?') % ('IS NULL' if hid is None else '= ?',
                                          '' if after_id is None else 'AND a.id > ?',
                                          'DESC' if after_id is None else 'ASC')   # constant fragments
    args: tuple[Any, ...] = (() if hid is None else (hid,)) + (() if after_id is None else (after_id,)) + (limit,)
    return [{'id': r['id'], 'at': r['at'], 'user_id': r['user_id'], 'username': r['username'], 'action': r['action'],
             'row': r['row_id'], 'detail': json.loads(r['detail_json']), 'imported': bool(r['imported'])}
            for r in conn.execute(sql, args)]


def last_audit_id(conn: sqlite3.Connection, hid: int) -> int:
    row = conn.execute('SELECT MAX(id) FROM audit_log WHERE household_id = ?', (hid,)).fetchone()
    return int(row[0] or 0)


def purge_audit(conn: sqlite3.Connection, *, older_than: datetime) -> int:
    """Retention helper: delete audit entries older than the given time (none are deleted unless asked)."""
    with transaction(conn):
        return conn.execute('DELETE FROM audit_log WHERE at < ?', (_now(older_than),)).rowcount


# ---------------------------------------------------------------- uploads and transactions
@dataclass(frozen=True, slots=True)
class Upload:
    id: str
    kind: str
    filename: str
    uploaded_at: str
    uploaded_by: str | None
    rows: int
    date_from: date | None
    date_to: date | None
    latest_balance: int | None
    has_balance: bool
    status: str
    note: str
    hidden: bool = False


def _upload(r: sqlite3.Row | None) -> Upload | None:
    if r is None:
        return None
    return Upload(r['id'], r['kind'], r['filename'], r['uploaded_at'], r['uploaded_by'], r['rows'], _d(r['date_from']),
                  _d(r['date_to']), r['latest_balance'], bool(r['has_balance']), r['status'], r['note'],
                  bool(r['hidden']))


def create_upload(conn: sqlite3.Connection, hid: int, *, upload_id: str, kind: Literal['bank', 'card'], filename: str,
                  uploaded_by: str | None, rows: int, date_from: date | None, date_to: date | None,
                  latest_balance: int | None, has_balance: bool, status: str = 'waiting', note: str = '',
                  now: datetime | None = None) -> None:
    """Upload metadata (the raw file is never stored). `filename` must already be cleaned (io.clean_filename)."""
    with transaction(conn):
        conn.execute('INSERT INTO uploads (household_id, id, kind, filename, uploaded_at, uploaded_by, rows, '
                     'date_from, date_to, latest_balance, has_balance, status, note) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (hid, upload_id, kind, filename, _now(now), uploaded_by, rows, _iso(date_from), _iso(date_to),
                      latest_balance, int(has_balance), status, note[:500]))


def get_upload(conn: sqlite3.Connection, hid: int, upload_id: str) -> Upload | None:
    return _upload(conn.execute('SELECT * FROM uploads WHERE household_id = ? AND id = ?', (hid, upload_id)).fetchone())


def list_uploads(conn: sqlite3.Connection, hid: int, *, kind: str | None = None,
                 include_hidden: bool = False) -> list[Upload]:
    rows = conn.execute('SELECT * FROM uploads WHERE household_id = ? AND (? IS NULL OR kind = ?) '
                        'AND (? OR hidden = 0) ORDER BY uploaded_at DESC, id',
                        (hid, kind, kind, int(include_hidden))).fetchall()
    return [u for u in (_upload(r) for r in rows) if u]


def set_upload_status(conn: sqlite3.Connection, hid: int, upload_id: str,
                      status: Literal['waiting', 'imported', 'rejected'], note: str = '') -> None:
    with transaction(conn):
        if conn.execute('UPDATE uploads SET status = ?, note = ? WHERE household_id = ? AND id = ?',
                        (status, note[:500], hid, upload_id)).rowcount != 1:
            raise KeyError(upload_id)
        _refresh_history(conn, hid, (r[0] for r in conn.execute(
            'SELECT txn_id FROM upload_txns WHERE household_id = ? AND upload_id = ?', (hid, upload_id))))


@dataclass(frozen=True, slots=True)
class UploadRemoval:
    """What deleting an upload with its postings would take out of the history (#88)."""
    can_delete: bool          # waiting and no reconciliation used it; otherwise it can only be hidden
    bank: int                 # bank postings no other upload or ledger row holds
    card: int
    first: date | None
    last: date | None


def _removable(conn: sqlite3.Connection, hid: int, upload_id: str) -> tuple[list[str], list[str]]:
    used = _used_txn_ids(conn, hid)
    bank = [r[0] for r in conn.execute(
        'SELECT l.txn_id FROM upload_txns l WHERE l.household_id = ? AND l.upload_id = ? AND NOT EXISTS (SELECT 1 '
        'FROM upload_txns o WHERE o.household_id = l.household_id AND o.txn_id = l.txn_id AND o.upload_id != '
        'l.upload_id)', (hid, upload_id)) if r[0] not in used]
    card = [r[0] for r in conn.execute(
        'SELECT l.txn_id FROM card_upload_txns l WHERE l.household_id = ? AND l.upload_id = ? AND NOT EXISTS (SELECT 1 '
        'FROM card_upload_txns o WHERE o.household_id = l.household_id AND o.txn_id = l.txn_id AND o.upload_id != '
        'l.upload_id)', (hid, upload_id))]
    return bank, card


def _deletable(conn: sqlite3.Connection, hid: int, up: Upload) -> bool:
    used_by_reconciliation = conn.execute('SELECT 1 FROM reconciliations WHERE household_id = ? AND upload_id = ?',
                                          (hid, up.id)).fetchone()
    return up.status == 'waiting' and not used_by_reconciliation


def upload_removal(conn: sqlite3.Connection, hid: int, upload_id: str) -> UploadRemoval:
    """For the delete confirmation: how many postings, over which dates, deleting the upload would remove."""
    up = get_upload(conn, hid, upload_id)
    if up is None:
        raise KeyError(upload_id)
    if not _deletable(conn, hid, up):
        return UploadRemoval(False, 0, 0, None, None)
    bank, card = _removable(conn, hid, upload_id)
    days: list[str] = []
    for table, ids in (('bank_txns', bank), ('card_txns', card)):
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            days += [r[0] for r in conn.execute('SELECT posted FROM %s WHERE household_id = ? AND id IN (%s)'  # noqa: S608
                                                % (table, ','.join('?' * len(chunk))), (hid, *chunk))]
    return UploadRemoval(True, len(bank), len(card), date.fromisoformat(min(days)) if days else None,
                         date.fromisoformat(max(days)) if days else None)


def delete_upload(conn: sqlite3.Connection, hid: int, upload_id: str, *,
                  postings: bool = False) -> Literal['deleted', 'hidden']:
    """Remove an upload from the list (D-028, #88). By default it is only hidden: its postings stay in the history
    behind Report, Check-up, Recap and Subscriptions. With `postings`, a waiting upload that no reconciliation used
    is deleted with its links and the transactions nothing else holds (another upload, a cleared or paid ledger
    row), for a wrong file; an imported or rejected one, or one a reconciliation used, is still only hidden."""
    with transaction(conn, immediate=True):
        up = get_upload(conn, hid, upload_id)
        if up is None:
            raise KeyError(upload_id)
        if not postings or not _deletable(conn, hid, up):
            conn.execute('UPDATE uploads SET hidden = 1 WHERE household_id = ? AND id = ?', (hid, upload_id))
            return 'hidden'
        bank_ids = [r[0] for r in conn.execute('SELECT txn_id FROM upload_txns WHERE household_id = ? AND '
                                               'upload_id = ?', (hid, upload_id))]
        card_ids = [r[0] for r in conn.execute('SELECT txn_id FROM card_upload_txns WHERE household_id = ? AND '
                                               'upload_id = ?', (hid, upload_id))]
        conn.execute('DELETE FROM uploads WHERE household_id = ? AND id = ?', (hid, upload_id))   # links cascade
        used = {r[0] for r in conn.execute('SELECT cleared_txn_id FROM ledger_rows WHERE household_id = ? AND '
                                           'cleared_txn_id IS NOT NULL', (hid,))}
        for raw in conn.execute('SELECT paid_txn_ids_json FROM ledger_rows WHERE household_id = ?', (hid,)):
            used.update(str(x) for x in _loads(raw[0]))
        for tid in bank_ids:
            linked = conn.execute('SELECT 1 FROM upload_txns WHERE household_id = ? AND txn_id = ?',
                                  (hid, tid)).fetchone()
            if not linked and tid not in used:
                conn.execute('DELETE FROM bank_txns WHERE household_id = ? AND id = ?', (hid, tid))
        _refresh_history(conn, hid, bank_ids)
        for tid in card_ids:
            linked = conn.execute('SELECT 1 FROM card_upload_txns WHERE household_id = ? AND txn_id = ?',
                                  (hid, tid)).fetchone()
            if not linked:
                conn.execute('DELETE FROM card_txns WHERE household_id = ? AND id = ?', (hid, tid))
        return 'deleted'


def _desc_key(description: str) -> str:
    return ' '.join(description.casefold().split())


def _is_content_id(txn_id: str) -> bool:
    return txn_id.startswith('h:')


def _same_posting(conn: sqlite3.Connection, table: str, hid: int,
                  items: list[tuple[str, date, int, str]]) -> dict[str, str]:
    """#87: one file may carry the bank's transaction ids and another (an export of the same account, mapped without
    its ID column) only content ids, so the same posting arrives under two ids. For each incoming id not stored yet,
    find a stored posting with the same date, amount and description whose id is of the other kind and that no
    other incoming row already took; the n-th identical row pairs with the n-th stored one.
    Returns {incoming id: stored id}."""
    if not items:
        return {}
    lo, hi = min(i[1] for i in items).isoformat(), max(i[1] for i in items).isoformat()
    pool: dict[tuple[str, int, str], list[str]] = {}
    sql = 'SELECT id, posted, amount, description FROM %s WHERE household_id = ? AND posted BETWEEN ? AND ? ORDER BY id'
    for r in conn.execute(sql % table, (hid, lo, hi)):  # table is one of two fixed names
        pool.setdefault((r[1], r[2], _desc_key(r[3])), []).append(r[0])
    stored = {i for ids in pool.values() for i in ids}
    taken = {i[0] for i in items if i[0] in stored}
    out: dict[str, str] = {}
    for tid, posted, amount, desc in items:
        if tid in stored or tid in out:
            continue
        for cand in pool.get((posted.isoformat(), amount, _desc_key(desc)), ()):
            if cand not in taken and _is_content_id(cand) != _is_content_id(tid):
                out[tid] = cand
                taken.add(cand)
                break
    return out


def insert_txns(conn: sqlite3.Connection, hid: int, upload_id: str, txns: Iterable[BankTxn]) -> tuple[int, int]:
    """Add an upload's transactions: new ones go into bank_txns (dedupe by id across uploads, and by date, amount and
    description against a posting stored under the other kind of id, #87), and every one of them, duplicates
    included, is linked to this upload with this file's file_index. Returns (new, duplicate)."""
    new = dup = 0
    ids: list[str] = []
    txns = list(txns)
    with transaction(conn):
        same = _same_posting(conn, 'bank_txns', hid, [(t.id, t.posted, t.amount, t.description) for t in txns])
        for t in txns:
            t = replace(t, id=same.get(t.id, t.id))
            cur = conn.execute('INSERT INTO bank_txns (household_id, id, first_upload_id, posted, effective, amount, '
                               'balance, description, category, file_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                               'ON CONFLICT (household_id, id) DO NOTHING',
                               (hid, t.id, upload_id, t.posted.isoformat(), _iso(t.effective), t.amount, t.balance,
                                t.description, t.category, t.file_index))
            if cur.rowcount:
                new += 1
            else:
                dup += 1
            conn.execute('INSERT INTO upload_txns (household_id, upload_id, txn_id, file_index, balance, category) '
                         'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (household_id, upload_id, txn_id) DO UPDATE SET '
                         'file_index = excluded.file_index, balance = excluded.balance, category = excluded.category',
                         (hid, upload_id, t.id, t.file_index, t.balance, t.category))
            ids.append(t.id)
        _refresh_history(conn, hid, ids)
    return new, dup


def _refresh_history(conn: sqlite3.Connection, hid: int, txn_ids: Iterable[str]) -> None:
    """The history row of a transaction takes the values of the newest upload holding it that was not rejected
    (a corrected re-export wins over the file it replaces). Rejected-only transactions keep their values but are
    left out of list_history."""
    for tid in set(txn_ids):
        best = conn.execute(
            "SELECT l.balance, l.category FROM upload_txns l JOIN uploads u ON u.household_id = l.household_id AND "
            "u.id = l.upload_id WHERE l.household_id = ? AND l.txn_id = ? AND u.status != 'rejected' "
            'ORDER BY u.uploaded_at DESC, u.id DESC LIMIT 1', (hid, tid)).fetchone()
        if best is not None:
            conn.execute('UPDATE bank_txns SET balance = ?, category = ? WHERE household_id = ? AND id = ?',
                         (best[0], best[1], hid, tid))


def _txn(r: sqlite3.Row, file_index: int | None = None) -> BankTxn:
    return BankTxn(id=r['id'], posted=date.fromisoformat(r['posted']), amount=r['amount'], description=r['description'],
                   balance=r['balance'], category=r['category'],
                   file_index=r['file_index'] if file_index is None else file_index, effective=_d(r['effective']))


def upload_txns(conn: sqlite3.Connection, hid: int, upload_id: str) -> list[BankTxn]:
    """One upload's complete set in its own file order, with its own file_index (the input to reconcile.prepare)."""
    rows = conn.execute('SELECT b.*, l.file_index AS link_index, l.balance AS link_balance, '
                        'l.category AS link_category FROM upload_txns l JOIN bank_txns b ON '
                        'b.household_id = l.household_id AND b.id = l.txn_id WHERE l.household_id = ? AND '
                        'l.upload_id = ? ORDER BY l.file_index', (hid, upload_id)).fetchall()
    # this file's own values: another upload of the same postings may have different running balances
    return [replace(_txn(r, r['link_index']), balance=r['link_balance'], category=r['link_category']) for r in rows]


# Nested EXISTS (not a JOIN) so both lookups start from the transaction's links (index upload_txns_txn).
_IN_HISTORY = ("(NOT EXISTS (SELECT 1 FROM upload_txns l WHERE l.household_id = b.household_id AND l.txn_id = b.id) "
               "OR EXISTS (SELECT 1 FROM upload_txns l WHERE l.household_id = b.household_id AND l.txn_id = b.id "
               "AND EXISTS (SELECT 1 FROM uploads u WHERE u.household_id = l.household_id AND u.id = l.upload_id "
               "AND u.status != 'rejected')))")


def list_history(conn: sqlite3.Connection, hid: int) -> list[BankTxn]:
    """Every bank transaction of the household once (deduplicated), oldest first: the history behind Check-up,
    Flow and caps. A transaction that only rejected uploads contain is left out."""
    rows = conn.execute('SELECT * FROM bank_txns b WHERE household_id = ? AND %s ORDER BY posted, id' % _IN_HISTORY,  # noqa: S608
                        (hid,)).fetchall()
    return [_txn(r) for r in rows]


def history_counts(conn: sqlite3.Connection, hid: int) -> dict[str, int]:
    """What a backup restore would delete, for the dry run. `uploads` counts all of them (a restore removes hidden
    ones too) and `uploads_hidden` says how many of those are hidden; `bank_txns_in_history` excludes
    rejected-only transactions."""
    def count(sql: str) -> int:
        return int(conn.execute(sql, (hid,)).fetchone()[0])
    return {
        'rows': count('SELECT COUNT(*) FROM ledger_rows WHERE household_id = ?'),
        'bank_txns': count('SELECT COUNT(*) FROM bank_txns WHERE household_id = ?'),
        'bank_txns_in_history': count('SELECT COUNT(*) FROM bank_txns b WHERE household_id = ? AND %s' % _IN_HISTORY),  # noqa: S608
        'card_txns': count('SELECT COUNT(*) FROM card_txns WHERE household_id = ?'),
        'uploads': count('SELECT COUNT(*) FROM uploads WHERE household_id = ?'),
        'uploads_hidden': count('SELECT COUNT(*) FROM uploads WHERE household_id = ? AND hidden = 1'),
        'reconciliations': count('SELECT COUNT(*) FROM reconciliations WHERE household_id = ?'),
        'profiles': count('SELECT COUNT(*) FROM mapping_profiles WHERE household_id = ?'),
    }


def seen_txn_ids(conn: sqlite3.Connection, hid: int) -> frozenset[str]:
    """Transactions already used by a past reconciliation: every txn of an upload that was reconciled."""
    rows = conn.execute('SELECT DISTINCT l.txn_id FROM upload_txns l JOIN reconciliations r ON '
                        'r.household_id = l.household_id AND r.upload_id = l.upload_id WHERE l.household_id = ?',
                        (hid,)).fetchall()
    return frozenset(r[0] for r in rows)


def insert_card_txns(conn: sqlite3.Connection, hid: int, upload_id: str | None,
                     txns: Iterable[CardTxn]) -> tuple[int, int]:
    new = dup = 0
    txns = list(txns)
    with transaction(conn):
        same = _same_posting(conn, 'card_txns', hid, [(t.id, t.posted, t.amount, t.description) for t in txns])
        for t in txns:
            t = replace(t, id=same.get(t.id, t.id))
            cur = conn.execute('INSERT INTO card_txns (household_id, id, first_upload_id, purchased, posted, amount, '
                               'description, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
                               'ON CONFLICT (household_id, id) DO NOTHING',
                               (hid, t.id, upload_id, t.purchased.isoformat(), t.posted.isoformat(), t.amount,
                                t.description, t.category))
            new, dup = (new + 1, dup) if cur.rowcount else (new, dup + 1)
            if upload_id is not None:
                conn.execute('INSERT INTO card_upload_txns (household_id, upload_id, txn_id) VALUES (?, ?, ?) '
                             'ON CONFLICT DO NOTHING', (hid, upload_id, t.id))
    return new, dup


def _used_txn_ids(conn: sqlite3.Connection, hid: int) -> set[str]:
    """Bank transactions a ledger row points at (cleared by, or part-paid by)."""
    used = {r[0] for r in conn.execute('SELECT cleared_txn_id FROM ledger_rows WHERE household_id = ? AND '
                                       'cleared_txn_id IS NOT NULL', (hid,))}
    for raw in conn.execute('SELECT paid_txn_ids_json FROM ledger_rows WHERE household_id = ?', (hid,)):
        used.update(str(x) for x in _loads(raw[0]))
    return used


def _pairs(rows: Iterable[sqlite3.Row]) -> list[tuple[str, str]]:
    """(bank id, content id) pairs of the same posting stored twice (#87): same date, amount and description, one
    under the bank's id and one under a content id; the n-th of each kind pair up."""
    groups: dict[tuple[str, int, str], tuple[list[str], list[str]]] = {}
    for r in rows:
        real, content = groups.setdefault((r['posted'], r['amount'], _desc_key(r['description'])), ([], []))
        (content if _is_content_id(r['id']) else real).append(r['id'])
    return [p for real, content in groups.values() for p in zip(sorted(real), sorted(content), strict=False)]


def merge_duplicate_txns(conn: sqlite3.Connection, hid: int) -> tuple[int, int]:
    """Fold postings stored twice before #87 was fixed into one: the copy a ledger row points at (else the one with
    the bank's id, or a card purchase moved by hand) stays, the other's upload links move to it and it is deleted.
    Safe to run again. Returns (bank, card) postings removed."""
    bank = card = 0
    with transaction(conn, immediate=True):
        used = _used_txn_ids(conn, hid)
        rows = conn.execute('SELECT id, posted, amount, description FROM bank_txns WHERE household_id = ?',
                            (hid,)).fetchall()
        for real, content in _pairs(rows):
            if real in used and content in used:
                continue
            keep, drop = (content, real) if content in used else (real, content)
            conn.execute('INSERT INTO upload_txns (household_id, upload_id, txn_id, file_index, balance, category) '
                         'SELECT household_id, upload_id, ?, file_index, balance, category FROM upload_txns WHERE '
                         'household_id = ? AND txn_id = ? ON CONFLICT DO NOTHING', (keep, hid, drop))
            conn.execute('DELETE FROM upload_txns WHERE household_id = ? AND txn_id = ?', (hid, drop))
            conn.execute('DELETE FROM bank_txns WHERE household_id = ? AND id = ?', (hid, drop))
            _refresh_history(conn, hid, [keep])
            bank += 1
        rows = conn.execute('SELECT id, posted, amount, description, by_hand FROM card_txns WHERE household_id = ?',
                            (hid,)).fetchall()
        by_hand = {r['id'] for r in rows if r['by_hand']}
        for real, content in _pairs(rows):
            keep, drop = (content, real) if content in by_hand and real not in by_hand else (real, content)
            conn.execute('INSERT INTO card_upload_txns (household_id, upload_id, txn_id) SELECT household_id, '
                         'upload_id, ? FROM card_upload_txns WHERE household_id = ? AND txn_id = ? '
                         'ON CONFLICT DO NOTHING', (keep, hid, drop))
            conn.execute('DELETE FROM card_upload_txns WHERE household_id = ? AND txn_id = ?', (hid, drop))
            conn.execute('DELETE FROM card_txns WHERE household_id = ? AND id = ?', (hid, drop))
            card += 1
    return bank, card


def redate_card_txns(conn: sqlite3.Connection, hid: int, date_of: Callable[[str, date], date]) -> int:
    """Stored purchases still dated by posting date get the purchase date `date_of(description, posted)` finds
    (#63: uploads before the default pattern, which re-uploading cannot fix since they are duplicates). Returns how
    many changed."""
    n = 0
    with transaction(conn):
        for r in conn.execute('SELECT id, posted, description FROM card_txns WHERE household_id = ? '
                              'AND purchased = posted', (hid,)).fetchall():
            posted = date.fromisoformat(r['posted'])
            bought = date_of(r['description'], posted)
            if bought != posted:
                conn.execute('UPDATE card_txns SET purchased = ? WHERE household_id = ? AND id = ?',
                             (bought.isoformat(), hid, r['id']))
                n += 1
    return n


def list_card_txns(conn: sqlite3.Connection, hid: int) -> list[CardTxn]:
    rows = conn.execute('SELECT * FROM card_txns WHERE household_id = ? ORDER BY purchased, id', (hid,)).fetchall()
    return [CardTxn(id=r['id'], purchased=date.fromisoformat(r['purchased']), posted=date.fromisoformat(r['posted']),
                    amount=r['amount'], description=r['description'], category=r['category'],
                    by_hand=bool(r['by_hand'])) for r in rows]


def set_card_category(conn: sqlite3.Connection, hid: int, txn_id: str, category: str, *, by_hand: bool) -> bool:
    """Put one stored card purchase in a category; False when there is no such purchase."""
    with transaction(conn):
        cur = conn.execute('UPDATE card_txns SET category = ?, by_hand = ? WHERE household_id = ? AND id = ?',
                           (category, int(by_hand), hid, txn_id))
    return cur.rowcount > 0


# ---------------------------------------------------------------- reconciliations
@dataclass(frozen=True, slots=True)
class Reconciliation:
    id: str
    at: str
    by_user: str | None
    upload_id: str | None
    through: date
    new_balance: int
    result: dict[str, Any]
    report_md: str


def save_reconciliation(conn: sqlite3.Connection, hid: int, *, rec_id: str, by_user: str | None, upload_id: str | None,
                        through: date, new_balance: int, result: Mapping[str, Any], report_md: str,
                        now: datetime | None = None) -> None:
    """`result` holds `cleared` [{row, txn, how}] and `accepted` [proposal ids] (the backup keeps those two)."""
    with transaction(conn):
        conn.execute('INSERT INTO reconciliations (household_id, id, at, by_user, upload_id, through, new_balance, '
                     'result_json, report_md) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (hid, rec_id, _now(now), by_user, upload_id, through.isoformat(), new_balance, _j(dict(result)),
                      report_md))


def delete_reconciliation(conn: sqlite3.Connection, hid: int, rec_id: str) -> None:
    with transaction(conn):
        conn.execute('DELETE FROM reconciliations WHERE household_id = ? AND id = ?', (hid, rec_id))


def _reconciliation(r: sqlite3.Row) -> Reconciliation:
    return Reconciliation(r['id'], r['at'], r['by_user'], r['upload_id'], date.fromisoformat(r['through']),
                          r['new_balance'], json.loads(r['result_json']), r['report_md'])


def get_reconciliation(conn: sqlite3.Connection, hid: int, rec_id: str) -> Reconciliation | None:
    r = conn.execute('SELECT * FROM reconciliations WHERE household_id = ? AND id = ?', (hid, rec_id)).fetchone()
    return _reconciliation(r) if r else None


def list_reconciliations(conn: sqlite3.Connection, hid: int) -> list[Reconciliation]:
    rows = conn.execute('SELECT * FROM reconciliations WHERE household_id = ? ORDER BY at DESC, rowid DESC',
                        (hid,)).fetchall()
    return [_reconciliation(r) for r in rows]


# ---------------------------------------------------------------- mapping profiles (PK household_id, id)
def save_profile(conn: sqlite3.Connection, hid: int, profile: MappingProfile) -> None:
    if profile.builtin:
        raise ValueError('built-in profiles live in code and are never stored')
    with transaction(conn):
        conn.execute('INSERT INTO mapping_profiles (household_id, id, name, kind, header_json, mapping_json) '
                     'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (household_id, id) DO UPDATE SET name = excluded.name, '
                     'kind = excluded.kind, header_json = excluded.header_json, mapping_json = excluded.mapping_json',
                     (hid, profile.id, profile.name, profile.kind, _j(list(profile.header)),
                      _j(billsystem._mapping_doc(profile.mapping))))


def _profile(r: sqlite3.Row) -> MappingProfile:
    return MappingProfile(id=r['id'], name=r['name'], kind=r['kind'],
                          mapping=billsystem._mapping_from(r['kind'], json.loads(r['mapping_json'])),
                          header=tuple(_loads(r['header_json'])))


def get_profile(conn: sqlite3.Connection, hid: int, profile_id: str) -> MappingProfile | None:
    r = conn.execute('SELECT * FROM mapping_profiles WHERE household_id = ? AND id = ?', (hid, profile_id)).fetchone()
    return _profile(r) if r else None


def list_profiles(conn: sqlite3.Connection, hid: int, *, kind: str | None = None) -> list[MappingProfile]:
    rows = conn.execute('SELECT * FROM mapping_profiles WHERE household_id = ? AND (? IS NULL OR kind = ?) '
                        'ORDER BY name, id', (hid, kind, kind)).fetchall()
    return [_profile(r) for r in rows]


def delete_profile(conn: sqlite3.Connection, hid: int, profile_id: str) -> None:
    with transaction(conn):
        if conn.execute('DELETE FROM mapping_profiles WHERE household_id = ? AND id = ?',
                        (hid, profile_id)).rowcount != 1:
            raise KeyError(profile_id)


# ---------------------------------------------------------------- backup extras
def load_extras(conn: sqlite3.Connection, hid: int) -> billsystem.BackupExtras:
    """Everything outside the core Budget, in backup (document) form."""
    m = billsystem._m
    bank = [(_txn(r), r['first_upload_id']) for r in
            conn.execute('SELECT * FROM bank_txns WHERE household_id = ? ORDER BY posted, id', (hid,))]
    links = [(r[0], r[1], r[2], r[3], r[4]) for r in conn.execute(
        'SELECT upload_id, txn_id, file_index, balance, category FROM upload_txns WHERE household_id = ? '
        'ORDER BY upload_id, file_index, txn_id', (hid,))]
    cards = [(t, r['first_upload_id']) for r, t in zip(
        conn.execute('SELECT * FROM card_txns WHERE household_id = ? ORDER BY purchased, id', (hid,)).fetchall(),
        list_card_txns(conn, hid), strict=True)]
    uploads: list[dict[str, object]] = []
    for u in sorted(list_uploads(conn, hid, include_hidden=True), key=lambda x: (x.uploaded_at, x.id)):
        uploads.append({'id': u.id, 'kind': u.kind, 'filename': u.filename, 'uploaded_at': u.uploaded_at,
                        'uploaded_by': u.uploaded_by, 'rows': u.rows, 'from': _iso(u.date_from), 'to': _iso(u.date_to),
                        'latest_balance': None if u.latest_balance is None else m(u.latest_balance),
                        'has_balance': u.has_balance, 'hidden': u.hidden, 'status': u.status, 'note': u.note})
    recs: list[dict[str, object]] = []
    for r in sorted(list_reconciliations(conn, hid), key=lambda x: (x.at, x.id)):
        recs.append({'id': r.id, 'at': r.at, 'by': r.by_user, 'upload': r.upload_id, 'through': r.through.isoformat(),
                     'new_balance': m(r.new_balance), 'cleared': list(r.result.get('cleared', [])),
                     'accepted': list(r.result.get('accepted', [])), 'report': r.report_md})
    entries = []
    for a in reversed(list_audit(conn, hid, limit=1_000_000)):
        entries.append({'at': a['at'], 'by': a['username'], 'action': a['action'], 'row': a['row'],
                        'detail': a['detail'], 'imported': a['imported']})
    return billsystem.BackupExtras(
        bank_txns=tuple(bank), upload_links=tuple(links), card_txns=tuple(cards), uploads=tuple(uploads),
        reconciliations=tuple(recs), mapping_profiles=tuple(list_profiles(conn, hid)), notes=load_notes(conn, hid),
        audit=tuple(entries), members=tuple((x.username, x.role) for x in list_members(conn, hid)))


def _audit_keys(conn: sqlite3.Connection, hid: int) -> set[tuple[Any, ...]]:
    return {(r['at'], r['action'], r['row_id'], r['detail_json']) for r in conn.execute(
        'SELECT at, action, row_id, detail_json FROM audit_log WHERE household_id = ?', (hid,))}


def save_extras(conn: sqlite3.Connection, hid: int, extras: billsystem.BackupExtras, *,
                known_audit: set[tuple[Any, ...]] | None = None) -> None:
    """Restore the backup parts into an emptied household. Members are NOT created (D-026); audit entries are
    stored as imported. `known_audit` are the household's audit entries from before the restore started."""
    cents = billsystem._cents
    with transaction(conn):
        for u in extras.uploads:
            conn.execute('INSERT INTO uploads (household_id, id, kind, filename, uploaded_at, uploaded_by, rows, '
                         'date_from, date_to, latest_balance, has_balance, hidden, status, note) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (hid, u['id'], u['kind'], u.get('filename') or '', u['uploaded_at'], u.get('uploaded_by'),
                          int(str(u.get('rows') or 0)), u.get('from'), u.get('to'),
                          None if u.get('latest_balance') is None else cents(u['latest_balance']),
                          int(bool(u.get('has_balance'))), int(bool(u.get('hidden'))), u['status'],
                          u.get('note') or ''))
        for t, upload in extras.bank_txns:
            conn.execute('INSERT INTO bank_txns (household_id, id, first_upload_id, posted, effective, amount, '
                         'balance, description, category, file_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (hid, t.id, upload, t.posted.isoformat(), _iso(t.effective), t.amount, t.balance,
                          t.description, t.category, t.file_index))
        for upload, txn, index, link_balance, link_category in extras.upload_links:
            conn.execute('INSERT INTO upload_txns (household_id, upload_id, txn_id, file_index, balance, category) '
                         'VALUES (?, ?, ?, ?, ?, ?)', (hid, upload, txn, index, link_balance, link_category))
        for c, upload in extras.card_txns:
            conn.execute('INSERT INTO card_txns (household_id, id, first_upload_id, purchased, posted, amount, '
                         'description, category, by_hand) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (hid, c.id, upload, c.purchased.isoformat(), c.posted.isoformat(), c.amount, c.description,
                          c.category, int(c.by_hand)))
            if upload is not None and get_upload(conn, hid, upload) is not None:
                conn.execute('INSERT INTO card_upload_txns (household_id, upload_id, txn_id) VALUES (?, ?, ?)',
                             (hid, upload, c.id))
        for r in extras.reconciliations:
            conn.execute('INSERT INTO reconciliations (household_id, id, at, by_user, upload_id, through, new_balance, '
                         'result_json, report_md) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (hid, r['id'], r['at'], r.get('by'), r.get('upload'), r['through'], cents(r['new_balance']),
                          _j({'cleared': r.get('cleared') or [], 'accepted': r.get('accepted') or []}),
                          r.get('report') or ''))
        for p in extras.mapping_profiles:
            save_profile(conn, hid, p)
        conn.execute('UPDATE households SET notes = ? WHERE id = ?', (extras.notes, hid))
        # The audit log survives a wipe, so a backup restored into the same household finds its own entries
        # still there: those are skipped (same time, action, row and detail) instead of being duplicated.
        existing = _audit_keys(conn, hid) if known_audit is None else known_audit
        for a in extras.audit:
            detail = dict(a.get('detail') or {})   # type: ignore[call-overload]
            if (a['at'], a['action'], a.get('row'), _j(detail)) in existing:
                continue
            # the username is kept as text: a restored entry is never linked to whichever user has that name now
            conn.execute('INSERT INTO audit_log (household_id, at, user_id, by_name, action, row_id, detail_json, '
                         'imported) VALUES (?, ?, NULL, ?, ?, ?, ?, 1)',
                         (hid, a['at'], a.get('by'), a['action'], a.get('row'), _j(detail)))


# ---------------------------------------------------------------- wipe, backup, import
_WIPE = ('ledger_rows', 'series', 'payee_rules', 'accounts', 'expected_items', 'card_categories', 'flow_groups',
         'upload_txns', 'card_upload_txns', 'uploads', 'bank_txns', 'card_txns', 'mapping_profiles', 'reconciliations')


def wipe_household(conn: sqlite3.Connection, hid: int, *, user_id: int | None = None, today: date | None = None,
                   now: datetime | None = None) -> None:
    """Delete the household's budget data and history. The household, its members and the audit log stay."""
    day = today or (now or datetime.now(UTC)).date()
    with transaction(conn):
        if get_household(conn, hid) is None:
            raise KeyError(hid)
        for table in _WIPE:
            conn.execute('DELETE FROM %s WHERE household_id = ?' % table, (hid,))  # noqa: S608 - constant names
        # a Plaid link stays (it is a setting, not budget data); the next sync starts over from Plaid's history
        conn.execute('DELETE FROM plaid_txns WHERE household_id = ?', (hid,))
        conn.execute('UPDATE plaid_items SET cursor = NULL WHERE household_id = ?', (hid,))
        conn.execute("UPDATE households SET start_balance = 0, as_of = ?, last_reconciled_through = NULL, "
                     "horizon_mode = 'rolling', horizon_months = 15, horizon_end = NULL, settings_json = '{}', "
                     "notes = '', updated_at = ? WHERE id = ?", (day.isoformat(), _now(now), hid))
        audit(conn, household_id=hid, user_id=user_id, action='household.wipe', now=now)


def backup(conn: sqlite3.Connection, hid: int, *, fmt: Literal['yaml', 'json'] = 'yaml', app_version: str,
           exported_at: datetime | None = None, user_id: int | None = None) -> str:
    """The full backup of a household (bill system + backup section) as YAML or JSON text; the export is audited."""
    from hbudget.io.export import backup as render

    h = get_household(conn, hid)
    if h is None:
        raise KeyError(hid)
    text = render(load_budget(conn, hid), load_extras(conn, hid), fmt=fmt,
                  exported_at=exported_at or datetime.now(UTC), app_version=app_version, household_name=h.name)
    audit(conn, household_id=hid, user_id=user_id, action='backup.export', detail={'format': fmt})
    return text


def import_bill_system(conn: sqlite3.Connection, hid: int, data: bytes, filename: str, *, today: date,
                       user_id: int | None, mode: Literal['replace', 'merge'] = 'replace') -> billsystem.BillSystem:
    """Import a bill-system file or a backup into the household.

    replace: the household's budget and history are replaced (a backup restores everything except memberships).
    merge (no backup section): series, rules, accounts, card categories and flow groups are added or updated by id;
    updated series are regenerated from `today`; nothing is deleted.
    """
    bs = billsystem.load(data, filename, today=today)
    name = str(billsystem._obj(bs.doc, 'household').get('name') or '')
    with transaction(conn):
        if get_household(conn, hid) is None:
            raise KeyError(hid)
        if mode == 'replace':
            known = _audit_keys(conn, hid)        # before the wipe and import add their own entries
            wipe_household(conn, hid, user_id=user_id, today=today)
            if name:
                rename_household(conn, hid, name)
            save_budget(conn, hid, bs.budget, user_id=user_id, action='import.replace')
            if bs.extras is not None:
                save_extras(conn, hid, bs.extras, known_audit=known)
        elif mode == 'merge':
            if bs.extras is not None:
                raise ImportProblem('A backup can only replace a household, not be merged into one.')
            merged = _merge(load_budget(conn, hid), bs.budget, today=today)
            save_budget(conn, hid, merged, user_id=user_id, action='import.merge')
        else:
            raise ValueError('mode is replace or merge')
    return bs


def _not_before(s: Series, first: date | None) -> Series:
    """A merged series whose schedule starts before the household's first row of it starts at that row instead,
    when the row is one of its occurrences: a bill paid early and gone from the household's rows is not made again
    (#64)."""
    if first is None or s.schedule.start >= first:
        return s
    if not any(o.nominal == first for o in occurrences(s.schedule, s.schedule.start, first)):
        return s
    return replace(s, schedule=replace(s.schedule, start=first))


def _merge(base: Budget, incoming: Budget, *, today: date) -> Budget:
    def upsert(old: Sequence[Any], new: Sequence[Any], key: str) -> tuple[Any, ...]:
        out = {getattr(x, key): x for x in old}
        for x in new:
            out[getattr(x, key)] = x
        return tuple(out.values())

    old_ids = {s.id for s in base.series}
    first_row: dict[str, date] = {}
    for r in base.rows:
        if r.series_id in old_ids:
            d = r.nominal_date or r.due_date
            first_row[r.series_id] = min(d, first_row.get(r.series_id, d))
    incoming = replace(incoming, series=tuple(_not_before(s, first_row.get(s.id)) for s in incoming.series))
    series = []
    by_old = base.series_by_id()
    for s in upsert(base.series, incoming.series, 'id'):
        if s.id in by_old and s is not by_old[s.id]:
            s = replace(s, generated_through=by_old[s.id].generated_through)   # keep the watermark of the old series
        elif s.id not in by_old:
            s = replace(s, generated_through=None)    # a new series: generate() below adds its rows
        series.append(s)
    b = replace(base, series=tuple(series), rules=upsert(base.rules, incoming.rules, 'id'),
                accounts=upsert(base.accounts, incoming.accounts, 'id'),
                card_categories=upsert(base.card_categories, incoming.card_categories, 'name'),
                flow_groups=upsert(base.flow_groups, incoming.flow_groups, 'name'))
    for s in incoming.series:
        if s.id in old_ids:
            b = regenerate_series(b, s.id, today)
    return generate(b, horizon_end(b))
