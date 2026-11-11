from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from hbudget.core.models import BankTxn, Cadence, CardTxn, Kind, Status, Weekend
from hbudget.io import ImportProblem
from hbudget.io import billsystem as bs
from hbudget.io.bank import BankMapping, MappingProfile
from hbudget.io.workbook_import import WorkbookMapping

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'examples' / 'bill-system.example.yaml'
TODAY = date(2029, 1, 5)
STAMP = datetime(2029, 1, 5, 12, 0, 0)


def _doc(**extra: object) -> dict[str, object]:
    doc: dict[str, object] = {'format': bs.FORMAT, 'version': 1, 'household': {'name': 'Test'}}
    doc.update(extra)
    return doc


def _yaml(text: str) -> bytes:
    return ('format: household-budget/bill-system\nversion: 1\nhousehold: {name: Test}\n' + text).encode()


# ---------------------------------------------------------------- parsing and validation
def test_dates_stay_strings_and_yaml_equals_json() -> None:
    doc = bs.parse(_yaml('bills:\n  - {id: rent, name: Rent, amount: 900,\n'
                         '     schedule: {cadence: monthly, start: 2029-02-01}}\n'))
    bills = doc['bills']
    assert isinstance(bills, list) and bills[0]['schedule']['start'] == '2029-02-01'
    assert bs.parse(json.dumps(doc).encode(), 'x.json') == doc


def test_aliases_anchors_depth_and_size_are_rejected() -> None:
    with pytest.raises(ImportProblem, match='anchors'):
        bs.parse(_yaml('notes: &a x\nmore: *a\n'))
    with pytest.raises(ImportProblem, match='aliases'):
        bs.parse(b'a: *missing\n')
    deep = 'x: ' + '[' * 40 + ']' * 40 + '\n'
    with pytest.raises(ImportProblem, match='nested'):
        bs.parse(deep.encode())
    with pytest.raises(ImportProblem, match='nested'):
        bs.parse(('{"a": ' + '[' * 40 + ']' * 40 + '}').encode(), 'x.json')
    with pytest.raises(ImportProblem, match='nested'):
        bs.parse(('[' * 100_000 + ']' * 100_000).encode(), 'x.json')
    with pytest.raises(ImportProblem, match='larger than'):
        bs.parse(b'a: ' + b'x' * (bs.MAX_BYTES + 1))


def test_parse_errors() -> None:
    with pytest.raises(ImportProblem, match='empty'):
        bs.parse(b'  \n')
    with pytest.raises(ImportProblem, match='not UTF-8'):
        bs.parse(b'\xff\xfe\x00')
    with pytest.raises(ImportProblem, match='JSON is not valid'):
        bs.parse(b'{"a": }', 'x.json')
    with pytest.raises(ImportProblem, match='YAML is not valid') as e:
        bs.parse(b'a: [1, 2\nb: 3\n')
    assert e.value.line is not None
    with pytest.raises(ImportProblem, match='mapping at the top level'):
        bs.parse(b'- 1\n- 2\n')


def test_validate_reports_paths() -> None:
    doc = _doc(bills=[{'id': 'x', 'name': 'X', 'amount': 10, 'schedule': {'cadence': 'monthly', 'day': 32}}])
    errors = bs.validate(doc)
    assert any(e.startswith('bills[0].schedule.day:') for e in errors)
    assert bs.validate({'version': 1})[0].startswith('(top level):')


def test_upgrade() -> None:
    assert bs.upgrade(_doc()) == _doc()
    with pytest.raises(ImportProblem, match='newer version'):
        bs.upgrade({'version': 2})


