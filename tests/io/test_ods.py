from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from odsmaker import cell, make_ods

from hbudget.io import ImportProblem, ods


def _table(rows: str, name: str = 'S', columns: str = '') -> str:
    return '<table:table table:name="%s">%s%s</table:table>' % (name, columns, rows)


def test_fixture_values_text_and_colours(fixtures: Path) -> None:
    sheets = ods.read_ods((fixtures / 'workbook' / 'checkbook.ods').read_bytes())
    assert [s.name for s in sheets] == ['Budget', 'Notes']
    budget = sheets[0]
    assert len(budget.rows) == 24                       # 1,048,000 blank trailing rows were not materialised
    assert budget.rows[0][1].value == 2000.0
    rent = budget.rows[9]
    assert rent[0].text == 'Rent' and rent[0].background == '#b6d7a8'
    assert rent[2].value == date(2029, 3, 3)
    assert budget.rows[10][0].background == '#ec9ba4'
    assert all(len(r) <= 5 for r in budget.rows)         # 1,000 blank trailing cells per row were trimmed


def test_repeated_cells_inside_data_are_expanded() -> None:
    row = '<table:table-row>%s<table:table-cell table:number-columns-repeated="3"/>%s</table:table-row>' % (
        cell('a'), cell('b'))
    twice = cell('x', attrs='table:number-columns-repeated="2"')
    rows = '<table:table-row table:number-rows-repeated="2">%s</table:table-row>' % twice
    sheet = ods.read_ods(make_ods(_table(row + rows)))[0]
    assert [c.text for c in sheet.rows[0]] == ['a', '', '', '', 'b']
    assert [[c.text for c in r] for r in sheet.rows[1:]] == [['x', 'x'], ['x', 'x']]


def test_repeated_columns_cap() -> None:
    row = '<table:table-row>%s</table:table-row>' % cell('x', attrs='table:number-columns-repeated="1025"')
    with pytest.raises(ImportProblem, match='more than 1024 columns'):
        ods.read_ods(make_ods(_table(row)))
    gap = '<table:table-row><table:table-cell table:number-columns-repeated="2000"/>%s</table:table-row>' % cell('x')
    with pytest.raises(ImportProblem, match='empty columns'):
        ods.read_ods(make_ods(_table(gap)))


def test_repeated_rows_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = '<table:table-row table:number-rows-repeated="20001">%s</table:table-row>' % cell('x')
    with pytest.raises(ImportProblem, match='repeats one row'):
        ods.read_ods(make_ods(_table(rows)))
    monkeypatch.setattr(ods, 'MAX_ROWS', 3)
    many = ''.join('<table:table-row>%s</table:table-row>' % cell(str(i)) for i in range(4))
    with pytest.raises(ImportProblem, match='more than 3 rows'):
        ods.read_ods(make_ods(_table(many)))
    gap = ('<table:table-row table:number-rows-repeated="5"><table:table-cell/></table:table-row>'
           '<table:table-row>%s</table:table-row>' % cell('x'))
    with pytest.raises(ImportProblem, match='more than 3 rows'):
        ods.read_ods(make_ods(_table(gap)))


def test_cell_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ods, 'MAX_CELLS', 5)
    row = '<table:table-row>%s</table:table-row>' % ''.join(cell(str(i)) for i in range(3))
    with pytest.raises(ImportProblem, match='more than 5 cells'):
        ods.read_ods(make_ods(_table(row * 2)))


def test_dtd_and_entities_are_refused() -> None:
    bad = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><office:document-content '
           'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0">&a;</office:document-content>')
    data = make_ods('')
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, 'w') as dst:
        for item in src.infolist():
            dst.writestr(item, bad if item.filename == 'content.xml' else src.read(item))
    with pytest.raises(ImportProblem, match='not allowed'):
        ods.read_ods(buf.getvalue())


def test_damaged_xml_and_no_sheets() -> None:
    with pytest.raises(ImportProblem, match='no sheets'):
        ods.read_ods(make_ods(''))
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('content.xml', '<not closed')
    with pytest.raises(ImportProblem, match='damaged'):
        ods.read_ods(buf.getvalue())


def test_text_details_and_value_types() -> None:
    p = ('<table:table-cell office:value-type="string"><text:p>a<text:s text:c="3"/>b<text:tab/>c'
         '<text:span>d</text:span><text:line-break/>e</text:p><office:annotation><text:p>NOTE</text:p>'
         '</office:annotation></table:table-cell>')
    row = '<table:table-row>%s%s%s%s%s%s</table:table-row>' % (
        p, cell('x', value='nope', vtype='float'), cell('d', value='2029-02-03T10:00:00', vtype='date'),
        cell('bad', value='29-02-2029', vtype='date'),
        '<table:table-cell office:value-type="boolean" office:boolean-value="true"><text:p>TRUE</text:p>'
        '</table:table-cell>', cell('12%', value='0.12', vtype='float'))
    c = ods.read_ods(make_ods(_table(row)))[0].rows[0]
    assert c[0].text == 'a   b\tcd\ne'                 # the annotation is not part of the cell text
    assert c[1].value is None and c[2].value == date(2029, 2, 3) and c[3].value is None
    assert c[4].value == 'true' and c[5].value == 0.12


