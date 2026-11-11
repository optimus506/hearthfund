from __future__ import annotations

import csv
import io
from datetime import date, datetime

from openpyxl import load_workbook

from hbudget.core.forecast import compute
from hbudget.core.models import Budget, Horizon, Kind, LedgerRow, Status, WhatIf
from hbudget.io import billsystem as bs
from hbudget.io import export


def _budget() -> Budget:
    d = date(2029, 3, 1)
    rows = (
        LedgerRow(id='r1', series_id=None, name='=HYPERLINK("x")', kind=Kind.BILL, amount=15000, due_date=d,
                  order_date=d, seq=1, status_override=Status.AUTOPAY),
        LedgerRow(id='r2', series_id=None, name='-Refund', kind=Kind.INCOME, amount=-5000, due_date=d,
                  order_date=d, seq=2),
        LedgerRow(id='r3', series_id=None, name='Rent', kind=Kind.BILL, amount=100000, due_date=date(2029, 3, 3),
                  order_date=date(2029, 3, 3), seq=3, status_override=Status.MANUAL),
    )
    return Budget(start_balance=50000, as_of=d, horizon=Horizon('fixed', end=date(2029, 3, 31)), series=(),
                  rows=rows)


def test_csv_prefixes_text_and_keeps_numbers_numeric() -> None:
    b = _budget()
    f = compute(b, today=b.as_of, whatifs=[WhatIf('@party', 1000, 'out', date(2029, 3, 2))])
    rows = list(csv.reader(io.StringIO(export.forecast_csv(b, f).decode())))
    assert rows[0] == list(export.CSV_HEADER)
    assert rows[1][1] == "'=HYPERLINK(\"x\")" and rows[1][5] == '150.00' and rows[1][7] == 'autopay'
    assert rows[2][1] == "'-Refund" and rows[2][5] == '-50.00'   # a negative amount stays a plain number
    assert rows[3][1] == "'@party" and rows[3][8] == 'expected'
    assert rows[4][3] == '2029-03-03' and rows[4][4] == 'Saturday' and rows[4][6] == '-610.00'


def test_safe_text() -> None:
    assert [export.safe_text(x) for x in ('=1', '+1', '-1', '@x', '\tx', '\rx', 'ok', '', '  =1', ' ok')] == \
        ["'=1", "'+1", "'-1", "'@x", "'\tx", "'\rx", 'ok', '', "'  =1", ' ok']


def test_xlsx_types_fills_and_summary() -> None:
    b = _budget()
    f = compute(b, today=b.as_of)
    wb = load_workbook(io.BytesIO(export.forecast_xlsx(b, f)))
    ws = wb['Forecast']
    assert ws['A1'].value == 'Balance' and ws['B1'].value == 500.0
    labels = [ws.cell(row=i, column=1).value for i in range(1, 8)]
    assert 'Lowest balance' in labels and 'Year end 2029' in labels
    top = next(i for i in range(1, 20) if ws.cell(row=i, column=1).value == 'Order')
    first = top + 1
    name = ws.cell(row=first, column=2)
    assert name.value == '=HYPERLINK("x")' and name.data_type == 's' and name.quotePrefix   # text, not a formula
    assert ws.cell(row=first + 2, column=2).quotePrefix is False
    assert ws.cell(row=first, column=2).fill.fgColor.rgb == 'FFB6D7A8'
    amount = ws.cell(row=first + 1, column=6)
    assert amount.value == -50.0 and amount.data_type == 'n'
    assert ws.cell(row=first, column=4).value == datetime(2029, 3, 1)
    assert ws.cell(row=first + 2, column=2).fill.fgColor.rgb == 'FFEC9BA4'
    assert wb['Series']['A1'].value == 'Id'


def test_xlsx_series_sheet() -> None:
    b = bs.load(b'format: household-budget/bill-system\nversion: 1\nhousehold: {name: T}\n'
                b'bills:\n  - {id: rent, name: Rent, amount: 900, schedule: {cadence: monthly, day: 3}}\n'
                b'  - {id: pay, name: "=Pay", amount: 9, schedule: {cadence: semimonthly, days: [1, 15]}}\n',
                'x.yaml', today=date(2029, 1, 5)).budget
    wb = load_workbook(io.BytesIO(export.forecast_xlsx(b, compute(b, today=b.as_of))))
    s = wb['Series']
    assert [s.cell(row=2, column=c).value for c in (1, 2, 4, 5, 7)] == ['rent', 'Rent', 900.0, 'monthly', '3']
    assert s.cell(row=3, column=2).value == '=Pay' and s.cell(row=3, column=2).data_type == 's'
    assert s.cell(row=3, column=7).value == '1, 15'


def test_backup_text_round_trips() -> None:
    b = _budget()
    extras = bs.BackupExtras(notes='n')
    for fmt in ('yaml', 'json'):
        text = export.backup(b, extras, fmt=fmt, exported_at=datetime(2029, 3, 1, 9), app_version='t',  # type: ignore[arg-type]
                             household_name='H')
        back = bs.load(text.encode(), 'b.' + fmt, today=date(2030, 1, 1))
        assert back.budget == b and back.extras == extras