def test_semantic_checks() -> None:
    doc = _doc(
        accounts=[{'id': 'card', 'name': 'Card', 'kind': 'credit_card', 'apr': 12.345},
                  {'id': 'card', 'name': 'Again', 'kind': 'loan'}],
        income=[{'id': 'pay', 'name': 'Pay', 'amount': 100, 'schedule': {'cadence': 'monthly', 'day': 1}}],
        bills=[{'id': 'pay', 'name': 'Dup', 'amount': 10.123, 'schedule': {'cadence': 'semimonthly', 'days': [5, 5]},
                'funded_by': 'nothing', 'card': 'nocard', 'seasonal': {'1-3': 1.001}, 'cycle': [1, 'x']}],
        payee_rules=[{'id': 'r', 'name': 'R', 'kind': 'card', 'targets': ['missing'], 'regex': '(a+)+'},
                     {'id': 'r', 'name': 'R2', 'kind': 'bill', 'min_amount': 1.111}],
        alerts={'cushion': 'lots', 'bill_variance_pct': 1.234,
                'income': [{'series': 'ghost', 'min_avg_pct': 1.111, 'short_by': 0.001}],
                'caps': [{'id': 'c', 'name': 'C', 'rules': ['nope'], 'period': 'week', 'amount': 1.001}]},
        settings={'reconcile': {'tolerance_pct': 1.005, 'tolerance_abs': 0.001}},
        card_categories=[{'name': 'G', 'weekly_series': ['ghost']}],
        flow_groups=[{'name': 'F', 'rules': ['nope'], 'series': ['ghost']}])
    doc['household'] = {'name': 'T', 'start_balance': 1.001}
    text = '\n'.join(bs.check(doc))
    for expected in ('bills[0].id: "pay" is used twice', 'accounts: an id is used twice',
                     'accounts[0].apr', 'bills[0].amount', 'bills[0].seasonal.1-3', 'bills[0].cycle[1]',
                     'bills[0].schedule.days', 'bills[0].funded_by', 'bills[0].card', 'payee_rules[0].targets',
                     'payee_rules[0].card', 'payee_rules[0].regex', 'payee_rules[1].id', 'payee_rules[1].min_amount',
                     'alerts.cushion', 'alerts.bill_variance_pct', 'alerts.income[0].series',
                     'alerts.income[0].min_avg_pct', 'alerts.income[0].short_by', 'alerts.caps[0].rules',
                     'alerts.caps[0].amount', 'settings.reconcile.tolerance_pct', 'settings.reconcile.tolerance_abs',
                     'household.start_balance', 'card_categories[0].weekly_series', 'flow_groups[0].rules',
                     'flow_groups[0].series'):
        assert expected in text, expected


def test_load_reports_errors() -> None:
    with pytest.raises(ImportProblem, match='has problems'):
        bad = _yaml('bills:\n  - {id: x, name: X, amount: -5, schedule: {cadence: monthly}}\n')
        bs.load(bad, 'x.yaml', today=TODAY)
    many = 'bills:\n' + ''.join('  - {id: b%d, name: B, amount: 1, schedule: {cadence: monthly}, funded_by: no}\n'
                                % i for i in range(25))
    with pytest.raises(ImportProblem, match=r'and \d+ more'):
        bs.load(_yaml(many), 'x.yaml', today=TODAY)


# ---------------------------------------------------------------- resolution
def test_schedules_resolve_from_as_of() -> None:
    text = """
income:
  - {id: pay, name: Pay, amount: 1000, schedule: {cadence: biweekly, weekday: friday}}
  - {id: pay2, name: Pay 2, amount: 500, schedule: {cadence: semimonthly, days: [15, 1], weekend: before}}
bills:
  - {id: rent, name: Rent, amount: 900, schedule: {cadence: monthly, day: 3}}
  - {id: late, name: Late, amount: 10, schedule: {cadence: monthly, day: 31}}
  - {id: gift, name: Gift, amount: 50, schedule: {cadence: yearly, month: 1, day: 2}}
  - {id: tax, name: Tax, amount: 50, schedule: {cadence: yearly, month: 4, day: 15}}
  - {id: once, name: Once, amount: 5, schedule: {cadence: once, start: 2029-02-02}}
spending:
  - {id: food, name: Food, amount: 100, schedule: {cadence: weekly, weekday: monday, start: 2029-01-09}}
"""
    b = bs.load(_yaml(text), 'x.yaml', today=TODAY)       # 2029-01-05 is a Friday
    s = b.budget.series_by_id()
    assert s['pay'].schedule.start == date(2029, 1, 5) and s['pay'].kind is Kind.INCOME
    assert b.warnings == ('Set a start date for Pay so the paydays fall in the right weeks.',)
    assert s['pay2'].schedule.start == date(2029, 1, 15) and s['pay2'].schedule.days == (1, 15)
    assert s['pay2'].schedule.weekend is Weekend.BEFORE
    assert s['rent'].schedule.start == date(2029, 2, 3)
    assert s['late'].schedule.start == date(2029, 1, 31)
    assert s['gift'].schedule.start == date(2030, 1, 2) and s['tax'].schedule.start == date(2029, 4, 15)
    assert s['once'].schedule.start == date(2029, 2, 2)
    assert s['food'].schedule.start == date(2029, 1, 15) and s['food'].kind is Kind.SPENDING
    assert b.budget.rows and b.extras is None                # rows generated from the series


