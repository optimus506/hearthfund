"""Excel (.xlsx) reader: openpyxl in read-only, cached-values mode, after the zip limits (ARCHITECTURE 12.1).

Formulas are never evaluated. A formula cell without a cached value is returned with value None and the formula
as its text, so the workbook importer can report that row instead of reading it as empty.
"""
from __future__ import annotations

import io
import warnings
from datetime import date, datetime, time
from typing import Any

import openpyxl
import openpyxl.xml

from . import ImportProblem
from .limits import MAX_CELL_CHARS, MAX_CELLS, MAX_ROWS, MAX_SHEETS, MAX_TEXT_CHARS, Totals, check_size, open_zip
from .tabular import Cell, Sheet

__all__ = ['read_xlsx']

# openpyxl parses the XML parts itself; it must be using defusedxml (installed as a dependency).
assert openpyxl.xml.DEFUSEDXML, 'openpyxl is not using defusedxml'


def _bg(cell: Any) -> str | None:
    fill = getattr(cell, 'fill', None)
    if fill is None or getattr(fill, 'fill_type', None) != 'solid':
        return None
    colour = fill.fgColor
    if getattr(colour, 'type', None) != 'rgb' or not isinstance(colour.rgb, str) or len(colour.rgb) != 8:
        return None    # theme / indexed colours are ignored
    return '#' + colour.rgb[2:].lower()


def _text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(' ')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _value(value: Any) -> str | int | float | date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, (int, float, str, date)) or value is None:
        return value
    return str(value)


def read_xlsx(data: bytes) -> list[Sheet]:
    check_size(data)
    open_zip(data)                       # real decompressed sizes are checked before openpyxl sees the file
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            cached = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            formulas = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception:   # openpyxl raises many types for damaged files
        raise ImportProblem('The file is not a valid .xlsx spreadsheet.') from None
    sheets: list[Sheet] = []
    totals = Totals()
    try:
        if len(cached.worksheets) > MAX_SHEETS:
            raise ImportProblem('The workbook has more than %d sheets.' % MAX_SHEETS)
        for ws in cached.worksheets:
            wf = formulas[ws.title]
            rows: list[tuple[Cell, ...]] = []
            for row_c, row_f in zip(ws.iter_rows(), wf.iter_rows(), strict=False):
                cells: list[Cell] = []
                for c, f in zip(row_c, row_f, strict=False):
                    raw = getattr(c, 'value', None)
                    formula = getattr(f, 'value', None)
                    if raw is None and isinstance(formula, str) and formula.startswith('='):
                        cells.append(Cell(None, formula, _bg(c)))
                    else:
                        cells.append(Cell(_value(raw), _text(raw), _bg(c)))
                while cells and cells[-1].value is None and not cells[-1].text:
                    cells.pop()
                rows.append(tuple(cells))
                if len(rows) > MAX_ROWS:
                    raise ImportProblem('The sheet has more than %d rows.' % MAX_ROWS)
                chars = sum(len(c.text) for c in cells)
                if any(len(c.text) > MAX_CELL_CHARS for c in cells):
                    raise ImportProblem('A cell holds more than %d characters of text.' % MAX_CELL_CHARS)
                # cells and text are counted for the whole workbook, not per sheet
                totals.add(len(cells), chars, max_cells=MAX_CELLS, max_chars=MAX_TEXT_CHARS)
            while rows and not rows[-1]:
                rows.pop()
            sheets.append(Sheet(ws.title, tuple(rows)))
    finally:
        cached.close()
        formulas.close()
    if not sheets:   # pragma: no cover - openpyxl always gives a workbook at least one sheet
        raise ImportProblem('The spreadsheet has no sheets.')
    return sheets
