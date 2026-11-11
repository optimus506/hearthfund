from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from hbudget.core.forecast import compute
from hbudget.core.ledger import ordered
from hbudget.core.models import Kind, Status
from hbudget.io import ImportProblem
from hbudget.io.tabular import Cell, Sheet, cell_at, read_table
from hbudget.io.workbook_import import (
    StoredCheck,
    WorkbookMapping,
    cell_cents,
    guess_mapping,
    import_rows,
    parse_date,
    preview,
)

TODAY = date(2029, 3, 1)
MAPPING = WorkbookMapping(sheet='Budget', header_row=6, first_row=8, name_col='A', amount_col='B', date_col='C',
                          start_balance_cell='B1', skip_names=('^Carry over$',), expected_low=('C2', 'C4'),
                          expected_end_cell='E')


def _sheet(rows: list[list[object]], name: str = 'S') -> list[Sheet]:
    def mk(v: object) -> Cell:
        if isinstance(v, Cell):
            return v
        return Cell(v, '' if v is None else str(v))   # type: ignore[arg-type]
    return [Sheet(name, tuple(tuple(mk(v) for v in r) for r in rows))]


def _simple(**kw: object) -> WorkbookMapping:
    base = WorkbookMapping(sheet='S', header_row=1, first_row=2, name_col='A', amount_col='B', date_col='C')
    return replace(base, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize('ext', ['ods', 'xlsx'])
def test_fixture_reproduces_stored_forecast(fixtures: Path, ext: str) -> None:
    sheets = read_table((fixtures / 'workbook' / ('checkbook.' + ext)).read_bytes(), 'c.' + ext)
    ib = import_rows(sheets, MAPPING, today=TODAY)
    f = compute(ib.budget, today=TODAY)
    assert ib.stored_check is not None
    assert ib.stored_check.matches(f) == {'start': True, 'low': True, 'low_date': True, 'end': True}
    assert ib.warnings == ()
    names = [r.name for r in ordered(ib.budget.rows)]
    assert names[:6] == ['Pay A', 'Rent', 'Electric', 'Grocery', 'Pay A', 'Car loan']   # sheet order, not due dates
    sheet = sheets[0]
    column = [c.text for c in (cell_at(sheet, n, 0) for n in range(9, 9 + len(names))) if c is not None]
    assert names == column                                   # every row, in the sheet's own order (row 8 skipped)
    car = next(r for r in ib.budget.rows if r.name == 'Car loan')
    assert car.due_date == date(2029, 3, 10) and car.order_date == date(2029, 3, 15)
    rent = next(s for s in ib.budget.series if s.id == 'rent')
    assert rent.status is Status.AUTOPAY
    electric = [r for r in ib.budget.rows if r.name == 'Electric']
    assert {r.status_override for r in electric} == {Status.MANUAL}
    assert ib.budget.horizon.mode == 'fixed' and ib.budget.horizon.end == date(2029, 4, 11)


def test_csv_workbook_without_colours(fixtures: Path) -> None:
    sheets = read_table((fixtures / 'workbook' / 'checkbook.csv').read_bytes(), 'checkbook.csv')
    m = WorkbookMapping(sheet='checkbook.csv', header_row=1, first_row=2, name_col='A', amount_col='B',
                        date_col='C', start_balance_value=200000, skip_names=('^Carry over$',))
    ib = import_rows(sheets, m, today=TODAY)
    f = compute(ib.budget, today=TODAY)
    assert f.end == 328000 and ib.stored_check is None
    assert all(r.status_override is None for r in ib.budget.rows)


def test_warnings_for_bad_rows_and_late_listing() -> None:
    sheets = _sheet([
        ['Name', 'Amount', 'Date'],
        ['Rent', 100, date(2029, 1, 1)],
        ['Formula', Cell(None, '=B2*2'), date(2029, 1, 2)],
        ['Bad amount', 'abc', date(2029, 1, 3)],
        ['Bad date', 5, 'someday'],
        ['Late', 7, date(2028, 12, 1)],
        ['Empty amount', None, date(2029, 1, 4)],
    ])
    ib = import_rows(sheets, _simple(), today=TODAY)
    text = ' '.join(ib.warnings)
    assert 'formula with no saved value' in text and 'amount could not be read' in text
    assert 'date could not be read' in text and 'listed long after its due date' in text
    assert [r.name for r in ib.budget.rows] == ['Rent', 'Late', 'Empty amount']
    assert next(r for r in ib.budget.rows if r.name == 'Empty amount').amount == 0


def test_sign_income_column_spending_and_cards() -> None:
    sheets = _sheet([
        ['Name', 'Amount', 'Date', 'In?'],
        ['Pay', 1000, '2029-01-01', 'yes'],
        ['Grocery', 50, '01/02/2029', ''],
        ['Rent', 400, '3-Jan-2029', ''],
    ])
    ib = import_rows(sheets, _simple(income_col='D', spending_names=('Grocery',),
                                     card_names=(('Grocery', 'card-a'),)), today=TODAY)
    kinds = {r.name: (r.kind, r.amount, r.card) for r in ib.budget.rows}
    assert kinds == {'Pay': (Kind.INCOME, -100000, None), 'Grocery': (Kind.SPENDING, 5000, 'card-a'),
                     'Rent': (Kind.BILL, 40000, None)}
    flipped = import_rows(sheets, _simple(sign='positive_in'), today=TODAY)
    assert {r.name: r.amount for r in flipped.budget.rows}['Rent'] == -40000


def test_blank_names_and_missing_start_cell() -> None:
    sheets = _sheet([['Name', 'Amount', 'Date'], ['A', 1, '2029-01-01'], ['', '', ''], ['B', 2, '2029-01-02']])
    assert [r.name for r in import_rows(sheets, _simple(), today=TODAY).budget.rows] == ['A']
    ib = import_rows(sheets, _simple(stop_at_blank_name=False, start_balance_cell='Z9'), today=TODAY)
    assert [r.name for r in ib.budget.rows] == ['A', 'B']
    assert ib.budget.start_balance == 0 and 'holds no amount' in ib.warnings[0]


def test_empty_sheet_and_missing_sheet() -> None:
    sheets = _sheet([['Name', 'Amount', 'Date']])
    with pytest.raises(ImportProblem, match='no rows'):
        import_rows(sheets, _simple(), today=TODAY)
    with pytest.raises(ImportProblem, match='no sheet named'):
        import_rows(sheets, _simple(sheet='Other'), today=TODAY)


def test_end_cell_by_reference_and_stored_check_matches() -> None:
    sheets = _sheet([['Name', 'Amount', 'Date', 'Bal'], ['A', 10, '2029-01-01', 90], ['B', 20, '2029-01-02', 70]])
    ib = import_rows(sheets, _simple(start_balance_value=10000, expected_end_cell='D3'), today=TODAY)
    assert ib.stored_check == StoredCheck(start=None, low=None, low_date=None, end=7000)
    assert ib.stored_check.matches(compute(ib.budget, today=TODAY)) == {'end': True}


def test_parse_date_and_cell_cents() -> None:
    assert parse_date('2029-02-28') == date(2029, 2, 28)
    assert parse_date('02/28/29') == date(2029, 2, 28)
    assert parse_date('March 5, 2029') == date(2029, 3, 5)
    assert parse_date('5.3.2029') == date(2029, 3, 5)
    assert parse_date('2029/02/28', '%Y/%m/%d') == date(2029, 2, 28)
    assert parse_date('') is None and parse_date(None) is None and parse_date('31/31/2029') is None
    assert parse_date(Cell(date(2029, 1, 1), 'x')) == date(2029, 1, 1)
    assert cell_cents(Cell(0.1 + 0.2, '')) == 30                 # float noise in stored balances
    assert cell_cents(Cell(None, '$ - ')) == 0
    assert cell_cents(Cell(None, '$(1,200.50)')) == -120050
    assert cell_cents(Cell(None, 'n/a')) is None and cell_cents(Cell(None, '')) is None
    assert cell_cents(None) is None and cell_cents(Cell(True, 'TRUE')) is None


def test_guess_mapping_and_preview(fixtures: Path) -> None:
    sheets = read_table((fixtures / 'workbook' / 'checkbook.ods').read_bytes(), 'c.ods')
    g = guess_mapping(sheets)
    assert (g.sheet, g.header_row, g.first_row, g.name_col, g.amount_col, g.date_col, g.start_balance_cell) == \
        ('Budget', 6, 8, 'A', 'B', 'C', 'B1')
    p = preview(sheets, None)
    assert p.sheets == ('Budget', 'Notes') and p.header is not None and p.header[0] == 'Bill'
    assert p.rows[0][0] == 'Carry over' and 'Car loan' in p.names and p.columns[:3] == ('A', 'B', 'C')
    assert preview(sheets, replace(g, stop_at_blank_name=False)).names == p.names


def test_guess_without_header_uses_types() -> None:
    sheets = _sheet([['Rent', 'monthly', 500, date(2029, 1, 1)], ['Food', 'weekly', 50, date(2029, 1, 2)]])
    g = guess_mapping(sheets)
    assert (g.header_row, g.first_row, g.name_col, g.amount_col, g.date_col) == (None, 1, 'A', 'C', 'D')
    with pytest.raises(ImportProblem):
        guess_mapping([])


def test_preview_and_guess_only_see_bounded_data() -> None:
    wide = [['Name', 'Amount', 'Date'] + ['x'] * 200] + [['Bill %d' % i, i, date(2029, 1, 1)] + ['y'] * 200
                                                         for i in range(500)]
    sheets = _sheet(wide)
    p = preview(sheets, None)
    assert len(p.rows) == 20 and len(p.columns) == 52 and p.header is not None and len(p.header) == 52
    assert all(len(r) == 52 for r in p.rows)
    assert guess_mapping(sheets).first_row == 2