def test_seasonal_ranges_wrap_and_compress() -> None:
    b = bs.load(_yaml('bills:\n  - {id: lawn, name: Lawn, amount: 50, seasonal: {"5-9": 80, "10-4": 30},'
                      ' schedule: {cadence: monthly, day: 1}}\n'), 'x.yaml', today=TODAY)
    lawn = b.budget.series_by_id()['lawn']
    assert dict(lawn.month_amounts) == {1: 3000, 2: 3000, 3: 3000, 4: 3000, 5: 8000, 6: 8000, 7: 8000, 8: 8000,
                                        9: 8000, 10: 3000, 11: 3000, 12: 3000}
    doc = bs.from_budget(b.budget, None, exported_at=STAMP, app_version='t')
    bills = doc['bills']
    assert isinstance(bills, list) and bills[0]['seasonal'] == {'1-4': 30, '5-9': 80, '10-12': 30}


def test_example_round_trip_without_backup() -> None:
    b = bs.load(EXAMPLE.read_bytes(), EXAMPLE.name, today=TODAY)
    doc = bs.from_budget(b.budget, None, exported_at=STAMP, app_version='t', household_name='Example')
    assert bs.validate(doc) == [] and bs.check(doc) == [] and 'backup' not in doc
    again = bs.load(bs.dump_yaml(doc).encode(), 'again.yaml', today=TODAY).budget
    assert again.series == b.budget.series and again.settings == b.budget.settings
    assert again.accounts == b.budget.accounts and again.rules == b.budget.rules
    assert again.rows == b.budget.rows


def _extras() -> bs.BackupExtras:
    txn = BankTxn(id='t1', posted=date(2029, 1, 3), amount=-15000, description='ELECTRIC CO', balance=100000,
                  category='Utilities', file_index=1, effective=date(2029, 1, 2))
    card = CardTxn(id='c1', purchased=date(2029, 1, 1), posted=date(2029, 1, 2), amount=-2500, description='CAFE',
                   category='Eating out')
    bank_profile = MappingProfile('p1', 'My bank', 'bank', BankMapping('Date', amount_col='Amount',
                                                                      ignore_cols=('Account',)), ('Date', 'Amount'))
    wb_profile = MappingProfile('p2', 'Sheet', 'workbook', WorkbookMapping(
        sheet='S', header_row=None, first_row=2, name_col='A', amount_col='B', date_col='C',
        skip_names=('^x$',), status_colours=(('#00ff00', Status.AUTOPAY),), expected_low=('C2', 'C4'),
        card_names=(('Food', 'card'),)))
    builtin = MappingProfile('b', 'Builtin', 'bank', BankMapping('Date'), builtin=True)
    return bs.BackupExtras(
        bank_txns=((txn, 'u1'),), upload_links=(('u1', 't1', 1, 100000, 'Utilities'),), card_txns=((card, None),),
        uploads=({'id': 'u1', 'kind': 'bank', 'filename': 'export.csv', 'uploaded_at': '2029-01-05T10:00:00',
                  'uploaded_by': 'owner', 'rows': 1, 'from': '2029-01-03', 'to': '2029-01-03',
                  'latest_balance': 1000, 'has_balance': True, 'status': 'imported', 'note': 'ok'},),
        reconciliations=({'id': 'rc1', 'at': '2029-01-05T10:05:00', 'by': 'owner', 'upload': 'u1',
                          'through': '2029-01-03', 'new_balance': 1000, 'cleared': [], 'accepted': ['P1'],
                          'report': '# Report'},),
        mapping_profiles=(bank_profile, wb_profile, builtin), notes='Remember the gutters.',
        audit=({'at': '2029-01-05T10:05:00', 'by': 'owner', 'action': 'reconcile', 'row': None, 'detail': {},
                'imported': False},),
        members=(('owner', 'owner'), ('partner', 'contributor')))


