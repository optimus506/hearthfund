"""Sheet / Cell model shared by the CSV, ODS and XLSX readers (ARCHITECTURE 12.2)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from . import ImportProblem
from .limits import check_size

__all__ = ['Cell', 'Sheet', 'cell_at', 'col_index', 'col_letter', 'read_table', 'split_ref']

EMPTY_TEXT = ''


@dataclass(frozen=True, slots=True)
class Cell:
    value: str | int | float | date | None   # typed value (CSV cells are text)
    text: str                                 # display text
    background: str | None = None             # "#rrggbb", lower case


@dataclass(frozen=True, slots=True)
class Sheet:
    name: str
    rows: tuple[tuple[Cell, ...], ...]


def col_letter(index: int) -> str:
    """0 -> "A", 25 -> "Z", 26 -> "AA"."""
    if index < 0:
        raise ValueError('negative column index')
    out = ''
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def col_index(letters: str) -> int:
    """"A" -> 0, "AA" -> 26. ValueError for anything that is not column letters."""
    s = letters.strip().upper()
    if not s or not s.isascii() or not s.isalpha() or len(s) > 3:
        raise ValueError('not a column: %r' % letters)
    n = 0
    for ch in s:
        n = n * 26 + ord(ch) - 64
    return n - 1


def split_ref(ref: str) -> tuple[int, int]:
    """"B1" -> (row 1-based, column 0-based)."""
    m = re.fullmatch(r'\s*([A-Za-z]{1,3})\$?(\d{1,7})\s*', ref.replace('$', ''))
    if not m:
        raise ValueError('not a cell reference: %r' % ref)
    return int(m.group(2)), col_index(m.group(1))


def cell_at(sheet: Sheet, row: int, col: int) -> Cell | None:
    """Cell by 1-based row and 0-based column, None outside the data."""
    if row < 1 or row > len(sheet.rows):
        return None
    cells = sheet.rows[row - 1]
    return cells[col] if 0 <= col < len(cells) else None


def read_table(data: bytes, filename: str) -> list[Sheet]:
    """Dispatch on the magic bytes (a zip is ODS or XLSX, by its content) and then on the extension."""
    check_size(data)
    if not data.strip():
        raise ImportProblem('The file is empty.')
    lower = (filename or '').lower()
    if data[:4] == b'PK\x03\x04':
        from .limits import open_zip
        z = open_zip(data)
        names = set(z.namelist())
        if 'content.xml' in names and ('mimetype' in names or lower.endswith('.ods')):
            from .ods import read_ods
            return read_ods(data)
        if '[Content_Types].xml' in names:
            from .xlsx import read_xlsx
            return read_xlsx(data)
        raise ImportProblem('This zip file is not an .ods or .xlsx spreadsheet.')
    if lower.endswith(('.ods', '.xlsx', '.xlsm', '.xls')):
        raise ImportProblem('The file is not a valid %s spreadsheet.' % lower.rsplit('.', 1)[-1])
    from .csvfile import read_csv
    return [read_csv(data, filename)]
