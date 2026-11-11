"""Spreadsheet import with a column-mapping step (ARCHITECTURE 12.3).

The user (or guess_mapping) says which columns hold the name, amount and date, where the data starts and, if the
sheet has them, which cells hold the starting balance and the sheet's own low point and end balance. Rows keep the
sheet order exactly: order_date is the running maximum of the due dates and seq is the sheet row number.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Literal

from hbudget.core import money
from hbudget.core.models import Budget, Cents, Horizon, Kind, LedgerRow, Settings, Status
from hbudget.core.rules import compile_safe, safe_search

from . import ImportProblem
from .infer import infer_series
from .tabular import Cell, Sheet, cell_at, col_index, col_letter, split_ref

if TYPE_CHECKING:
    from hbudget.core.forecast import Forecast

__all__ = ['ImportedBudget', 'Preview', 'StoredCheck', 'WorkbookMapping', 'cell_cents', 'guess_mapping',
           'import_rows', 'parse_date', 'preview']

LATE_WARNING_DAYS = 14
PREVIEW_ROWS = 20


@dataclass(frozen=True, slots=True)
class WorkbookMapping:
    sheet: str
    header_row: int | None           # 1-based; None = no header
    first_row: int                   # 1-based first data row
    name_col: str                    # column letter, e.g. "A"
    amount_col: str
    date_col: str
    sign: Literal['positive_out', 'positive_in'] = 'positive_out'
    income_col: str | None = None    # optional column that flags income (non-empty = income)
    start_balance_cell: str | None = None
    start_balance_value: Cents | None = None
    stop_at_blank_name: bool = True
    skip_names: tuple[str, ...] = () # regexes; matching rows are skipped (e.g. a carry-over line)
    read_status_colours: bool = True
    status_colours: tuple[tuple[str, Status], ...] = (('#b6d7a8', Status.AUTOPAY), ('#ec9ba4', Status.MANUAL))
    date_format: str | None = None   # for text dates, e.g. "%m/%d/%Y"; None = auto
    expected_low: tuple[str, str] | None = None   # cells (value, date) to cross-check, e.g. ("C2", "C4")
    expected_end_cell: str | None = None   # a cell ("E540"), or a column ("E") = that column of the last data row
    spending_names: tuple[str, ...] = ()   # names the user ticked as spending lines (D-008)
    card_names: tuple[tuple[str, str], ...] = ()   # (row name, card account id): rows paid with that card


@dataclass(frozen=True, slots=True)
class Preview:
    sheets: tuple[str, ...]
    sheet: str
    columns: tuple[str, ...]
    header: tuple[str, ...] | None
    rows: tuple[tuple[str, ...], ...]
    guess: WorkbookMapping
    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoredCheck:
    """What the workbook itself says, read from the mapped cells."""
    start: Cents | None
    low: Cents | None
    low_date: date | None
    end: Cents | None

    def matches(self, f: Forecast) -> dict[str, bool]:
        out: dict[str, bool] = {}
        if self.start is not None:
            out['start'] = f.start == self.start
        if self.low is not None:
            out['low'] = f.low == self.low
        if self.low_date is not None:
            out['low_date'] = f.low_date == self.low_date
        if self.end is not None:
            out['end'] = f.end == self.end
        return out


@dataclass(frozen=True, slots=True)
class ImportedBudget:
    budget: Budget
    warnings: tuple[str, ...]
    stored_check: StoredCheck | None


# ---------------------------------------------------------------- cell parsing
_DATE_FORMATS = ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%d-%b-%Y', '%d-%b-%y', '%B %d, %Y', '%b %d, %Y', '%d.%m.%Y')
_ZERO_TEXT = re.compile(r'^\(?\$?\s*-\s*\)?$')   # how spreadsheets display a zero amount: "$-", " $ - "


def parse_date(cell: Cell | str | None, fmt: str | None = None) -> date | None:
    """A date from a date cell or text; None when it cannot be read."""
    value: object = cell.value if isinstance(cell, Cell) else cell
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = (cell.text if isinstance(cell, Cell) else (cell or '')).strip()
    if not text:
        return None
    for f in (fmt,) if fmt else _DATE_FORMATS:
        try:
            return datetime.strptime(text, f).date()
        except ValueError:
            continue
    return None


def cell_cents(cell: Cell | None) -> Cents | None:
    """Money from a cell: numbers are rounded to cents (stored balances can carry float noise), text is parsed."""
    if cell is None:
        return None
    v = cell.value
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int((Decimal(repr(v)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    text = (cell.text or '').strip().replace(' ', '').replace('\xa0', '')
    if not text:
        return None
    if _ZERO_TEXT.match(text):
        return 0
    if text.startswith('$(') and text.endswith(')'):   # "$(1,200.50)": a spreadsheet's negative currency
        text = text[1:]
    try:
        return money.parse(text)
    except ValueError:
        return None


def _cell(sheet: Sheet, row: int, letter: str | None) -> Cell | None:
    if not letter:
        return None
    return cell_at(sheet, row, col_index(letter))


def _ref(sheet: Sheet, ref: str | None) -> Cell | None:
    if not ref:
        return None
    r, c = split_ref(ref)
    return cell_at(sheet, r, c)


def _sheet(sheets: Sequence[Sheet], name: str) -> Sheet:
    for s in sheets:
        if s.name == name:
            return s
    raise ImportProblem('The workbook has no sheet named "%s".' % name)


# ---------------------------------------------------------------- import
def import_rows(sheets: list[Sheet], mapping: WorkbookMapping, *, today: date) -> ImportedBudget:
    sheet = _sheet(sheets, mapping.sheet)
    for pattern in mapping.skip_names:      # a bad pattern is a clear error before any row is read
        try:
            compile_safe(pattern)
        except ValueError as e:
            raise ImportProblem('Skip pattern %r: %s' % (pattern, e)) from None
    colours = {c.lower(): st for c, st in mapping.status_colours}
    spending = set(mapping.spending_names)
    cards = dict(mapping.card_names)
    warnings: list[str] = []
    rows: list[LedgerRow] = []
    last_row_number: int | None = None
    order: date | None = None
    for n in range(max(1, mapping.first_row), len(sheet.rows) + 1):
        name_cell = _cell(sheet, n, mapping.name_col)
        name = ' '.join((name_cell.text if name_cell else '').split())
        if not name:
            if mapping.stop_at_blank_name:
                break
            continue
        last_row_number = n
        if any(safe_search(p, name) for p in mapping.skip_names):
            continue
        amount_cell = _cell(sheet, n, mapping.amount_col)
        if amount_cell is not None and amount_cell.value is None and amount_cell.text.startswith('='):
            warnings.append('Row %d (%s) has a formula with no saved value; open and save the file first. '
                            'The row was skipped.' % (n, name))
            continue
        amount = cell_cents(amount_cell)
        if amount is None:
            if amount_cell is not None and amount_cell.text.strip():
                warnings.append('Row %d (%s): the amount could not be read; the row was skipped.' % (n, name))
                continue
            amount = 0
        due = parse_date(_cell(sheet, n, mapping.date_col), mapping.date_format)
        if due is None:
            warnings.append('Row %d (%s): the date could not be read; the row was skipped.' % (n, name))
            continue
        if mapping.sign == 'positive_in':
            amount = -amount
        income_flag = _cell(sheet, n, mapping.income_col)
        if income_flag is not None and income_flag.text.strip() and amount > 0:
            amount = -amount
        kind = Kind.INCOME if amount < 0 else (Kind.SPENDING if name in spending else Kind.BILL)
        order = due if order is None else max(order, due)
        if (order - due).days > LATE_WARNING_DAYS:
            warnings.append('Row %d (%s) is listed long after its due date; check the date.' % (n, name))
        status = None
        if mapping.read_status_colours and kind is Kind.BILL and name_cell is not None and name_cell.background:
            status = colours.get(name_cell.background.lower())
        rows.append(LedgerRow(
            id='r%d' % n, series_id=None, name=name, kind=kind, amount=amount, due_date=due, order_date=order,
            seq=n, status_override=status, card=cards.get(name)))
    if not rows:
        raise ImportProblem('The sheet has no rows to import with this mapping.')

    end = max(r.due_date for r in rows)
    series, rows = infer_series(rows, horizon_end=end)
    start_cell = _ref(sheet, mapping.start_balance_cell)
    start = cell_cents(start_cell) if start_cell is not None else None
    if mapping.start_balance_cell and start is None:
        warnings.append('The starting balance cell %s holds no amount; 0 was used.' % mapping.start_balance_cell)
    start_balance = start if start is not None else (mapping.start_balance_value or 0)
    settings = Settings(status_colours=tuple((st, c) for c, st in mapping.status_colours))
    budget = Budget(start_balance=start_balance, as_of=today, horizon=Horizon('fixed', end=end),
                    series=tuple(series), rows=tuple(rows), settings=settings)
    return ImportedBudget(budget, tuple(warnings), _stored_check(sheet, mapping, last_row_number, start))


def _stored_check(sheet: Sheet, mapping: WorkbookMapping, last_row: int | None, start: Cents | None
                  ) -> StoredCheck | None:
    if not (mapping.expected_low or mapping.expected_end_cell or mapping.start_balance_cell):
        return None
    low = low_date = end = None
    if mapping.expected_low:
        low = cell_cents(_ref(sheet, mapping.expected_low[0]))
        low_date = parse_date(_ref(sheet, mapping.expected_low[1]))
    ref = mapping.expected_end_cell
    if ref:
        if ref.strip().isalpha():
            end = cell_cents(_cell(sheet, last_row, ref)) if last_row else None
        else:
            end = cell_cents(_ref(sheet, ref))
    return StoredCheck(start=start, low=low, low_date=low_date, end=end)


# ---------------------------------------------------------------- guessing and preview
_NAME_WORDS = ('name', 'description', 'item', 'bill', 'payee', 'creditor', 'what')
_AMOUNT_WORDS = ('amount', 'payment', 'cost', 'value', 'sum')
_DATE_WORDS = ('due', 'date', 'when')


def _texts(row: tuple[Cell, ...]) -> list[str]:
    return [c.text.strip().lower() for c in row]


def _find(texts: Sequence[str], words: Sequence[str], exclude: set[int]) -> int | None:
    for w in words:
        for i, t in enumerate(texts):
            if i not in exclude and t and w in t:
                return i
    return None


def _numeric(sheet: Sheet, col: int, first: int) -> float:
    cells = [cell_at(sheet, n, col) for n in range(first, min(first + 30, len(sheet.rows) + 1))]
    filled = [c for c in cells if c is not None and (c.text.strip() or c.value is not None)]
    if not filled:
        return 0.0
    return sum(cell_cents(c) is not None and not isinstance(c.value, date) for c in filled) / len(filled)


def guess_mapping(sheets: list[Sheet]) -> WorkbookMapping:
    """Header-name heuristics: the first row (of the largest sheet) that names a name column and a date column."""
    if not sheets:
        raise ImportProblem('The workbook has no sheets.')
    sheet = max(sheets, key=lambda s: len(s.rows))
    header_row: int | None = None
    name_i = date_i = amount_i = None
    for n, row in enumerate(sheet.rows[:30], 1):
        texts = _texts(row)
        ni = _find(texts, _NAME_WORDS, set())
        di = _find(texts, _DATE_WORDS, {ni} if ni is not None else set())
        if ni is not None and di is not None:
            header_row, name_i, date_i = n, ni, di
            amount_i = _find(texts, _AMOUNT_WORDS, {ni, di})
            break
    first = (header_row or 0) + 1
    if header_row is None:
        name_i, date_i = 0, None
    # first data row: the first row after the header with a name
    for n in range(first, min(first + 50, len(sheet.rows) + 1)):
        c = cell_at(sheet, n, name_i or 0)
        if c is not None and c.text.strip():
            first = n
            break
    width = max((len(r) for r in sheet.rows[first - 1:first + 30]), default=0)
    header = _texts(sheet.rows[header_row - 1]) if header_row else []
    if date_i is None:
        date_i = next((i for i in range(width) if parse_date(cell_at(sheet, first, i)) is not None), 1)
    if amount_i is None:
        for i in range(width):
            if i in (name_i, date_i) or (i < len(header) and 'balance' in header[i]):
                continue
            if _numeric(sheet, i, first) >= 0.8:
                amount_i = i
                break
    start_cell = None
    for n, row in enumerate(sheet.rows[:(header_row or 1) - 1], 1):
        for i, c in enumerate(row):
            if 'balance' in c.text.lower() and cell_cents(cell_at(sheet, n, i + 1)) is not None:
                start_cell = '%s%d' % (col_letter(i + 1), n)
                break
        if start_cell:
            break
    return WorkbookMapping(
        sheet=sheet.name, header_row=header_row, first_row=first, name_col=col_letter(name_i or 0),
        amount_col=col_letter(amount_i if amount_i is not None else 1), date_col=col_letter(date_i),
        start_balance_cell=start_cell)


def preview(sheets: list[Sheet], mapping: WorkbookMapping | None) -> Preview:
    guess = mapping or guess_mapping(sheets)
    sheet = _sheet(sheets, guess.sheet)
    data = sheet.rows[guess.first_row - 1:guess.first_row - 1 + PREVIEW_ROWS]
    width = max((len(r) for r in data), default=0)
    if guess.header_row:
        width = max(width, len(sheet.rows[guess.header_row - 1]))
    width = min(width, 52)
    header = None
    if guess.header_row:
        cells = sheet.rows[guess.header_row - 1]
        header = tuple((cells[i].text if i < len(cells) else '') for i in range(width))
    rows = tuple(tuple((r[i].text if i < len(r) else '') for i in range(width)) for r in data)
    names: list[str] = []
    seen: set[str] = set()
    ni = col_index(guess.name_col)
    for n in range(guess.first_row, len(sheet.rows) + 1):
        c = cell_at(sheet, n, ni)
        name = ' '.join((c.text if c else '').split())
        if not name:
            if guess.stop_at_blank_name:
                break
            continue
        if name not in seen:
            seen.add(name)
            names.append(name)
    return Preview(sheets=tuple(s.name for s in sheets), sheet=sheet.name,
                   columns=tuple(col_letter(i) for i in range(width)), header=header, rows=rows, guess=guess,
                   names=tuple(names))