@pytest.mark.parametrize('fmt', ['yaml', 'json'])
def test_full_backup_round_trip(fmt: str) -> None:
    from hbudget.core.ledger import change_amount, mark_status, ordered, remove_row
    b = bs.load(EXAMPLE.read_bytes(), EXAMPLE.name, today=TODAY).budget
    rows = ordered(b.rows)
    b = change_amount(b, rows[3].id, 12345)
    b = remove_row(b, rows[5].id)
    b = mark_status(b, rows[6].id, Status.MANUAL)
    b = replace(b, last_reconciled_through=date(2029, 1, 3))
    from hbudget.core.models import Goal, Milestone
    b = replace(b, settings=replace(b.settings, goals=(
        Goal('g-rainy', 'Rainy day', 'emergency', months=4, saved=123456, by=date(2029, 12, 1)),
        Goal('g-trip', 'Trip', 'savings', target=250000), Goal('g-free', 'Debt-free', 'debt_free', start_debt=900000)),
        celebrate=False,
        milestones=(Milestone('goal:g-trip', 'goal_reached', 'Goal reached: Trip', date(2029, 1, 2)),)))
    debt = next(a for a in b.accounts if a.kind.value != 'checking')
    b = replace(b, accounts=tuple(replace(a, history=((date(2028, 12, 1), 250050), (date(2029, 1, 1), 240000)))
                                  if a is debt else a for a in b.accounts))
    extras = _extras()
    doc = bs.from_budget(b, extras, exported_at=STAMP, app_version='0.1.0', household_name='Example')
    assert bs.validate(doc) == [] and bs.check(doc) == []
    text = bs.dump_json(doc) if fmt == 'json' else bs.dump_yaml(doc)
    back = bs.load(text.encode(), 'backup.' + fmt, today=date(2030, 6, 1))   # a later day: nothing is regenerated
    assert back.budget == b
    assert back.extras == replace(extras, mapping_profiles=extras.mapping_profiles[:2])   # built-ins are code
    again = bs.from_budget(back.budget, back.extras, exported_at=STAMP, app_version='0.1.0', household_name='Example')
    assert again == doc


def test_unknown_mapping_field_is_rejected() -> None:
    doc = bs.from_budget(bs.load(EXAMPLE.read_bytes(), 'e.yaml', today=TODAY).budget, _extras(), exported_at=STAMP,
                         app_version='t')
    backup = doc['backup']
    assert isinstance(backup, dict)
    backup['mapping_profiles'][0]['mapping']['evil'] = 1
    with pytest.raises(ImportProblem, match='Unknown mapping field'):
        bs.load(bs.dump_json(doc).encode(), 'b.json', today=TODAY)
    del backup['mapping_profiles'][0]['mapping']['evil']
    del backup['mapping_profiles'][0]['mapping']['date_col']
    with pytest.raises(ImportProblem, match='incomplete'):
        bs.load(bs.dump_json(doc).encode(), 'b.json', today=TODAY)


def test_dump_yaml_quotes_date_like_strings() -> None:
    text = bs.dump_yaml({'a': '2029-01-01', 'b': 1.5})
    assert bs.parse(text.encode()) == {'a': '2029-01-01', 'b': 1.5}
    assert json.loads(bs.dump_json({'a': 1})) == {'a': 1}


