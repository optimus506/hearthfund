#!/usr/bin/env python3
"""Generate the synthetic workbook fixtures (fake household, round amounts, 2029 dates).

  python3 tests/fixtures/make_fixtures.py

Writes tests/fixtures/workbook/checkbook.ods, .xlsx and .csv. They hold the same checkbook sheet: a starting
balance in B1, the sheet's own MIN and date-of-minimum in C2 / C4, the end balance in column E of the last row,
a carry-over line to skip, bill status colours on the Name cells, a bill listed after a later payday, a
zero-amount row, a payday moved to the Friday before a weekend, and blank trailing rows and columns.
The output is committed; the generator is kept so the fixtures can be rebuilt and reviewed.
"""
from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent / 'workbook'
GREEN, PINK = '#b6d7a8', '#ec9ba4'
START = 2000_00

# (name, amount in cents: + out / - in, due date, name colour) in sheet order
ROWS: list[tuple[str, int, date, str | None]] = [
    ('Carry over', 0, date(2029, 3, 1), None),
    ('Pay A', -2000_00, date(2029, 3, 1), None),
    ('Rent', 1500_00, date(2029, 3, 3), GREEN),
    ('Electric', 100_00, date(2029, 3, 6), PINK),
    ('Grocery', 150_00, date(2029, 3, 7), None),
    ('Pay A', -2000_00, date(2029, 3, 15), None),
    ('Car loan', 300_00, date(2029, 3, 10), None),       # due on the 10th, paid from the 15th payday
    ('Grocery', 150_00, date(2029, 3, 14), None),
    ('Internet', 0, date(2029, 3, 20), GREEN),           # zero-amount row
    ('Grocery', 150_00, date(2029, 3, 21), None),
    ('Grocery', 150_00, date(2029, 3, 28), None),
    ('Pay A', -2000_00, date(2029, 3, 30), None),        # Sunday Apr 1 moved to Friday Mar 30
    ('Rent', 1500_00, date(2029, 4, 3), GREEN),
    ('Grocery', 150_00, date(2029, 4, 4), None),
    ('Electric', 120_00, date(2029, 4, 6), PINK),
    ('Car loan', 300_00, date(2029, 4, 10), None),
    ('Grocery', 150_00, date(2029, 4, 11), None),
]
HEADER_ROW, FIRST_ROW = 6, 8


def balances() -> list[int]:
    out, bal = [], START
    for _, amount, _, _ in ROWS:
        bal -= amount
        out.append(bal)
    return out


def check_cells() -> tuple[int, date, int]:
    bal = balances()
    body = list(zip(bal[1:], [r[2] for r in ROWS[1:]], strict=True))   # the minimum excludes the carry-over row
    low = min(b for b, _ in body)
    low_date = next(d for b, d in body if b == low)
    return low, low_date, bal[-1]