def test_parent_style_and_column_default_style() -> None:
    styles = ('<style:style style:name="base" style:family="table-cell"><style:table-cell-properties '
              'fo:background-color="#B6D7A8"/></style:style><style:style style:name="child" '
              'style:family="table-cell" style:parent-style-name="base"/><style:style style:name="clear" '
              'style:family="table-cell"><style:table-cell-properties fo:background-color="transparent"/>'
              '</style:style>')
    columns = ('<table:table-columns><table:table-column table:default-cell-style-name="child"/></table:table-columns>'
               '<table:table-column table:number-columns-repeated="2" table:default-cell-style-name="clear"/>')
    styled = cell('c', attrs='table:style-name="base"')
    row = '<table:table-row>%s%s%s</table:table-row>' % (cell('a'), cell('b'), styled)
    rows = '<table:table-header-rows>%s</table:table-header-rows>' % row
    c = ods.read_ods(make_ods(_table(rows, columns=columns), styles=styles))[0].rows[0]
    assert [x.background for x in c] == ['#b6d7a8', None, '#b6d7a8']


def test_space_runs_cell_text_and_sheet_text_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    spaces = '<table:table-row><table:table-cell office:value-type="string"><text:p>a<text:s text:c="999999999"/>' \
             'b</text:p></table:table-cell></table:table-row>'
    c = ods.read_ods(make_ods(_table(spaces)))[0].rows[0][0]
    assert len(c.text) == 1002                          # one element writes at most 1,000 spaces
    many = '<text:s text:c="1000"/>' * 11
    big = '<table:table-row><table:table-cell><text:p>%s</text:p></table:table-cell></table:table-row>' % many
    with pytest.raises(ImportProblem, match='more than 10000 characters'):
        ods.read_ods(make_ods(_table(big)))
    attr = '<table:table-row><table:table-cell office:value-type="string" office:string-value="%s"/>' \
           '</table:table-row>' % ('x' * 10_001)
    with pytest.raises(ImportProblem, match='more than 10000 characters'):
        ods.read_ods(make_ods(_table(attr)))
    monkeypatch.setattr(ods, 'MAX_TEXT_CHARS', 10)
    rows = '<table:table-row table:number-rows-repeated="3">%s</table:table-row>' % cell('abcd')
    with pytest.raises(ImportProblem, match='more than 10 characters'):
        ods.read_ods(make_ods(_table(rows)))


def test_totals_are_per_workbook_not_per_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    row = '<table:table-row>%s</table:table-row>' % ''.join(cell('ab') for _ in range(3))
    two = _table(row, 'One') + _table(row, 'Two')
    assert len(ods.read_ods(make_ods(two))) == 2
    monkeypatch.setattr(ods, 'MAX_CELLS', 5)                      # 3 cells per sheet, 6 in the workbook
    with pytest.raises(ImportProblem, match='workbook has more than 5 cells'):
        ods.read_ods(make_ods(two))
    monkeypatch.setattr(ods, 'MAX_CELLS', 100)
    monkeypatch.setattr(ods, 'MAX_TEXT_CHARS', 10)                # 6 characters per sheet, 12 in the workbook
    with pytest.raises(ImportProblem, match='workbook holds more than 10 characters'):
        ods.read_ods(make_ods(two))


def test_sheet_count_is_capped() -> None:
    many = ''.join(_table('<table:table-row>%s</table:table-row>' % cell('x'), 'S%d' % i) for i in range(51))
    with pytest.raises(ImportProblem, match='more than 50 sheets'):
        ods.read_ods(make_ods(many))


def test_huge_repeat_numbers_are_bounded() -> None:
    gap = '<table:table-row table:number-rows-repeated="%s"><table:table-cell/></table:table-row>' % ('9' * 30)
    data = '<table:table-row>%s</table:table-row>' % cell('x')
    assert len(ods.read_ods(make_ods(_table(data + gap)))[0].rows) == 1        # a blank tail is free
    with pytest.raises(ImportProblem, match='more than 20000 rows'):            # a blank gap before data is not
        ods.read_ods(make_ods(_table(gap + data)))
    assert ods._int('9' * 5000) == 1 and ods._int('9' * 30) == 1 << 31 and ods._int('-3') == 1


def test_deep_nesting_is_refused() -> None:
    deep_p = '<text:span>' * 70 + 'x' + '</text:span>' * 70
    row = '<table:table-row><table:table-cell><text:p>%s</text:p></table:table-cell></table:table-row>' % deep_p
    with pytest.raises(ImportProblem, match='text is nested too deeply'):
        ods.read_ods(make_ods(_table(row)))
    groups = '<table:table-row-group>' * 70 + '<table:table-row>%s</table:table-row>' % cell('x') \
        + '</table:table-row-group>' * 70
    with pytest.raises(ImportProblem, match='rows are nested too deeply'):
        ods.read_ods(make_ods(_table(groups)))
    cols = '<table:table-column-group>' * 70 + '<table:table-column/>' + '</table:table-column-group>' * 70
    with pytest.raises(ImportProblem, match='columns are nested too deeply'):
        ods.read_ods(make_ods(_table('<table:table-row>%s</table:table-row>' % cell('x'), columns=cols)))