def test_schema_is_loaded_from_the_package() -> None:
    assert bs.schema()['$schema'].startswith('https://json-schema.org/draft/2020-12')


# ---------------------------------------------------------------- bills CSV
def test_bills_csv() -> None:
    data = (b'name,kind,amount,cadence,start,day,days,month,weekend,status,funded_by,card,group\n'
            b'Pay,income,"2,000.00",semimonthly,2029-01-01,,1;15,,before,,,,\n'
            b'Rent,bill,900,monthly,2029-01-03,3,,,,autopay,pay,,Home\n'
            b'Rent,bill,50,yearly,,1,,6,,,,,\n'
            b',,,,,,,,,,,,\n')
    series = bs.bills_csv(data, today=TODAY)
    assert [s.id for s in series] == ['pay', 'rent', 'rent-2']
    pay, rent, yearly = series
    assert pay.amount == 200000 and pay.schedule.days == (1, 15) and pay.schedule.weekend is Weekend.BEFORE
    assert rent.status is Status.AUTOPAY and rent.funded_by == 'pay' and rent.group == 'Home'
    assert yearly.schedule.cadence is Cadence.YEARLY and yearly.schedule.start == date(2029, 6, 1)


def test_bills_csv_errors() -> None:
    head = b'name,kind,amount,cadence,start,days\n'
    with pytest.raises(ImportProblem, match='missing column'):
        bs.bills_csv(b'name,amount\nA,1\n')
    with pytest.raises(ImportProblem, match='empty'):
        bs.bills_csv(b'')
    with pytest.raises(ImportProblem, match='no rows'):
        bs.bills_csv(head)
    for row, why in ((b'A,salary,1,monthly,2029-01-01,', 'Line 2'), (b'A,bill,1,daily,2029-01-01,', 'daily'),
                     (b'A,bill,-1,monthly,2029-01-01,', 'positive'), (b'A,bill,1,monthly,,', 'start date'),
                     (b'A,bill,1,semimonthly,2029-01-01,5;5', 'two different days')):
        with pytest.raises(ImportProblem, match=why):
            bs.bills_csv(head + row + b'\n')


def test_to_budget_directly_collects_warnings() -> None:
    doc = bs.parse(_yaml('household: {name: T, start_balance: "1,000.50", as_of: 2029-01-05}\n'
                         'income:\n  - {id: pay, name: Pay, amount: 100,\n'
                         '     schedule: {cadence: biweekly, weekday: monday}}\n')
                   .replace(b'household: {name: Test}\n', b''))
    warnings: list[str] = []
    b = bs.to_budget(doc, today=date(2031, 1, 1), warnings=warnings)
    assert b.start_balance == 100050 and b.as_of == date(2029, 1, 5)       # as_of from the file wins over today
    assert b.series[0].schedule.start == date(2029, 1, 8)
    assert warnings == ['Set a start date for Pay so the paydays fall in the right weeks.']
    assert b.rows and b.rows[0].due_date == date(2029, 1, 8)
    # another currency is kept (backups round-trip) but the household is told it changes nothing
    eur = {**doc, 'household': {**doc['household'], 'currency': 'EUR'}}  # type: ignore[dict-item]
    said: list[str] = []
    assert bs.to_budget(eur, today=date(2031, 1, 1), warnings=said).settings.currency == 'EUR'
    assert said[0].startswith('The file says currency EUR. It is kept, but amounts are shown in US dollars')
    assert bs.to_budget(doc, today=date(2031, 1, 1)) == b                   # warnings are optional


def test_overlapping_seasonal_ranges_are_rejected() -> None:
    doc = _doc(bills=[{'id': 'x', 'name': 'X', 'amount': 1, 'schedule': {'cadence': 'monthly', 'day': 1},
                       'seasonal': {'4-10': 2, '10-3': 3, '13': 1}}])
    errors = bs.check(doc)
    assert any('seasonal.10-3: overlaps another range (month 10)' in e for e in errors)
    assert any(e.startswith('bills[0].seasonal.13:') for e in errors)
