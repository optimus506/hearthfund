from __future__ import annotations

import io
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.styles import PatternFill

from hbudget.io import ImportProblem, xlsx


def _book(fill: PatternFill | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = 'Data'
    ws['A1'] = 'Name'
    ws['B1'] = 12.5
    ws['C1'] = datetime(2029, 1, 2)
    ws['D1'] = '=B1*2'                 # openpyxl saves formulas without cached values
    ws['E1'] = True
    ws['F1'] = datetime(2029, 1, 2, 9, 30)
    ws['G1'] = 3.0
    if fill:
        ws['A1'].fill = fill
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_fixture_reads_values_and_fills(fixtures: Path) -> None:
    sheets = xlsx.read_xlsx((fixtures / 'workbook' / 'checkbook.xlsx').read_bytes())
    budget = sheets[0]
    assert budget.name == 'Budget' and budget.rows[0][1].value == 2000
    rent = budget.rows[9]
    assert rent[0].text == 'Rent' and rent[0].background == '#b6d7a8' and rent[2].value == date(2029, 3, 3)


def test_formula_without_cached_value_and_types() -> None:
    row = xlsx.read_xlsx(_book(PatternFill('solid', fgColor='FFEC9BA4')))[0].rows[0]
    assert row[0].background == '#ec9ba4'
    assert row[1].value == 12.5 and row[2].value == date(2029, 1, 2) and row[2].text == '2029-01-02'
    assert row[3].value is None and row[3].text == '=B1*2'
    assert row[4].value == 'TRUE'
    assert row[5].text == '2029-01-02 09:30:00'
    assert row[6].text == '3'


def test_theme_colours_are_ignored() -> None:
    from openpyxl.styles.colors import Color
    row = xlsx.read_xlsx(_book(PatternFill('solid', fgColor=Color(theme=4))))[0].rows[0]
    assert row[0].background is None


def test_damaged_file() -> None:
    with pytest.raises(ImportProblem):
        xlsx.read_xlsx(b'PK\x03\x04garbage')


def test_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsx, 'MAX_ROWS', 0)
    with pytest.raises(ImportProblem, match='more than 0 rows'):
        xlsx.read_xlsx(_book())


def test_cell_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsx, 'MAX_CELLS', 2)
    with pytest.raises(ImportProblem, match='more than 2 cells'):
        xlsx.read_xlsx(_book())


def test_value_and_text_helpers() -> None:
    from datetime import time
    assert xlsx._text(None) == '' and xlsx._text(date(2029, 1, 2)) == '2029-01-02' and xlsx._text(2.5) == '2.5'
    assert xlsx._value(time(9, 30)) == '09:30:00' and xlsx._value(None) is None and xlsx._value(7) == 7


def test_damaged_parts_inside_a_valid_zip() -> None:
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('xl/workbook.xml', 'not xml')
    with pytest.raises(ImportProblem, match=r'not a valid \.xlsx'):
        xlsx.read_xlsx(buf.getvalue())


def test_empty_workbook_has_no_rows_and_trailing_blank_rows_are_trimmed() -> None:
    from hbudget.io.workbook_import import WorkbookMapping, import_rows
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = 'Empty'
    buf = io.BytesIO()
    wb.save(buf)
    sheets = xlsx.read_xlsx(buf.getvalue())
    assert sheets[0].rows == ()
    m = WorkbookMapping(sheet='Empty', header_row=None, first_row=1, name_col='A', amount_col='B', date_col='C')
    with pytest.raises(ImportProblem, match='no rows'):
        import_rows(sheets, m, today=date(2029, 1, 1))
    ws['A1'] = 'x'
    ws['A5'] = None
    ws['B5'].fill = PatternFill('solid', fgColor='FFFFFFFF')     # a styled but empty row at the end
    buf = io.BytesIO()
    wb.save(buf)
    assert len(xlsx.read_xlsx(buf.getvalue())[0].rows) == 1


def _sheets(n: int, value: str = 'ab', cells: int = 3) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    for i in range(n):
        sheet = ws if i == 0 else wb.create_sheet('S%d' % i)
        for c in range(cells):
            sheet.cell(row=1, column=c + 1, value=value)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_totals_are_per_workbook_and_sheets_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(xlsx.read_xlsx(_sheets(2))) == 2
    monkeypatch.setattr(xlsx, 'MAX_CELLS', 5)
    with pytest.raises(ImportProblem, match='workbook has more than 5 cells'):
        xlsx.read_xlsx(_sheets(2))
    monkeypatch.setattr(xlsx, 'MAX_CELLS', 100)
    monkeypatch.setattr(xlsx, 'MAX_TEXT_CHARS', 10)
    with pytest.raises(ImportProblem, match='workbook holds more than 10 characters'):
        xlsx.read_xlsx(_sheets(2))
    with pytest.raises(ImportProblem, match='more than 50 sheets'):
        xlsx.read_xlsx(_sheets(51, cells=1))


def test_long_cell_text_is_refused() -> None:
    with pytest.raises(ImportProblem, match='more than 10000 characters'):
        xlsx.read_xlsx(_sheets(1, value='x' * 10_001, cells=1))