def money(cents: int) -> str:
    return '%s$%s.%02d' % ('-' if cents < 0 else '', format(abs(cents) // 100, ','), abs(cents) % 100)


# ---------------------------------------------------------------- ODS
NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
      'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"')


def _text(t: str, style: str | None = None) -> str:
    st = ' table:style-name="%s"' % style if style else ''
    return '<table:table-cell%s office:value-type="string"><text:p>%s</text:p></table:table-cell>' % (st, escape(t))


def _num(cents: int) -> str:
    return ('<table:table-cell office:value-type="currency" office:currency="USD" office:value="%s">'
            '<text:p>%s</text:p></table:table-cell>' % (repr(cents / 100), money(cents)))


def _date(d: date) -> str:
    return ('<table:table-cell office:value-type="date" office:date-value="%s"><text:p>%s</text:p>'
            '</table:table-cell>' % (d.isoformat(), d.strftime('%B %d, %Y').replace(' 0', ' ')))


EMPTY = '<table:table-cell/>'
TRAIL = '<table:table-cell table:number-columns-repeated="1000"/>'


def _row(cells: list[str]) -> str:
    return '<table:table-row>%s%s</table:table-row>' % (''.join(cells), TRAIL)


def write_ods(path: Path) -> None:
    low, low_date, _ = check_cells()
    styles = {GREEN: 'ceGreen', PINK: 'cePink'}
    rows = [
        _row([_text('Checkbook balance'), _num(START), _text('Minimum')]),
        _row([EMPTY, EMPTY, _num(low)]),
        _row([EMPTY, EMPTY, _text('Minimum date')]),
        _row([EMPTY, EMPTY, _date(low_date)]),
        '<table:table-row table:number-rows-repeated="1"><table:table-cell table:number-columns-repeated="5"/>'
        '</table:table-row>',
        _row([_text('Bill'), _text('Amount'), _text('Due date'), _text('Weekday'), _text('Balance')]),
        _row([EMPTY, EMPTY, EMPTY, EMPTY, _num(START)]),
    ]
    for (name, amount, due, colour), bal in zip(ROWS, balances(), strict=True):
        rows.append(_row([_text(name, styles.get(colour or '')), _num(amount), _date(due),
                          _text(due.strftime('%A')), _num(bal)]))
    rows.append('<table:table-row table:number-rows-repeated="1048000"><table:table-cell '
                'table:number-columns-repeated="1024"/></table:table-row>')
    auto = ''.join('<style:style style:name="%s" style:family="table-cell"><style:table-cell-properties '
                   'fo:background-color="%s"/></style:style>' % (n, c) for c, n in styles.items())
    content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content %s office:version="1.3">'
               '<office:automatic-styles>%s</office:automatic-styles><office:body><office:spreadsheet>'
               '<table:table table:name="Budget"><table:table-column table:number-columns-repeated="1005"/>%s'
               '</table:table><table:table table:name="Notes"><table:table-row>%s</table:table-row></table:table>'
               '</office:spreadsheet></office:body></office:document-content>'
               % (NS, auto, ''.join(rows), _text('Fake household for tests')))
    manifest = ('<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
                'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">'
                '<manifest:file-entry manifest:full-path="/" '
                'manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
                '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
                '</manifest:manifest>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype', (2029, 1, 1, 0, 0, 0)), 'application/vnd.oasis.opendocument.spreadsheet',
                   compress_type=zipfile.ZIP_STORED)
        for name, data in (('content.xml', content), ('META-INF/manifest.xml', manifest)):
            z.writestr(zipfile.ZipInfo(name, (2029, 1, 1, 0, 0, 0)), data, compress_type=zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------- XLSX and CSV
def write_xlsx(path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill

    low, low_date, _ = check_cells()
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = 'Budget'
    ws['A1'], ws['B1'], ws['C1'] = 'Checkbook balance', START / 100, 'Minimum'
    ws['C2'], ws['C3'], ws['C4'] = low / 100, 'Minimum date', low_date
    for col, head in zip('ABCDE', ('Bill', 'Amount', 'Due date', 'Weekday', 'Balance'), strict=True):
        ws['%s%d' % (col, HEADER_ROW)] = head
    ws['E7'] = START / 100
    fills = {c: PatternFill('solid', fgColor='FF' + c[1:].upper()) for c in (GREEN, PINK)}
    for n, ((name, amount, due, colour), bal) in enumerate(zip(ROWS, balances(), strict=True), FIRST_ROW):
        ws.cell(row=n, column=1, value=name)
        if colour:
            ws.cell(row=n, column=1).fill = fills[colour]
        ws.cell(row=n, column=2, value=amount / 100)
        ws.cell(row=n, column=3, value=due)
        ws.cell(row=n, column=4, value=due.strftime('%A'))
        ws.cell(row=n, column=5, value=bal / 100)
    wb.create_sheet('Notes')['A1'] = 'Fake household for tests'
    wb.save(path)


def write_csv(path: Path) -> None:
    lines = ['Bill,Amount,Due date,Balance']
    for (name, amount, due, _), bal in zip(ROWS, balances(), strict=True):
        lines.append('%s,"%s",%s,"%s"' % (name, money(amount), due.strftime('%m/%d/%Y'), money(bal)))
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_ods(OUT / 'checkbook.ods')
    write_xlsx(OUT / 'checkbook.xlsx')
    write_csv(OUT / 'checkbook.csv')
    low, low_date, end = check_cells()
    print('wrote %s: start %s, low %s on %s, end %s' % (OUT, money(START), money(low), low_date, money(end)))


if __name__ == '__main__':
    main()
