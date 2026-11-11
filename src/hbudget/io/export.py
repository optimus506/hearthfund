"""Forecast exports (CSV, XLSX) and the full backup (ARCHITECTURE 12.6).

Formula injection: text cells that start with = + - @ tab or CR get a leading apostrophe, and XLSX text cells are
always written as strings. Numeric and date columns are written as numbers and dates, never prefixed (D-033).
"""
from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

from hbudget.core.models import Budget, Cents, Kind, row_status

from . import billsystem

if TYPE_CHECKING:
    from hbudget.core.forecast import Forecast

__all__ = ['CSV_HEADER', 'backup', 'forecast_csv', 'forecast_xlsx', 'safe_text']

CSV_HEADER = ('Order', 'Name', 'Kind', 'Due date', 'Weekday', 'Amount', 'Balance', 'Status', 'State')
_DANGER = ('=', '+', '-', '@', '\t', '\r')
_MONEY_FORMAT = '"$"#,##0.00;-"$"#,##0.00'


def _dangerous(value: str) -> bool:
    """Starts (after any spaces) with a character a spreadsheet may treat as a formula."""
    return value.startswith(('\t', '\r')) or value.lstrip().startswith(_DANGER)


def safe_text(value: str) -> str:
    """A CSV text cell that a spreadsheet will never run as a formula: a leading apostrophe when needed."""
    return "'" + value if _dangerous(value) else value


def _dollars(cents: Cents) -> Decimal:
    return Decimal(cents) / 100


def _rows(budget: Budget, forecast: Forecast) -> list[tuple[int, str, str, date, str, Cents, Cents, str, str]]:
    by_row = {r.id: r for r in budget.rows}
    by_series = budget.series_by_id()
    out = []
    for i, p in enumerate(forecast.points, 1):
        row = by_row.get(p.row_id) if p.row_id else None
        if row is None:
            kind, status, state = ('income' if p.amount < 0 else 'expense'), '', 'expected'
        else:
            kind = row.kind.value
            status = row_status(row, by_series).value if row.kind is Kind.BILL else ''
            state = row.state.value
        out.append((i, p.label, kind, p.due_date, p.due_date.strftime('%A'), p.amount, p.balance, status, state))
    return out


def forecast_csv(budget: Budget, forecast: Forecast) -> bytes:
    buf = io.StringIO(newline='')
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for order, name, kind, due, weekday, amount, balance, status, state in _rows(budget, forecast):
        w.writerow([order, safe_text(name), kind, due.isoformat(), weekday, '%.2f' % _dollars(amount),
                    '%.2f' % _dollars(balance), safe_text(status), state])
    return buf.getvalue().encode('utf-8')


def _text_cell(ws: Worksheet, row: int, col: int, value: str) -> None:
    """XLSX text is stored as a string cell (never a formula); risky text also gets Excel's quote prefix,
    which hides the apostrophe but keeps the cell text."""
    cell = ws.cell(row=row, column=col)
    cell.value = value
    cell.data_type = 's'
    if _dangerous(value):
        cell.quotePrefix = True


def _money_cell(ws: Worksheet, row: int, col: int, cents: Cents) -> None:
    cell = ws.cell(row=row, column=col, value=float(_dollars(cents)))
    cell.number_format = _MONEY_FORMAT


def forecast_xlsx(budget: Budget, forecast: Forecast) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = 'Forecast'
    bold = Font(bold=True)
    summary: Sequence[tuple[str, Cents, date | None]] = [
        ('Balance', forecast.start, forecast.as_of), ('Lowest balance', forecast.low, forecast.low_date),
        ('Highest balance', forecast.high, forecast.high_date), ('End balance', forecast.end, forecast.end_date),
        *[('Year end %d' % y, v, date(y, 12, 31)) for y, v in forecast.year_ends],
    ]
    for i, (label, cents, when) in enumerate(summary, 1):
        _text_cell(ws, i, 1, label)
        ws.cell(row=i, column=1).font = bold
        _money_cell(ws, i, 2, cents)
        if when is not None:
            ws.cell(row=i, column=3, value=when).number_format = 'yyyy-mm-dd'
    top = len(summary) + 2
    for col, head in enumerate(CSV_HEADER, 1):
        _text_cell(ws, top, col, head)
        ws.cell(row=top, column=col).font = bold
    fills = {st.value: PatternFill('solid', fgColor='FF' + colour.lstrip('#').upper())
             for st, colour in budget.settings.status_colours}
    for n, line in enumerate(_rows(budget, forecast), top + 1):
        order, name, kind, due, weekday, amount, balance, status, state = line
        ws.cell(row=n, column=1, value=order)
        _text_cell(ws, n, 2, name)
        if status in fills:
            ws.cell(row=n, column=2).fill = fills[status]
        _text_cell(ws, n, 3, kind)
        ws.cell(row=n, column=4, value=due).number_format = 'yyyy-mm-dd'
        _text_cell(ws, n, 5, weekday)
        _money_cell(ws, n, 6, amount)
        _money_cell(ws, n, 7, balance)
        _text_cell(ws, n, 8, status)
        _text_cell(ws, n, 9, state)
    ws.column_dimensions['B'].width = 32
    series = wb.create_sheet('Series')
    for col, head in enumerate(('Id', 'Name', 'Kind', 'Amount', 'Cadence', 'Start', 'Day(s)', 'Status'), 1):
        _text_cell(series, 1, col, head)
    for n, s in enumerate(budget.series, 2):
        _text_cell(series, n, 1, s.id)
        _text_cell(series, n, 2, s.name)
        _text_cell(series, n, 3, s.kind.value)
        _money_cell(series, n, 4, s.amount)
        _text_cell(series, n, 5, s.schedule.cadence.value)
        series.cell(row=n, column=6, value=s.schedule.start).number_format = 'yyyy-mm-dd'
        days = s.schedule.days or ((s.schedule.day,) if s.schedule.day else ())
        _text_cell(series, n, 7, ', '.join(str(d) for d in days))
        _text_cell(series, n, 8, s.status.value)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def backup(budget: Budget, extras: billsystem.BackupExtras, *, fmt: Literal['yaml', 'json'] = 'yaml',
           exported_at: datetime, app_version: str, household_name: str = 'Household') -> str:
    """The full bill system plus the backup section, as YAML or JSON text."""
    doc = billsystem.from_budget(budget, extras, exported_at=exported_at, app_version=app_version,
                                 household_name=household_name)
    return billsystem.dump_json(doc) if fmt == 'json' else billsystem.dump_yaml(doc)

