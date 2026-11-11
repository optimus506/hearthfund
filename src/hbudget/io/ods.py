"""OpenDocument spreadsheet (.ods) reader: zipfile + defusedxml, values, display text and cell backgrounds.

Repeated cells and rows are expanded only up to the limits in io.limits. Empty repeated cells and rows (LibreOffice
writes thousands of them at the end of every row and sheet) are counted, and only materialised when real data
follows them, so trailing blanks never cost memory.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from . import ImportProblem
from .limits import (
    MAX_CELL_CHARS,
    MAX_CELLS,
    MAX_DEPTH,
    MAX_REPEAT_COLS,
    MAX_REPEAT_ROWS,
    MAX_ROWS,
    MAX_SHEETS,
    MAX_SPACES,
    MAX_TEXT_CHARS,
    Totals,
    check_size,
    open_zip,
    read_member,
)
from .tabular import Cell, Sheet

__all__ = ['read_ods']

NS = {
    'table': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0',
    'office': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0',
    'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0',
    'style': 'urn:oasis:names:tc:opendocument:xmlns:style:1.0',
    'fo': 'urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0',
}


def _q(prefix: str, name: str) -> str:
    return '{%s}%s' % (NS[prefix], name)


T_TABLE, T_ROW, T_CELL, T_COVERED = _q('table', 'table'), _q('table', 'table-row'), _q('table', 'table-cell'), \
    _q('table', 'covered-table-cell')
T_COLUMN = _q('table', 'table-column')
ROW_GROUPS = {_q('table', n) for n in ('table-header-rows', 'table-rows', 'table-row-group')}
COL_GROUPS = {_q('table', n) for n in ('table-header-columns', 'table-columns', 'table-column-group')}
A_REP_COL, A_REP_ROW = _q('table', 'number-columns-repeated'), _q('table', 'number-rows-repeated')
A_STYLE, A_DEFAULT_STYLE = _q('table', 'style-name'), _q('table', 'default-cell-style-name')
A_VTYPE, A_VALUE, A_DATE = _q('office', 'value-type'), _q('office', 'value'), _q('office', 'date-value')
A_STRING, A_BOOL = _q('office', 'string-value'), _q('office', 'boolean-value')
T_P, T_S, T_TAB, T_BR = _q('text', 'p'), _q('text', 's'), _q('text', 'tab'), _q('text', 'line-break')
S_STYLE, S_NAME, S_PARENT = _q('style', 'style'), _q('style', 'name'), _q('style', 'parent-style-name')
S_CELLPROPS, FO_BG = _q('style', 'table-cell-properties'), _q('fo', 'background-color')
A_TABLE_NAME = _q('table', 'name')


def _parse(xml: bytes) -> Element:
    try:
        root: Element = SafeET.fromstring(xml, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException:
        raise ImportProblem('The spreadsheet contains XML features that are not allowed (DTD or entities).') from None
    except SafeET.ParseError:
        raise ImportProblem('The spreadsheet XML is damaged.') from None
    return root


def _styles(*roots: Element) -> dict[str, str | None]:
    """Background colour per cell style name, following parent styles."""
    own: dict[str, tuple[str | None, str | None]] = {}
    for root in roots:
        for st in root.iter(S_STYLE):
            name = st.get(S_NAME)
            if not name:
                continue
            props = st.find(S_CELLPROPS)
            bg = props.get(FO_BG) if props is not None else None
            own[name] = (bg, st.get(S_PARENT))
    out: dict[str, str | None] = {}
    for name in own:
        seen: set[str] = set()
        cur: str | None = name
        colour: str | None = None
        while cur and cur in own and cur not in seen:
            seen.add(cur)
            bg, parent = own[cur]
            if bg:
                colour = bg
                break
            cur = parent
        out[name] = _colour(colour)
    return out


def _colour(value: str | None) -> str | None:
    if not value or not value.startswith('#') or len(value) != 7:
        return None
    return value.lower()


def _int(value: str | None) -> int:
    """A repeat count: at least 1, at most 2**31 (callers cap what they expand)."""
    try:
        return min(max(1, int(value or 1)), 1 << 31)
    except ValueError:
        return 1


class _Text:
    """Collects a cell's text with a running length cap."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0

    def add(self, s: str) -> None:
        self.size += len(s)
        if self.size > MAX_CELL_CHARS:
            raise ImportProblem('A cell holds more than %d characters of text.' % MAX_CELL_CHARS)
        self.parts.append(s)


