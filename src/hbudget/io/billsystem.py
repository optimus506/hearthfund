"""The bill-system YAML/JSON format and the full backup (ARCHITECTURE 12.5, 13).

YAML is read with a SafeLoader that keeps dates as strings, rejects anchors and aliases before anything is built,
and caps the nesting depth. The same schema validation and semantic checks apply to YAML and JSON.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from yaml.events import AliasEvent

from hbudget.core import money
from hbudget.core.dates import clamp_day
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
    Cents,
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
    slug,
)

from . import ImportProblem
from .bank import BankMapping, MappingProfile
from .workbook_import import WorkbookMapping

__all__ = ['FORMAT', 'MAX_BYTES', 'MAX_DEPTH', 'VERSION', 'BackupExtras', 'BillSystem', 'bills_csv', 'check',
           'dump_json', 'dump_yaml', 'from_budget', 'load', 'parse', 'schema', 'to_budget', 'upgrade', 'validate']

FORMAT = 'household-budget/bill-system'
VERSION = 1
MAX_BYTES = 5 * 1024 * 1024
MAX_DEPTH = 32
WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
SECTIONS = (('income', Kind.INCOME), ('bills', Kind.BILL), ('spending', Kind.SPENDING))


@dataclass(frozen=True, slots=True)
class BackupExtras:
    bank_txns: tuple[tuple[BankTxn, str | None], ...] = ()   # (txn, first upload id)
    # (upload id, txn id, file_index, balance in that file, category in that file)
    upload_links: tuple[tuple[str, str, int, Cents | None, str], ...] = ()
    card_txns: tuple[tuple[CardTxn, str | None], ...] = ()
    uploads: tuple[dict[str, object], ...] = ()
    reconciliations: tuple[dict[str, object], ...] = ()
    mapping_profiles: tuple[MappingProfile, ...] = ()
    notes: str = ''
    audit: tuple[dict[str, object], ...] = ()
    members: tuple[tuple[str, str], ...] = ()                # (username, role)


@dataclass(frozen=True, slots=True)
class BillSystem:
    doc: dict[str, object]
    budget: Budget
    extras: BackupExtras | None
    warnings: tuple[str, ...]


# ---------------------------------------------------------------- parsing
class _Loader(yaml.SafeLoader):
    """SafeLoader without the timestamp resolver, without anchors/aliases, with a depth cap."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise ImportProblem('YAML aliases (*name) are not allowed in a bill-system file.')
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if getattr(event, 'anchor', None):
            raise ImportProblem('YAML anchors (&name) are not allowed in a bill-system file.')
        self._depth += 1
        try:
            if self._depth > MAX_DEPTH:
                raise ImportProblem('The file is nested more than %d levels deep.' % MAX_DEPTH)
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


