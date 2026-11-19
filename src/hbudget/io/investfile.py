"""Investment files (D-103): what an account held on a date, read from a broker's or a plan's export.

Three shapes are read, and only dates and totals come out of any of them:

- an OFX / QFX investment statement: the statement date and the value of the holdings plus the cash;
- a table with a date column: one balance per date (a history kept in a spreadsheet, or a "value over time" export);
- a table of holdings without dates (a "positions" export): the holdings' values added up, per account.

What is bought, sold or held is never kept. An account's label is masked: at most the last four digits stay."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from hbudget.core.models import Cents
from hbudget.core.textmask import mask

from . import ImportProblem
from .csvfile import decode
from .limits import check_size
from .ofx import _amount, _date, _Node, _sgml, _xml
from .tabular import Cell, read_table
from .workbook_import import cell_cents, parse_date

__all__ = ['OFX_EXT', 'TABLE_EXT', 'InvestFile', 'Reading', 'read_invest_file']

OFX_EXT = ('.ofx', '.qfx')
TABLE_EXT = ('.csv', '.ods', '.xlsx')
MAX_READINGS = 20          # accounts read out of one file
HEADER_ROWS = 40           # a broker's export may open with a few lines of text before the table

_DATE_NAMES = ('date', 'as of', 'as of date', 'statement date', 'end date', 'date ending', 'period ending',
               'period end', 'month', 'month ending')
_VALUE_NAMES = ('current value', 'market value', 'mkt val', 'total value', 'ending value', 'ending balance',
                'end balance', 'account value', 'portfolio value', 'balance', 'value', 'total')
_ACCOUNT_NAMES = ('account name', 'account', 'account name/number', 'account number', 'account #')
_TOTAL_WORDS = ('total', 'account total', 'grand total', 'totals')


@dataclass(frozen=True, slots=True)
class Reading:
    label: str                                  # what the file calls the account, masked; '' when it names none
    points: tuple[tuple[date | None, Cents], ...]   # oldest first; a date of None: the file gives a value, no date
    holdings: Cents | None = None               # a statement's two parts, when it lists cash on its own:
    cash: Cents | None = None                   # some brokers count the cash among the holdings as well


@dataclass(frozen=True, slots=True)
class InvestFile:
    kind: Literal['statement', 'history', 'positions']
    readings: tuple[Reading, ...]
    skipped: int = 0                            # rows without a readable date or amount


def read_invest_file(data: bytes, filename: str) -> InvestFile:
    check_size(data)
    lower = (filename or '').lower()
    if lower.endswith(OFX_EXT) or b'<OFX' in data[:4096].upper():
        return _ofx(data)
    return _table(data, filename)


# ---------------------------------------------------------------- OFX / QFX

def _label(node: _Node, n: int) -> str:
    ids = [x.values.get('ACCTID', '') for x in node.iter('INVACCTFROM') + node.iter('BANKACCTFROM')]
    tail = ''.join(ch for ch in (ids[0] if ids else '') if ch.isdigit())[-4:]
    return 'Account ending %s' % tail if tail else 'Account %d' % n


def _money(node: _Node, tag: str) -> Cents | None:
    raw = node.values.get(tag)
    if raw is None:
        return None
    try:
        return _amount(raw)
    except ValueError:
        return None


def _ofx(data: bytes) -> InvestFile:
    head = data[:512].decode('latin-1', 'replace').upper()
    text = data.decode('cp1252', 'replace') if 'CHARSET:1252' in head else decode(data)
    if '<OFX' not in text.upper():
        raise ImportProblem('This is not an OFX or QFX file.')
    is_xml = text.lstrip().startswith('<?xml') or '<?OFX' in text[:1024].upper()
    root = _xml(text) if is_xml else _sgml(text)
    readings: list[Reading] = []
    for n, st in enumerate(root.iter('INVSTMTRS'), 1):
        day = _date(st.values.get('DTASOF'))
        plan = next((v for b in st.iter('INV401KBAL') if (v := _money(b, 'TOTAL')) is not None), None)
        values = [v for p in st.iter('INVPOS') if (v := _money(p, 'MKTVAL')) is not None]
        cash = next((v for b in st.iter('INVBAL') if (v := _money(b, 'AVAILCASH')) is not None), None)
        if plan is not None:
            readings.append(Reading(_label(st, n), ((day, plan),)))
        elif values or cash is not None:
            held = sum(values)
            split = bool(values) and bool(cash)
            readings.append(Reading(_label(st, n), ((day, held + (cash or 0)),),
                                    holdings=held if split else None, cash=cash if split else None))
    if not readings:                       # a savings account or a certificate: a bank statement with its balance
        for n, st in enumerate(root.iter('STMTRS'), 1):
            for bal in st.iter('LEDGERBAL'):
                v = _money(bal, 'BALAMT')
                if v is not None:
                    readings.append(Reading(_label(st, n), ((_date(bal.values.get('DTASOF')), v),)))
                    break
    if not readings:
        raise ImportProblem('The file holds no account value: no holdings, no cash and no balance. Download the '
                            'statement or the positions of the account, not its transactions alone.')
    return InvestFile('statement', tuple(readings[:MAX_READINGS]))


# ---------------------------------------------------------------- tables: CSV, ODS, XLSX

def _norm(c: Cell) -> str:
    return ' '.join(c.text.strip().lower().replace('_', ' ').split())


def _find(header: Sequence[str], names: Sequence[str]) -> int | None:
    """The first column named like one of `names` (best name first); "Mkt Val (Market Value)" counts as "mkt val"."""
    for name in names:
        for i, h in enumerate(header):
            if h == name or h.startswith(name + ' (') or h.startswith(name + ' $'):
                return i
    return None


def _header(rows: Sequence[Sequence[Cell]]) -> tuple[int, list[str]]:
    for i, row in enumerate(rows[:HEADER_ROWS]):
        names = [_norm(c) for c in row]
        if sum(1 for n in names if n) >= 2 and (_find(names, _DATE_NAMES) is not None
                                                or _find(names, _VALUE_NAMES) is not None):
            return i, names
    raise ImportProblem('No table found in the file. It needs a row of column names with a date column and a '
                        'balance column, or a column such as "Current value" or "Market value".')


def _cell(row: Sequence[Cell], i: int | None) -> Cell | None:
    return row[i] if i is not None and i < len(row) else None


def _is_total(row: Sequence[Cell]) -> bool:
    return any(_norm(c) in _TOTAL_WORDS or _norm(c).startswith(('total ', 'account total')) for c in row[:3])


def _table(data: bytes, filename: str) -> InvestFile:
    sheets = read_table(data, filename)
    sheet = next((s for s in sheets if s.rows), None)
    if sheet is None:
        raise ImportProblem('The file is empty.')
    at, header = _header(sheet.rows)
    body = sheet.rows[at + 1:]
    date_col = _find(header, _DATE_NAMES)
    value_col = _find(header, _VALUE_NAMES)
    acct_col = _find(header, _ACCOUNT_NAMES)
    if date_col is not None and any(parse_date(_cell(r, date_col)) for r in body):
        written = [' '.join(c.text.split()) for c in sheet.rows[at]]
        return _history(written, body, date_col, value_col, acct_col)
    if value_col is None:
        raise ImportProblem('The file has no column with a balance or a value.')
    return _positions(body, value_col, acct_col)


def _history(header: Sequence[str], body: Sequence[Sequence[Cell]], date_col: int, value_col: int | None,
             acct_col: int | None) -> InvestFile:
    series: dict[str, dict[date, Cents]] = {}
    skipped = 0
    if value_col is not None:
        cols = [(value_col, '')]
    else:                                  # "Date, Plan at work, Brokerage": one column per account, named as written
        def amounts(i: int) -> bool:       # some amounts, and nothing else: a column of notes is not an account
            cells = [c for r in body if (c := _cell(r, i)) is not None and c.text.strip()]
            return bool(cells) and all(cell_cents(c) is not None for c in cells)
        cols = [(i, h) for i, h in enumerate(header) if h and i != date_col and amounts(i)]
        if not cols:
            raise ImportProblem('The file has dates but no column with a balance.')
    for row in body:
        day = parse_date(_cell(row, date_col))
        if day is None or _is_total(row):
            skipped += 1
            continue
        read = False
        for i, name in cols:
            v = cell_cents(_cell(row, i))
            if v is None:
                continue
            acct = _cell(row, acct_col)
            label = mask(name) or (mask(acct.text.strip()) if acct is not None else '')
            series.setdefault(label, {})[day] = v
            read = True
        skipped += 0 if read else 1
    if not series:
        raise ImportProblem('No row has both a date and an amount that could be read.')
    return InvestFile('history', tuple(Reading(k, tuple(sorted(v.items())))
                                       for k, v in list(series.items())[:MAX_READINGS]), skipped)


def _positions(body: Sequence[Sequence[Cell]], value_col: int, acct_col: int | None) -> InvestFile:
    sums: dict[str, Cents] = {}
    skipped = 0
    for row in body:
        v = cell_cents(_cell(row, value_col))
        if v is None or _is_total(row):
            skipped += 0 if not any(c.text.strip() for c in row[1:]) else 1   # a line of text is not a skipped row
            continue
        acct = _cell(row, acct_col)
        label = mask(acct.text.strip()) if acct is not None else ''
        sums[label] = sums.get(label, 0) + v
    if not sums:
        raise ImportProblem('No row has a value that could be read.')
    return InvestFile('positions', tuple(Reading(k, ((None, v),)) for k, v in list(sums.items())[:MAX_READINGS]),
                      skipped)