def _p_text(p: Element, out: _Text, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ImportProblem('The spreadsheet text is nested too deeply.')
    out.add(p.text or '')
    for child in p:
        if child.tag == T_S:
            out.add(' ' * min(_int(child.get(_q('text', 'c'))), MAX_SPACES))
        elif child.tag == T_TAB:
            out.add('\t')
        elif child.tag == T_BR:
            out.add('\n')
        else:
            _p_text(child, out, depth + 1)
        out.add(child.tail or '')


def _cell(el: Element, style: str | None, styles: dict[str, str | None]) -> tuple[Cell, bool]:
    """(cell, has_content)."""
    collected = _Text()
    for i, p in enumerate(el.findall(T_P)):
        if i:
            collected.add('\n')
        _p_text(p, collected)
    text = ''.join(collected.parts)
    vtype = el.get(A_VTYPE)
    value: Any = None
    if vtype in ('float', 'percentage', 'currency'):
        try:
            value = float(el.get(A_VALUE) or 'nan')
        except ValueError:
            value = None
        if value is not None and value != value:   # NaN: not a usable number
            value = None
    elif vtype == 'date':
        raw = el.get(A_DATE) or ''
        try:
            value = datetime.fromisoformat(raw).date() if 'T' in raw else date.fromisoformat(raw[:10])
        except ValueError:
            value = None
    elif vtype == 'boolean':
        value = el.get(A_BOOL) or text
    elif vtype == 'string':
        value = el.get(A_STRING) or text
    elif text:
        value = text
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        raise ImportProblem('A cell holds more than %d characters of text.' % MAX_CELL_CHARS)
    name = el.get(A_STYLE) or style
    bg = styles.get(name) if name else None
    return Cell(value, text, bg), bool(text) or value is not None


class _Columns:
    """Default cell style per column index (columns are repeated too)."""

    def __init__(self, table: Element) -> None:
        self.segments: list[tuple[int, str | None]] = []

        def walk(node: Element, depth: int) -> None:
            if depth > MAX_DEPTH:
                raise ImportProblem('The spreadsheet columns are nested too deeply.')
            for child in node:
                if child.tag == T_COLUMN:
                    self.segments.append((_int(child.get(A_REP_COL)), child.get(A_DEFAULT_STYLE)))
                elif child.tag in COL_GROUPS:
                    walk(child, depth + 1)
        walk(table, 0)

    def style(self, index: int) -> str | None:
        pos = 0
        for count, style in self.segments:
            if index < pos + count:
                return style
            pos += count
        return None


def _rows(table: Element) -> list[Element]:
    out: list[Element] = []

    def walk(node: Element, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise ImportProblem('The spreadsheet rows are nested too deeply.')
        for child in node:
            if child.tag == T_ROW:
                out.append(child)
            elif child.tag in ROW_GROUPS:
                walk(child, depth + 1)
    walk(table, 0)
    return out


def _read_row(row: Element, columns: _Columns, styles: dict[str, str | None]) -> tuple[Cell, ...]:
    cells: list[Cell] = []
    pending: list[tuple[Cell, int]] = []   # empty cells not yet known to be inside the data
    col = 0
    for el in row:
        if el.tag not in (T_CELL, T_COVERED):
            continue
        rep = _int(el.get(A_REP_COL))
        cell, filled = _cell(el, columns.style(col), styles)
        col += rep
        if not filled:
            pending.append((cell, rep))
            continue
        if rep > MAX_REPEAT_COLS:
            raise ImportProblem('A row repeats one value across more than %d columns.' % MAX_REPEAT_COLS)
        for empty, n in pending:
            if n > MAX_REPEAT_COLS:
                raise ImportProblem('A row has more than %d empty columns before its data.' % MAX_REPEAT_COLS)
            cells.extend([empty] * n)
        pending.clear()
        cells.extend([cell] * rep)
        if len(cells) > MAX_REPEAT_COLS * 16:
            raise ImportProblem('A row has too many columns.')
    return tuple(cells)


def _read_sheet(table: Element, styles: dict[str, str | None], totals: Totals) -> Sheet:
    columns = _Columns(table)
    rows: list[tuple[Cell, ...]] = []
    pending_empty = 0
    for row in _rows(table):
        rep = _int(row.get(A_REP_ROW))
        cells = _read_row(row, columns, styles)
        if not cells:
            pending_empty += rep
            continue
        if rep > MAX_REPEAT_ROWS:
            raise ImportProblem('A sheet repeats one row more than %d times.' % MAX_REPEAT_ROWS)
        if pending_empty:
            if len(rows) + pending_empty > MAX_ROWS:
                raise ImportProblem('The sheet has more than %d rows.' % MAX_ROWS)
            rows.extend([()] * pending_empty)
            pending_empty = 0
        rows.extend([cells] * rep)
        if len(rows) > MAX_ROWS:
            raise ImportProblem('The sheet has more than %d rows.' % MAX_ROWS)
        # cells and text are counted for the whole workbook, not per sheet
        totals.add(len(cells) * rep, sum(len(c.text) for c in cells) * rep, max_cells=MAX_CELLS,
                   max_chars=MAX_TEXT_CHARS)
    return Sheet(table.get(A_TABLE_NAME) or 'Sheet', tuple(rows))


def read_ods(data: bytes) -> list[Sheet]:
    check_size(data)
    z = open_zip(data)
    content = _parse(read_member(z, 'content.xml'))
    roots = [content]
    if 'styles.xml' in z.namelist():
        roots.append(_parse(read_member(z, 'styles.xml')))
    styles = _styles(*roots)
    tables = list(content.iter(T_TABLE))
    if len(tables) > MAX_SHEETS:
        raise ImportProblem('The workbook has more than %d sheets.' % MAX_SHEETS)
    totals = Totals()
    sheets = [_read_sheet(t, styles, totals) for t in tables]
    if not sheets:
        raise ImportProblem('The spreadsheet has no sheets.')
    return sheets