_Loader.yaml_implicit_resolvers = {
    key: [r for r in resolvers if r[0] != 'tag:yaml.org,2002:timestamp']
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _depth(obj: object) -> int:
    deepest, stack = 0, [(obj, 1)]
    while stack:
        node, d = stack.pop()
        deepest = max(deepest, d)
        if isinstance(node, dict):
            stack.extend((v, d + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, d + 1) for v in node)
    return deepest


def parse(data: bytes, filename: str = '') -> dict[str, object]:
    """YAML or JSON bytes -> a plain document (not yet validated)."""
    if len(data) > MAX_BYTES:
        raise ImportProblem('The file is larger than %d MB.' % (MAX_BYTES // (1024 * 1024)))
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ImportProblem('The file is not UTF-8 text.') from None
    if not text.strip():
        raise ImportProblem('The file is empty.')
    is_json = filename.lower().endswith('.json') or text.lstrip().startswith('{')
    try:
        doc = json.loads(text) if is_json else yaml.load(text, Loader=_Loader)  # noqa: S506 - restricted SafeLoader
    except RecursionError:
        raise ImportProblem('The file is nested too deeply.') from None
    except json.JSONDecodeError as e:
        raise ImportProblem('The JSON is not valid (%s).' % e.msg, line=e.lineno) from None
    except yaml.YAMLError as e:
        mark = getattr(e, 'problem_mark', None)
        raise ImportProblem('The YAML is not valid (%s).' % (getattr(e, 'problem', None) or 'syntax error'),
                            line=mark.line + 1 if mark else None) from None
    if not isinstance(doc, dict):
        raise ImportProblem('The file must contain a bill-system document (a mapping at the top level).')
    if _depth(doc) > MAX_DEPTH:
        raise ImportProblem('The file is nested more than %d levels deep.' % MAX_DEPTH)
    return doc


@cache
def schema() -> dict[str, Any]:
    text = resources.files('hbudget').joinpath('schema/bill-system.schema.json').read_text('utf-8')
    return cast(dict[str, Any], json.loads(text))


def _path(parts: Iterable[object]) -> str:
    out = ''
    for p in parts:
        out += '[%d]' % p if isinstance(p, int) else ('.' if out else '') + str(p)
    return out or '(top level)'


def validate(doc: dict[str, object]) -> list[str]:
    """Schema errors with JSON paths, e.g. "bills[3].schedule.day: 32 is greater than the maximum of 31"."""
    validator = Draft202012Validator(schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(doc), key=lambda e: [str(x) for x in e.absolute_path])
    return ['%s: %s' % (_path(e.absolute_path), e.message) for e in errors]


def upgrade(doc: dict[str, object]) -> dict[str, object]:
    """Migrate older documents to the current version (there is only version 1 so far)."""
    version = doc.get('version')
    if isinstance(version, int) and not isinstance(version, bool) and version > VERSION:
        raise ImportProblem('This file was made by a newer version of the app (format version %d).' % version)
    return doc


# ---------------------------------------------------------------- semantic checks
def _cents(v: object) -> Cents:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError('not an amount')
    return money.parse(v)


def _bp(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError('not a percentage')
    return money.percent_to_bp(v)


def _list(doc: Mapping[str, object], key: str) -> list[dict[str, Any]]:
    v = doc.get(key)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _obj(doc: Mapping[str, object], key: str) -> dict[str, Any]:
    v = doc.get(key)
    return v if isinstance(v, dict) else {}


def check(doc: dict[str, object]) -> list[str]:
    """Semantic checks after the schema (13.5): references, unique ids, two decimals, safe regexes."""
    from hbudget.core.rules import compile_safe

    errors: list[str] = []

    def money_at(path: str, v: object) -> None:
        if v is None:
            return
        try:
            _cents(v)
        except (ValueError, TypeError):
            errors.append('%s: %r is not an amount with at most two decimals' % (path, v))

    def pct_at(path: str, v: object) -> None:
        if v is None:
            return
        try:
            _bp(v)
        except (ValueError, TypeError):
            errors.append('%s: %r is not a percentage with at most two decimals' % (path, v))

    series_ids: dict[str, Kind] = {}
    for section, kind in SECTIONS:
        for i, s in enumerate(_list(doc, section)):
            path = '%s[%d]' % (section, i)
            sid = s.get('id')
            if sid in series_ids:
                errors.append('%s.id: "%s" is used twice' % (path, sid))
            elif isinstance(sid, str):
                series_ids[sid] = kind
            money_at(path + '.amount', s.get('amount'))
            seen_months: set[int] = set()
            for k, v in _obj(s, 'seasonal').items():
                money_at('%s.seasonal.%s' % (path, k), v)
                try:
                    months = _months(str(k))
                except ValueError as e:
                    errors.append('%s.seasonal.%s: %s' % (path, k, e))
                    continue
                if months & seen_months:
                    errors.append('%s.seasonal.%s: overlaps another range (month %s)'
                                  % (path, k, ', '.join(str(m) for m in sorted(months & seen_months))))
                seen_months |= months
            for j, v in enumerate(s.get('cycle') or []):
                money_at('%s.cycle[%d]' % (path, j), v)
            days = _obj(s, 'schedule').get('days')
            if isinstance(days, list) and len(set(days)) != len(days):
                errors.append('%s.schedule.days: the two days must differ' % path)
    accounts = {a.get('id'): a.get('kind') for a in _list(doc, 'accounts')}
    if len(accounts) != len(_list(doc, 'accounts')):
        errors.append('accounts: an id is used twice')
    cards = {k for k, v in accounts.items() if v == 'credit_card'}
    debts = {k for k, v in accounts.items() if v not in ('checking', 'savings')}
    for i, a in enumerate(_list(doc, 'accounts')):
        for k in ('balance', 'payment', 'minimum', 'limit'):
            money_at('accounts[%d].%s' % (i, k), a.get(k))
        for k in ('apr', 'apr_after'):
            pct_at('accounts[%d].%s' % (i, k), a.get(k))
    for section, _kind in SECTIONS:
        for i, s in enumerate(_list(doc, section)):
            path = '%s[%d]' % (section, i)
            fb = s.get('funded_by')
            if fb is not None and series_ids.get(fb) is not Kind.INCOME:
                errors.append('%s.funded_by: "%s" is not an income id' % (path, fb))
            if s.get('card') is not None and s.get('card') not in cards:
                errors.append('%s.card: "%s" is not a credit-card account' % (path, s.get('card')))
            if s.get('debt') is not None and s.get('debt') not in debts:
                errors.append('%s.debt: "%s" is not a debt account' % (path, s.get('debt')))
    rule_ids: set[str] = set()
    for i, r in enumerate(_list(doc, 'payee_rules')):
        path = 'payee_rules[%d]' % i
        if r.get('id') in rule_ids:
            errors.append('%s.id: "%s" is used twice' % (path, r.get('id')))
        rule_ids.add(str(r.get('id')))
        for t in r.get('targets') or []:
            if t not in series_ids:
                errors.append('%s.targets: "%s" is not a series id' % (path, t))
        if r.get('kind') == 'card' and r.get('card') not in cards:
            errors.append('%s.card: a card rule must name a credit-card account' % path)
        if r.get('regex'):
            try:
                compile_safe(str(r['regex']))
            except ValueError as e:
                errors.append('%s.regex: %s' % (path, e))
        money_at(path + '.min_amount', r.get('min_amount'))
        money_at(path + '.max_amount', r.get('max_amount'))
    alerts = _obj(doc, 'alerts')
    money_at('alerts.cushion', alerts.get('cushion'))
    pct_at('alerts.bill_variance_pct', alerts.get('bill_variance_pct'))
    for i, a in enumerate(_list(alerts, 'income')):
        if a.get('series') not in series_ids:
            errors.append('alerts.income[%d].series: "%s" is not a series id' % (i, a.get('series')))
        pct_at('alerts.income[%d].min_avg_pct' % i, a.get('min_avg_pct'))
        money_at('alerts.income[%d].short_by' % i, a.get('short_by'))
    for i, c in enumerate(_list(alerts, 'caps')):
        for rid in c.get('rules') or []:
            if rid not in rule_ids:
                errors.append('alerts.caps[%d].rules: "%s" is not a payee rule id' % (i, rid))
        money_at('alerts.caps[%d].amount' % i, c.get('amount'))
    rec = _obj(_obj(doc, 'settings'), 'reconcile')
    pct_at('settings.reconcile.tolerance_pct', rec.get('tolerance_pct'))
    money_at('settings.reconcile.tolerance_abs', rec.get('tolerance_abs'))
    money_at('household.start_balance', _obj(doc, 'household').get('start_balance'))
    for i, c in enumerate(_list(doc, 'card_categories')):
        for sid in c.get('weekly_series') or []:
            if sid not in series_ids:
                errors.append('card_categories[%d].weekly_series: "%s" is not a series id' % (i, sid))
    for i, g in enumerate(_list(doc, 'flow_groups')):
        for rid in g.get('rules') or []:
            if rid not in rule_ids:
                errors.append('flow_groups[%d].rules: "%s" is not a payee rule id' % (i, rid))
        for sid in g.get('series') or []:
            if sid not in series_ids:
                errors.append('flow_groups[%d].series: "%s" is not a series id' % (i, sid))
    return errors


# ---------------------------------------------------------------- document -> core
def _d(v: object) -> date | None:
    return date.fromisoformat(v) if isinstance(v, str) and v else None


def _first_on_or_after(dates: Iterable[date], as_of: date) -> date:
    return min(d for d in dates if d >= as_of)


def _resolve_schedule(d: Mapping[str, Any], as_of: date, name: str, warnings: list[str]) -> Schedule:
    cadence = Cadence(d['cadence'])
    start = _d(d.get('start'))
    weekday = d.get('weekday')
    day, month = d.get('day'), d.get('month')
    days = tuple(sorted(d['days'])) if d.get('days') else None
    if cadence in (Cadence.WEEKLY, Cadence.BIWEEKLY) and weekday:
        base = start or as_of
        start = base + timedelta(days=(WEEKDAYS.index(weekday) - base.weekday()) % 7)
        if cadence is Cadence.BIWEEKLY and d.get('start') is None:
            warnings.append('Set a start date for %s so the paydays fall in the right weeks.' % name)
    elif start is None:
        y, m = as_of.year, as_of.month
        nxt = (y + 1, 1) if m == 12 else (y, m + 1)
        if cadence is Cadence.MONTHLY and day:
            start = _first_on_or_after((clamp_day(y, m, day), clamp_day(*nxt, day)), as_of)
        elif cadence is Cadence.SEMIMONTHLY and days:
            start = _first_on_or_after([clamp_day(yy, mm, x) for yy, mm in ((y, m), nxt) for x in days], as_of)
        elif cadence is Cadence.YEARLY and month and day:
            start = _first_on_or_after((clamp_day(y, month, day), clamp_day(y + 1, month, day)), as_of)
        else:
            start = as_of
    return Schedule(cadence=cadence, start=start, end=_d(d.get('end')), interval=int(d.get('interval') or 1),
                    day=day, days=cast('tuple[int, int] | None', days), month=month,
                    weekend=Weekend(d.get('weekend') or 'none'))


def _months(spec: str) -> frozenset[int]:
    from hbudget.core.ledger import parse_months
    return parse_months(spec)


def _series(s: Mapping[str, Any], kind: Kind, as_of: date, warnings: list[str],
            watermarks: Mapping[str, date | None]) -> Series:
    month_amounts: dict[int, Cents] = {}
    for spec, amount in (s.get('seasonal') or {}).items():
        for m in _months(str(spec)):
            month_amounts[m] = _cents(amount)
    return Series(
        id=s['id'], name=s['name'], kind=kind, amount=_cents(s['amount']),
        schedule=_resolve_schedule(s['schedule'], as_of, s['name'], warnings),
        funded_by=s.get('funded_by'), status=Status(s.get('status') or 'none'), card=s.get('card'), debt=s.get('debt'),
        group=s.get('group'), month_amounts=tuple(sorted(month_amounts.items())),
        cycle_amounts=tuple(_cents(x) for x in s.get('cycle') or ()), active=bool(s.get('active', True)),
        notes=s.get('notes') or '', generated_through=watermarks.get(s['id']))


def _opt_cents(v: object) -> Cents | None:
    return None if v is None else _cents(v)


def _account(a: Mapping[str, Any]) -> Account:
    return Account(
        id=a['id'], name=a['name'], kind=AccountKind(a['kind']), balance=_cents(a.get('balance', 0)),
        apr_bp=_bp(a.get('apr', 0)), payment=_cents(a.get('payment', 0)), minimum=_cents(a.get('minimum', 0)),
        limit=_opt_cents(a.get('limit')), as_of=_d(a.get('as_of')), promo_ends=_d(a.get('promo_ends')),
        apr_after_bp=None if a.get('apr_after') is None else _bp(a['apr_after']),
        deferred_interest=bool(a.get('deferred_interest', False)), is_mortgage=bool(a.get('is_mortgage', False)),
        include_in_payoff=bool(a.get('include_in_payoff', True)), notes=a.get('notes') or '',
        history=tuple(sorted((date.fromisoformat(str(d)), _cents(v)) for d, v in a.get('history') or ())))


def _rule(r: Mapping[str, Any]) -> PayeeRule:
    return PayeeRule(
        id=r['id'], name=r['name'], kind=RuleKind(r['kind']), contains=tuple(r.get('contains') or ()),
        regex=r.get('regex'), direction=Direction(r.get('direction') or 'any'),
        min_amount=_opt_cents(r.get('min_amount')), max_amount=_opt_cents(r.get('max_amount')),
        targets=tuple(r.get('targets') or ()), card=r.get('card'), note=r.get('note') or '')


def _settings(doc: Mapping[str, object]) -> Settings:
    st = _obj(doc, 'settings')
    rec = _obj(st, 'reconcile')
    defaults = ReconcileSettings()
    reconcile = ReconcileSettings(
        days_before=int(rec.get('days_before', defaults.days_before)),
        days_after=int(rec.get('days_after', defaults.days_after)),
        tolerance_bp=_bp(rec['tolerance_pct']) if 'tolerance_pct' in rec else defaults.tolerance_bp,
        tolerance_abs=_cents(rec['tolerance_abs']) if 'tolerance_abs' in rec else defaults.tolerance_abs)
    al = _obj(doc, 'alerts')
    ad = AlertSettings()
    alerts = AlertSettings(
        cushion=_cents(al['cushion']) if 'cushion' in al else ad.cushion,
        bill_variance_bp=_bp(al['bill_variance_pct']) if 'bill_variance_pct' in al else ad.bill_variance_bp,
        unscheduled_days=int(al.get('unscheduled_days', ad.unscheduled_days)),
        expected_grace_days=int(al.get('expected_grace_days', ad.expected_grace_days)),
        unmatched_recurring=bool(al.get('unmatched_recurring', ad.unmatched_recurring)),
        income=tuple(IncomeAlert(series_id=a['series'], min_avg_bp=_bp(a.get('min_avg_pct', 85)),
                                 window=int(a.get('window', 4)), short_by=_opt_cents(a.get('short_by')))
                     for a in _list(al, 'income')),
        caps=tuple(CapRule(id=c['id'], name=c['name'], rule_ids=tuple(c['rules']), period=c['period'],
                           amount=_cents(c['amount'])) for c in _list(al, 'caps')))
    colours = dict(Settings().status_colours)
    for k, v in _obj(st, 'status_colours').items():
        colours[Status(k)] = str(v).lower()
    currency = _obj(doc, 'household').get('currency') or 'USD'
    return Settings(currency=str(currency), week_starts=6 if st.get('week_starts') == 'sunday' else 0,
                    reconcile=reconcile, alerts=alerts,
                    status_colours=((Status.AUTOPAY, colours[Status.AUTOPAY]), (Status.MANUAL, colours[Status.MANUAL])),
                    goals=tuple(_goal(g) for g in _list(doc, 'goals')),
                    celebrate=bool(st.get('celebrate', True)),
                    milestones=tuple(_milestone(x) for x in _list(doc, 'milestones')),
                    not_recurring=tuple((str(x['source']), str(x['payee'])) for x in _list(doc, 'not_recurring')),
                    alert_snoozes=tuple((str(x['alert']), str(x['payee']), date.fromisoformat(str(x['since'])))
                                        for x in _list(doc, 'alert_snoozes')),
                    card_transfers_on_payday=bool(st.get('card_transfers_on_payday', False)))


def _milestone(x: Mapping[str, Any]) -> Milestone:
    kind = x.get('kind')
    if kind not in ('debt_paid', 'goal_reached', 'above_cushion', 'under_budget'):
        raise ImportProblem('A milestone has an unknown kind: %r.' % kind)
    return Milestone(str(x['key']), kind, str(x['text']), date.fromisoformat(str(x['on'])))


def _goal(g: Mapping[str, Any]) -> Goal:
    kind = g.get('kind')
    if kind not in ('emergency', 'savings', 'debt_free'):
        raise ImportProblem('A goal has an unknown kind: %r.' % kind)
    return Goal(id=str(g['id']), name=str(g['name']), kind=kind, target=_opt_cents(g.get('target')),
                months=int(g.get('months', 3)), saved=_cents(g.get('saved', 0)), by=_d(g.get('by')),
                start_debt=_opt_cents(g.get('start_debt')))


def _goal_doc(g: Goal) -> dict[str, object]:
    out: dict[str, object] = {'id': g.id, 'name': g.name, 'kind': g.kind}
    if g.target is not None:
        out['target'] = _m(g.target)
    if g.kind == 'emergency':
        out['months'] = g.months
    if g.saved:
        out['saved'] = _m(g.saved)
    if g.by:
        out['by'] = g.by.isoformat()
    if g.start_debt is not None:
        out['start_debt'] = _m(g.start_debt)
    return out


def _ledger_row(r: Mapping[str, Any]) -> LedgerRow:
    return LedgerRow(
        id=str(r['id']), series_id=r.get('series'), name=r['name'], kind=Kind(r['kind']), amount=_cents(r['amount']),
        due_date=date.fromisoformat(r['due']), order_date=date.fromisoformat(r['order_date']), seq=int(r['seq']),
        nominal_date=_d(r.get('nominal')), state=RowState(r['state']),
        status_override=Status(r['status']) if r.get('status') else None,
        amount_was=_opt_cents(r.get('amount_was')), overridden=bool(r.get('overridden', False)), card=r.get('card'),
        paid=_cents(r.get('paid', 0)), paid_txn_ids=tuple(r.get('paid_txns') or ()),
        paid_at_change=_cents(r.get('paid_at_change', 0)), cleared_txn_id=r.get('cleared_txn'),
        cleared_on=_d(r.get('cleared_on')), note=r.get('note') or '')


def _expected(e: Mapping[str, Any]) -> ExpectedItem:
    return ExpectedItem(id=str(e['id']), label=e['label'], amount=_cents(e['amount']), direction=e['direction'],
                        date=date.fromisoformat(e['date']), status=e.get('status') or 'open')


def to_budget(doc: dict[str, object], *, today: date, warnings: list[str] | None = None) -> Budget:
    """Resolve a validated document into a Budget. Without a backup section the rows are generated."""
    warn = warnings if warnings is not None else []
    hh = _obj(doc, 'household')
    if (hh.get('currency') or 'USD') != 'USD':
        warn.append('The file says currency %s. It is kept, but amounts are shown in US dollars: nothing is '
                    'converted.' % hh.get('currency'))
    as_of = _d(hh.get('as_of')) or today
    raw_backup = doc.get('backup')
    backup: dict[str, object] = raw_backup if isinstance(raw_backup, dict) else {}
    watermarks = {str(s['id']): _d(s.get('generated_through')) for s in _list(backup, 'series_state')}
    series = tuple(_series(s, kind, as_of, warn, watermarks) for section, kind in SECTIONS for s in _list(doc, section))
    hz = _obj(hh, 'horizon')
    budget = Budget(
        start_balance=_cents(hh.get('start_balance', 0)), as_of=as_of,
        horizon=Horizon(mode=hz.get('mode') or 'rolling', months=int(hz.get('months') or 15), end=_d(hz.get('end'))),
        series=series,
        rows=tuple(_ledger_row(r) for r in _list(backup, 'ledger')),
        rules=tuple(_rule(r) for r in _list(doc, 'payee_rules')),
        accounts=tuple(_account(a) for a in _list(doc, 'accounts')),
        expected=tuple(_expected(e) for e in _list(backup, 'expected')),
        settings=_settings(doc),
        card_categories=tuple(CardCategory(
            name=c['name'], keywords=tuple(c.get('keywords') or ()),
            bank_categories=tuple(c.get('bank_categories') or ()),
            weekly_series=tuple(c.get('weekly_series') or ()), has_own_rows=bool(c.get('has_own_rows', False)),
            merchants=tuple(c.get('merchants') or ()))
            for c in _list(doc, 'card_categories')),
        flow_groups=tuple(FlowGroup(
            name=g['name'], colour=g.get('colour') or '--f-other', rule_ids=tuple(g.get('rules') or ()),
            series_ids=tuple(g.get('series') or ()), row_names=tuple(g.get('row_names') or ()))
            for g in _list(doc, 'flow_groups')),
        last_reconciled_through=_d(hh.get('last_reconciled_through')))
    if not backup:
        from hbudget.core.ledger import generate, horizon_end
        budget = generate(budget, horizon_end(budget))
    return budget


def _mapping_from(kind: str, data: Mapping[str, Any]) -> BankMapping | WorkbookMapping:
    cls: type[BankMapping] | type[WorkbookMapping] = WorkbookMapping if kind == 'workbook' else BankMapping
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ImportProblem('Unknown mapping field(s): %s' % ', '.join(sorted(unknown)))

    def tup(v: object) -> object:
        return tuple(tup(x) for x in v) if isinstance(v, list) else v
    values: dict[str, Any] = {k: tup(v) for k, v in data.items()}
    if 'status_colours' in values:
        values['status_colours'] = tuple((str(c).lower(), Status(s)) for c, s in values['status_colours'])
    try:
        return cls(**values)
    except TypeError as e:
        raise ImportProblem('A mapping profile is incomplete (%s).' % e) from None


def _extras(backup: Mapping[str, Any]) -> BackupExtras:
    def txn(t: Mapping[str, Any]) -> BankTxn:
        return BankTxn(id=str(t['id']), posted=date.fromisoformat(t['posted']), amount=_cents(t['amount']),
                       description=t['description'], balance=_opt_cents(t.get('balance')),
                       category=t.get('category') or '', file_index=int(t.get('file_index', 0)),
                       effective=_d(t.get('effective')))

    def card(t: Mapping[str, Any]) -> CardTxn:
        return CardTxn(id=str(t['id']), purchased=date.fromisoformat(t['purchased']),
                       posted=date.fromisoformat(t['posted']), amount=_cents(t['amount']),
                       description=t['description'], category=t['category'], by_hand=bool(t.get('by_hand', False)))
    bank = {t.id: t for t in (txn(x) for x in _list(backup, 'bank_transactions'))}

    def _link(x: Mapping[str, Any], known: Mapping[str, BankTxn]) -> tuple[str, str, int, Cents | None, str]:
        # older backups have no per-file values: the transaction's own values were the file's then
        t = known.get(str(x['txn']))
        bal = _opt_cents(x['balance']) if 'balance' in x else (t.balance if t else None)
        cat = str(x['category']) if 'category' in x else (t.category if t else '')
        return str(x['upload']), str(x['txn']), int(x['file_index']), bal, cat
    return BackupExtras(
        bank_txns=tuple((txn(t), t.get('upload')) for t in _list(backup, 'bank_transactions')),
        upload_links=tuple(_link(x, bank) for x in _list(backup, 'upload_links')),
        card_txns=tuple((card(t), t.get('upload')) for t in _list(backup, 'card_transactions')),
        uploads=tuple(dict(u) for u in _list(backup, 'uploads')),
        reconciliations=tuple(dict(r) for r in _list(backup, 'reconciliations')),
        mapping_profiles=tuple(MappingProfile(id=str(p['id']), name=p['name'], kind=p['kind'],
                                              mapping=_mapping_from(p['kind'], p['mapping']),
                                              header=tuple(p.get('header') or ()))
                               for p in _list(backup, 'mapping_profiles')),
        notes=backup.get('notes') or '',
        audit=tuple(dict(a) for a in _list(backup, 'audit')),
        members=tuple((str(m['username']), str(m['role'])) for m in _list(backup, 'members')))


def load(data: bytes, filename: str, *, today: date) -> BillSystem:
    """Parse, upgrade, validate, check and resolve. Every problem is an ImportProblem listing the errors."""
    doc = upgrade(parse(data, filename))
    errors = validate(doc)
    if not errors:
        errors = check(doc)
    if errors:
        shown = errors[:20] + (['... and %d more' % (len(errors) - 20)] if len(errors) > 20 else [])
        raise ImportProblem('The bill-system file has problems:\n' + '\n'.join(shown))
    warnings: list[str] = []
    budget = to_budget(doc, today=today, warnings=warnings)
    backup = doc.get('backup')
    extras = _extras(backup) if isinstance(backup, dict) else None
    return BillSystem(doc=doc, budget=budget, extras=extras, warnings=tuple(warnings))


# ---------------------------------------------------------------- core -> document
def _m(cents: Cents) -> int | float:
    """Cents as a dollar number: whole dollars as int, otherwise exactly two-decimal float (repr round-trips)."""
    return cents // 100 if cents % 100 == 0 else float(Decimal(cents) / 100)


def _pct(bp: int) -> int | float:
    return bp // 100 if bp % 100 == 0 else float(Decimal(bp) / 100)


def _seasonal(month_amounts: Sequence[tuple[int, Cents]]) -> dict[str, int | float]:
    out: dict[str, int | float] = {}
    items = sorted(month_amounts)
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1][0] == items[j][0] + 1 and items[j + 1][1] == items[i][1]:
            j += 1
        key = str(items[i][0]) if i == j else '%d-%d' % (items[i][0], items[j][0])
        out[key] = _m(items[i][1])
        i = j + 1
    return out


def _schedule_doc(s: Schedule) -> dict[str, object]:
    out: dict[str, object] = {'cadence': s.cadence.value, 'start': s.start.isoformat()}
    if s.end:
        out['end'] = s.end.isoformat()
    if s.interval != 1:
        out['interval'] = s.interval
    if s.day is not None:
        out['day'] = s.day
    if s.days is not None:
        out['days'] = list(s.days)
    if s.month is not None:
        out['month'] = s.month
    if s.weekend is not Weekend.NONE:
        out['weekend'] = s.weekend.value
    return out


def _series_doc(s: Series) -> dict[str, object]:
    out: dict[str, object] = {'id': s.id, 'name': s.name, 'amount': _m(s.amount), 'schedule': _schedule_doc(s.schedule)}
    if s.status is not Status.NONE:
        out['status'] = s.status.value
    for key, value in (('funded_by', s.funded_by), ('card', s.card), ('debt', s.debt), ('group', s.group)):
        if value is not None:
            out[key] = value
    if s.month_amounts:
        out['seasonal'] = _seasonal(s.month_amounts)
    if s.cycle_amounts:
        out['cycle'] = [_m(c) for c in s.cycle_amounts]
    if not s.active:
        out['active'] = False
    if s.notes:
        out['notes'] = s.notes
    return out


def _account_doc(a: Account) -> dict[str, object]:
    out: dict[str, object] = {'id': a.id, 'name': a.name, 'kind': a.kind.value}
    if a.balance:
        out['balance'] = _m(a.balance)
    if a.apr_bp:
        out['apr'] = _pct(a.apr_bp)
    if a.payment:
        out['payment'] = _m(a.payment)
    if a.minimum:
        out['minimum'] = _m(a.minimum)
    if a.limit is not None:
        out['limit'] = _m(a.limit)
    for key, d in (('as_of', a.as_of), ('promo_ends', a.promo_ends)):
        if d:
            out[key] = d.isoformat()
    if a.apr_after_bp is not None:
        out['apr_after'] = _pct(a.apr_after_bp)
    if a.deferred_interest:
        out['deferred_interest'] = True
    if a.is_mortgage:
        out['is_mortgage'] = True
    if not a.include_in_payoff:
        out['include_in_payoff'] = False
    if a.notes:
        out['notes'] = a.notes
    if a.history:
        out['history'] = [[d.isoformat(), _m(v)] for d, v in a.history]
    return out


def _rule_doc(r: PayeeRule) -> dict[str, object]:
    out: dict[str, object] = {'id': r.id, 'name': r.name, 'kind': r.kind.value}
    if r.contains:
        out['contains'] = list(r.contains)
    if r.regex:
        out['regex'] = r.regex
    if r.direction is not Direction.ANY:
        out['direction'] = r.direction.value
    if r.min_amount is not None:
        out['min_amount'] = _m(r.min_amount)
    if r.max_amount is not None:
        out['max_amount'] = _m(r.max_amount)
    if r.targets:
        out['targets'] = list(r.targets)
    if r.card:
        out['card'] = r.card
    if r.note:
        out['note'] = r.note
    return out


def _alerts_doc(a: AlertSettings) -> dict[str, object]:
    return {
        'cushion': _m(a.cushion), 'bill_variance_pct': _pct(a.bill_variance_bp),
        'unscheduled_days': a.unscheduled_days, 'expected_grace_days': a.expected_grace_days,
        'unmatched_recurring': a.unmatched_recurring,
        'income': [{'series': i.series_id, 'min_avg_pct': _pct(i.min_avg_bp), 'window': i.window,
                    **({'short_by': _m(i.short_by)} if i.short_by is not None else {})} for i in a.income],
        'caps': [{'id': c.id, 'name': c.name, 'rules': list(c.rule_ids), 'period': c.period, 'amount': _m(c.amount)}
                 for c in a.caps],
    }


def _row_doc(r: LedgerRow) -> dict[str, object]:
    return {
        'id': r.id, 'series': r.series_id, 'name': r.name, 'kind': r.kind.value, 'amount': _m(r.amount),
        'due': r.due_date.isoformat(), 'nominal': r.nominal_date.isoformat() if r.nominal_date else None,
        'order_date': r.order_date.isoformat(), 'seq': r.seq, 'state': r.state.value,
        'status': r.status_override.value if r.status_override else None,
        'amount_was': None if r.amount_was is None else _m(r.amount_was), 'overridden': r.overridden,
        'card': r.card, 'paid': _m(r.paid), 'paid_txns': list(r.paid_txn_ids), 'paid_at_change': _m(r.paid_at_change),
        'cleared_txn': r.cleared_txn_id, 'cleared_on': r.cleared_on.isoformat() if r.cleared_on else None,
        'note': r.note,
    }


def _plain(v: object) -> object:
    if isinstance(v, tuple | list):
        return [_plain(x) for x in v]
    if isinstance(v, Status):
        return v.value
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, date):
        return v.isoformat()
    return v


def _mapping_doc(m: BankMapping | WorkbookMapping) -> dict[str, object]:
    assert is_dataclass(m)
    return {f.name: _plain(getattr(m, f.name)) for f in fields(m)}


def _backup_doc(budget: Budget, extras: BackupExtras, exported_at: datetime, app_version: str) -> dict[str, object]:
    def txn(t: BankTxn, upload: str | None) -> dict[str, object]:
        return {'id': t.id, 'upload': upload, 'posted': t.posted.isoformat(),
                'effective': t.effective.isoformat() if t.effective else None, 'amount': _m(t.amount),
                'balance': None if t.balance is None else _m(t.balance), 'description': t.description,
                'category': t.category, 'file_index': t.file_index}

    def card(t: CardTxn, upload: str | None) -> dict[str, object]:
        return {'id': t.id, 'upload': upload, 'purchased': t.purchased.isoformat(), 'posted': t.posted.isoformat(),
                'amount': _m(t.amount), 'description': t.description, 'category': t.category,
                **({'by_hand': True} if t.by_hand else {})}
    return {
        'exported_at': exported_at.isoformat(), 'app_version': app_version,
        'ledger': [_row_doc(r) for r in budget.rows],
        'series_state': [{'id': s.id, 'generated_through': s.generated_through.isoformat() if s.generated_through
                          else None} for s in budget.series],
        'expected': [{'id': e.id, 'label': e.label, 'amount': _m(e.amount), 'direction': e.direction,
                      'date': e.date.isoformat(), 'status': e.status} for e in budget.expected],
        'notes': extras.notes,
        'bank_transactions': [txn(t, u) for t, u in extras.bank_txns],
        'upload_links': [{'upload': u, 'txn': t, 'file_index': i, 'balance': None if b is None else _m(b),
                          'category': c} for u, t, i, b, c in extras.upload_links],
        'card_transactions': [card(t, u) for t, u in extras.card_txns],
        'uploads': [_plain(u) for u in extras.uploads],
        'mapping_profiles': [{'id': p.id, 'name': p.name, 'kind': p.kind, 'mapping': _mapping_doc(p.mapping),
                              'header': list(p.header)} for p in extras.mapping_profiles if not p.builtin],
        'reconciliations': [_plain(r) for r in extras.reconciliations],
        'audit': [_plain(a) for a in extras.audit],
        'members': [{'username': u, 'role': r} for u, r in extras.members],
    }


def from_budget(budget: Budget, extras: BackupExtras | None, *, exported_at: datetime, app_version: str,
                household_name: str = 'Household') -> dict[str, object]:
    """A bill-system document (with the backup section when extras is given)."""
    st = budget.settings
    hh: dict[str, object] = {'name': household_name, 'currency': st.currency, 'start_balance': _m(budget.start_balance),
                             'as_of': budget.as_of.isoformat()}
    if budget.last_reconciled_through:
        hh['last_reconciled_through'] = budget.last_reconciled_through.isoformat()
    hz: dict[str, object] = {'mode': budget.horizon.mode, 'months': budget.horizon.months}
    if budget.horizon.end:
        hz['end'] = budget.horizon.end.isoformat()
    hh['horizon'] = hz
    colours = dict(st.status_colours)
    doc: dict[str, object] = {
        'format': FORMAT, 'version': VERSION, 'household': hh,
        'settings': {
            'week_starts': 'sunday' if st.week_starts == 6 else 'monday',
            'reconcile': {'days_before': st.reconcile.days_before, 'days_after': st.reconcile.days_after,
                          'tolerance_pct': _pct(st.reconcile.tolerance_bp),
                          'tolerance_abs': _m(st.reconcile.tolerance_abs)},
            'status_colours': {k.value: colours[k] for k in (Status.AUTOPAY, Status.MANUAL) if k in colours},
            **({} if st.celebrate else {'celebrate': False}),
            **({'card_transfers_on_payday': True} if st.card_transfers_on_payday else {}),
        },
        'accounts': [_account_doc(a) for a in budget.accounts],
    }
    for section, kind in SECTIONS:
        doc[section] = [_series_doc(s) for s in budget.series if s.kind is kind]
    doc['payee_rules'] = [_rule_doc(r) for r in budget.rules]
    doc['alerts'] = _alerts_doc(st.alerts)
    if st.goals:
        doc['goals'] = [_goal_doc(g) for g in st.goals]
    if st.milestones:
        doc['milestones'] = [{'key': x.key, 'kind': x.kind, 'text': x.text, 'on': x.on.isoformat()}
                             for x in st.milestones]
    if st.not_recurring:
        doc['not_recurring'] = [{'source': s, 'payee': p} for s, p in st.not_recurring]
    if st.alert_snoozes:
        doc['alert_snoozes'] = [{'alert': c, 'payee': k, 'since': d.isoformat()} for c, k, d in st.alert_snoozes]
    doc['card_categories'] = [
        {'name': c.name, 'keywords': list(c.keywords),
         **({'bank_categories': list(c.bank_categories)} if c.bank_categories else {}),
         **({'weekly_series': list(c.weekly_series)} if c.weekly_series else {}),
         **({'has_own_rows': True} if c.has_own_rows else {}),
         **({'merchants': list(c.merchants)} if c.merchants else {})} for c in budget.card_categories]
    doc['flow_groups'] = [
        {'name': g.name, 'colour': g.colour, **({'rules': list(g.rule_ids)} if g.rule_ids else {}),
         **({'series': list(g.series_ids)} if g.series_ids else {}),
         **({'row_names': list(g.row_names)} if g.row_names else {})} for g in budget.flow_groups]
    if extras is not None:
        doc['backup'] = _backup_doc(budget, extras, exported_at, app_version)
    return doc


def dump_yaml(doc: dict[str, object]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False, width=120)


def dump_json(doc: dict[str, object]) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + '\n'


# ---------------------------------------------------------------- simple CSV of bills
CSV_COLUMNS = ('name', 'kind', 'amount', 'cadence', 'start', 'day', 'days', 'month', 'weekend', 'status',
               'funded_by', 'card', 'group')


def bills_csv(data: bytes, *, today: date | None = None) -> list[Series]:
    """One series per line: name,kind,amount,cadence,start,day,days,month,weekend,status,funded_by,card,group.

    `days` holds the two semimonthly days separated by ";" or a space. `start` may be empty when `today` is given and
    the pattern is fully described by day/days/month.
    """
    from .csvfile import read_rows
    rows = read_rows(data)
    if not rows:
        raise ImportProblem('The file is empty.')
    header = [h.lower() for h in rows[0]]
    missing = [c for c in ('name', 'kind', 'amount', 'cadence') if c not in header]
    if missing:
        raise ImportProblem('The bills CSV is missing column(s): %s.' % ', '.join(missing))
    out: list[Series] = []
    used: set[str] = set()
    warnings: list[str] = []

    def get(row: list[str], col: str) -> str:
        i = header.index(col) if col in header else -1
        return row[i].strip() if 0 <= i < len(row) else ''
    for line, row in enumerate(rows[1:], 2):
        name = get(row, 'name')
        if not name:
            continue
        try:
            kind = {'income': Kind.INCOME, 'bill': Kind.BILL, 'spending': Kind.SPENDING}[get(row, 'kind').lower()]
            sched: dict[str, Any] = {'cadence': get(row, 'cadence').lower()}
            Cadence(sched['cadence'])
            for col in ('day', 'month'):
                if get(row, col):
                    sched[col] = int(get(row, col))
            if get(row, 'days'):
                sched['days'] = [int(x) for x in get(row, 'days').replace(';', ' ').replace(',', ' ').split()]
                if len(sched['days']) != 2 or len(set(sched['days'])) != 2:
                    raise ValueError('days needs two different days')
            if get(row, 'weekend'):
                sched['weekend'] = Weekend(get(row, 'weekend').lower()).value
            if get(row, 'start'):
                sched['start'] = date.fromisoformat(get(row, 'start')).isoformat()
            elif today is None:
                raise ValueError('a start date is needed')
            amount = money.parse(get(row, 'amount'))
            if amount < 0:
                raise ValueError('amounts are positive; the kind says money in or out')
            status = Status(get(row, 'status').lower() or 'none')
        except (KeyError, ValueError) as e:
            raise ImportProblem('Line %d (%s): %s' % (line, name, e or 'bad value'), line=line) from None
        sid = slug(name) or 'item'
        base, n = sid, 1
        while sid in used:
            n += 1
            sid = '%s-%d' % (base, n)
        used.add(sid)
        schedule = _resolve_schedule(sched, today or date.fromisoformat(sched['start']), name, warnings)
        out.append(Series(id=sid, name=name, kind=kind, amount=amount, schedule=schedule,
                          funded_by=get(row, 'funded_by') or None, status=status, card=get(row, 'card') or None,
                          group=get(row, 'group') or None))
    if not out:
        raise ImportProblem('The bills CSV has no rows.')
    return out

