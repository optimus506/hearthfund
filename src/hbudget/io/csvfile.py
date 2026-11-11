"""CSV reader: encoding and delimiter detection, limits (ARCHITECTURE 12.1, 12.2)."""
from __future__ import annotations

import csv
import io

from . import ImportProblem
from .limits import MAX_FIELD_CHARS, MAX_ROWS, check_size
from .tabular import Cell, Sheet

__all__ = ['decode', 'read_csv', 'read_rows']


def decode(data: bytes) -> str:
    """UTF-8 (with or without BOM), then Windows-1252, then Latin-1 (which never fails)."""
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise AssertionError('latin-1 decodes every byte')  # pragma: no cover


def _dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=',;\t|')
    except csv.Error:
        return csv.excel


def read_rows(data: bytes) -> list[list[str]]:
    """All non-empty rows as lists of stripped strings. Raises ImportProblem on limits or broken quoting."""
    check_size(data)
    text = decode(data)
    if '\x00' in text:
        raise ImportProblem('The file contains binary data; save it as a CSV text file.')
    sample = text[:8192]
    rows: list[list[str]] = []
    try:
        reader = csv.reader(io.StringIO(text, newline=''), _dialect(sample))
        for line_no, row in enumerate(reader, 1):
            if not any(field.strip() for field in row):
                continue
            for field in row:
                if len(field) > MAX_FIELD_CHARS:
                    raise ImportProblem('A field is longer than %d characters.' % MAX_FIELD_CHARS, line=line_no)
            rows.append([field.strip() for field in row])
            if len(rows) > MAX_ROWS:
                raise ImportProblem('The file has more than %d rows.' % MAX_ROWS)
    except csv.Error as e:
        raise ImportProblem('The CSV file could not be read (%s).' % e) from None
    return rows


def read_csv(data: bytes, filename: str = '') -> Sheet:
    """One sheet of text cells, named after the file."""
    rows = read_rows(data)
    if not rows:
        raise ImportProblem('The file is empty.')
    name = (filename or 'CSV').rsplit('/', 1)[-1]
    return Sheet(name, tuple(tuple(Cell(v, v) for v in row) for row in rows))
