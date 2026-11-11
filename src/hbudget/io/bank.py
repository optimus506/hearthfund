"""Bank and card CSV exports with mapping profiles (ARCHITECTURE 12.4).

Descriptions are masked before they leave this module; columns that hold account or card numbers are never read.
Transactions come out newest first, with file_index = their position (0 = newest).
"""
from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

from hbudget.core import money
from hbudget.core.models import BankTxn, CardCategory, CardTxn, Cents
from hbudget.core.rules import compile_safe, safe_search
from hbudget.core.textmask import mask

from . import ImportProblem
from .csvfile import read_rows
from .workbook_import import WorkbookMapping, parse_date

__all__ = ['BUILTIN_PROFILES', 'BankFile', 'BankMapping', 'MappingProfile', 'detect_profile', 'finish',
           'read_bank_csv', 'read_card', 'txn_id']


@dataclass(frozen=True, slots=True)
class BankMapping:
    date_col: str                    # header names, not letters
    amount_col: str | None = None    # signed amount, or:
    debit_col: str | None = None
    credit_col: str | None = None
    description_cols: tuple[str, ...] = ('Description',)
    balance_col: str | None = None
    id_col: str | None = None
    status_col: str | None = None
    posted_values: tuple[str, ...] = ('Posted',)
    category_col: str | None = None
    date_format: str | None = None
    negate: bool = False             # for exports where purchases are positive
    ignore_cols: tuple[str, ...] = ()   # columns never read (account / card numbers)
    purchase_date_regex: str | None = None   # card exports: purchase date inside the text, groups m, d, y


@dataclass(frozen=True, slots=True)
class MappingProfile:
    id: str
    name: str
    kind: Literal['bank', 'card', 'workbook']
    mapping: BankMapping | WorkbookMapping
    header: tuple[str, ...] = ()
    builtin: bool = False


@dataclass(frozen=True, slots=True)
class BankFile:
    txns: tuple[BankTxn, ...]        # newest first, file_index assigned, descriptions masked
    ignored: int                     # non-posted rows
    duplicates: int                  # dropped duplicates inside the file
    first: date
    last: date
    latest_balance: Cents | None
    has_balance: bool


BUILTIN_PROFILES: tuple[MappingProfile, ...] = (
    MappingProfile('builtin-date-desc-debit-credit-balance', 'Date / Description / Debit / Credit / Balance', 'bank',
                   BankMapping('Date', debit_col='Debit', credit_col='Credit', balance_col='Balance'),
                   ('Date', 'Description', 'Debit', 'Credit', 'Balance'), builtin=True),
    MappingProfile('builtin-date-desc-amount-balance', 'Date / Description / Amount / Balance', 'bank',
                   BankMapping('Date', amount_col='Amount', balance_col='Balance'),
                   ('Date', 'Description', 'Amount', 'Balance'), builtin=True),
    MappingProfile('builtin-date-desc-amount', 'Date / Description / Amount', 'bank',
                   BankMapping('Date', amount_col='Amount'), ('Date', 'Description', 'Amount'), builtin=True),
)
# the bank's own transaction id, best first (#87: "Transaction ID" was missed, so such exports got content ids)
ID_NAMES = ('transaction id', 'transaction number', 'id', 'fitid', 'transaction reference', 'reference number',
            'reference', 'ref')
_STATUS_NAMES = ('status', 'state')


def _norm(h: str) -> str:
    return ' '.join(h.strip().lower().split())


def id_column(header: Sequence[str]) -> str | None:
    """The header's transaction-id column, by the names banks use (ID_NAMES, best first)."""
    have = {_norm(h): h for h in header}
    return next((have[n] for n in ID_NAMES if n in have), None)


def detect_profile(header: Sequence[str], profiles: Sequence[MappingProfile]) -> MappingProfile | None:
    """The household's saved profile whose header matches (case-insensitive), else a fitting built-in. A saved bank
    or card profile made without an ID column picks up the header's one (#87: the mapping form did not offer it)."""
    have = [_norm(h) for h in header]
    for p in profiles:
        if p.kind != 'workbook' and p.header and [_norm(h) for h in p.header] == have:
            m = p.mapping
            if isinstance(m, BankMapping) and m.id_col is None and (found := id_column(header)) is not None:
                return replace(p, mapping=replace(m, id_col=found))
            return p
    present = set(have)
    for p in BUILTIN_PROFILES:
        if not {_norm(h) for h in p.header} <= present:
            continue
        m = p.mapping
        assert isinstance(m, BankMapping)
        id_col = id_column(header)
        status_col = next((h for h in header if _norm(h) in _STATUS_NAMES), None)
        actual = {_norm(h): h for h in header}
        m = replace(m, date_col=actual[_norm(m.date_col)],
                    description_cols=tuple(actual[_norm(c)] for c in m.description_cols),
                    amount_col=actual[_norm(m.amount_col)] if m.amount_col else None,
                    debit_col=actual[_norm(m.debit_col)] if m.debit_col else None,
                    credit_col=actual[_norm(m.credit_col)] if m.credit_col else None,
                    balance_col=actual[_norm(m.balance_col)] if m.balance_col else None,
                    id_col=id_col, status_col=status_col)
        return replace(p, mapping=m, header=tuple(header))
    return None


def txn_id(posted: date, amount: Cents, description: str, n: int) -> str:
    """Content id for exports without one: the n-th identical row in the file stays a separate transaction."""
    raw = '%s|%d|%s|%d' % (posted.isoformat(), amount, description, n)
    return 'h:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def _amount(text: str, line: int, what: str) -> Cents:
    t = text.strip()
    if not t:
        return 0
    try:
        return money.parse(t)
    except ValueError:
        raise ImportProblem('The %s could not be read.' % what, line=line) from None


def _oldest_first(rows: Sequence[tuple[date, Cents, Cents | None]]) -> bool:
    """True when the export lists the oldest posting first (by the balance chain when there is one, else dates)."""
    if len(rows) < 2:
        return False
    if all(b is not None for _, _, b in rows):
        newest_first = all(rows[i + 1][2] + rows[i][1] == rows[i][2] for i in range(len(rows) - 1))  # type: ignore[operator]
        oldest_first = all(rows[i][2] + rows[i + 1][1] == rows[i + 1][2] for i in range(len(rows) - 1))  # type: ignore[operator]
        if newest_first != oldest_first:
            return oldest_first
    return rows[0][0] < rows[-1][0]


def finish(txns: Sequence[BankTxn], *, ignored: int = 0, duplicates: int = 0) -> BankFile:
    """Newest first (stable within a day), file_index assigned, and the summary fields."""
    if not txns:
        if ignored:
            raise ImportProblem('The file has no posted transactions.')
        raise ImportProblem('The file has no transactions.')
    ordered = sorted(txns, key=lambda t: t.posted, reverse=True)
    out = tuple(replace(t, file_index=i) for i, t in enumerate(ordered))
    has_balance = all(t.balance is not None for t in out)
    return BankFile(out, ignored, duplicates, min(t.posted for t in out), max(t.posted for t in out),
                    out[0].balance if has_balance else None, has_balance)


def _column(header: Sequence[str], name: str | None, ignore: set[str]) -> int | None:
    if not name:
        return None
    want = _norm(name)
    if want in ignore:
        raise ImportProblem('The column "%s" is marked as never to be read.' % name)
    for i, h in enumerate(header):
        if _norm(h) == want:
            return i
    raise ImportProblem('The file has no "%s" column.' % name)


def _parse(data: bytes, mapping: BankMapping) -> tuple[list[tuple[int, date, Cents, str, Cents | None, str, str]], int]:
    """(line, posted, amount, masked description, balance, category, id or '') per posted row, and the ignored count."""
    rows = read_rows(data)
    if not rows:
        raise ImportProblem('The file has no transactions.')
    header, body = rows[0], rows[1:]
    ignore = {_norm(c) for c in mapping.ignore_cols}
    i_date = _column(header, mapping.date_col, ignore)
    i_amount = _column(header, mapping.amount_col, ignore)
    i_debit = _column(header, mapping.debit_col, ignore)
    i_credit = _column(header, mapping.credit_col, ignore)
    if i_amount is None and i_debit is None and i_credit is None:
        raise ImportProblem('The mapping needs an amount column, or debit and credit columns.')
    i_desc = [_column(header, c, ignore) for c in mapping.description_cols]
    i_bal = _column(header, mapping.balance_col, ignore)
    i_id = _column(header, mapping.id_col, ignore)
    i_status = _column(header, mapping.status_col, ignore)
    i_cat = _column(header, mapping.category_col, ignore)
    posted_values = {_norm(v) for v in mapping.posted_values}
    out = []
    ignored = 0

    def get(row: list[str], i: int | None) -> str:
        return row[i] if i is not None and i < len(row) else ''

    for line, row in enumerate(body, 2):
        if i_status is not None and _norm(get(row, i_status)) not in posted_values:
            ignored += 1
            continue
        assert i_date is not None
        posted = parse_date(get(row, i_date), mapping.date_format)
        if posted is None:
            raise ImportProblem('The date could not be read.', line=line)
        if i_amount is not None:
            amount = _amount(get(row, i_amount), line, 'amount')
        else:
            amount = _amount(get(row, i_credit), line, 'credit') - abs(_amount(get(row, i_debit), line, 'debit'))
        if mapping.negate:
            amount = -amount
        desc = mask(' '.join(get(row, i) for i in i_desc if i is not None))
        bal_text = get(row, i_bal)
        balance = _amount(bal_text, line, 'balance') if i_bal is not None and bal_text.strip() else None
        out.append((line, posted, amount, desc, balance, get(row, i_cat), get(row, i_id).strip()))
    return out, ignored


def read_bank_csv(data: bytes, mapping: BankMapping) -> BankFile:
    parsed, ignored = _parse(data, mapping)
    if _oldest_first([(p[1], p[2], p[4]) for p in parsed]):
        parsed.reverse()
    seen: set[str] = set()
    counts: Counter[tuple[date, Cents, str]] = Counter()
    duplicates = 0
    txns: list[BankTxn] = []
    for _line, posted, amount, desc, balance, cat, rid in parsed:
        if rid:
            tid = rid
        else:
            key = (posted, amount, desc)
            counts[key] += 1
            tid = txn_id(posted, amount, desc, counts[key])
        if tid in seen:
            duplicates += 1
            continue
        seen.add(tid)
        txns.append(BankTxn(id=tid, posted=posted, amount=amount, description=desc, balance=balance, category=cat))
    return finish(txns, ignored=ignored, duplicates=duplicates)


# Card exports that put the purchase date in the description ("... Date 10/30/26 ..."): tried when the layout
# has no pattern of its own (#63). Only a date up to DEFAULT_DATE_DAYS before the posting date is taken.
DEFAULT_PURCHASE_DATE = r'\bDATE (?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{2,4})\b'
DEFAULT_DATE_DAYS = 60


def card_date_pattern(mapping: BankMapping | None) -> str:
    """The purchase-date pattern a card upload uses: the layout's own, else the default."""
    return (mapping.purchase_date_regex if mapping else None) or DEFAULT_PURCHASE_DATE


def purchase_date(desc: str, posted: date, pattern: str | None) -> date:
    """The purchase date in a card description, else the posting date."""
    bought = _purchase_date(desc, posted, pattern)
    if pattern == DEFAULT_PURCHASE_DATE and (posted - bought).days > DEFAULT_DATE_DAYS:
        return posted
    return bought


def _purchase_date(desc: str, posted: date, pattern: str | None) -> date:
    if not pattern:
        return posted
    m = safe_search(pattern, desc)          # user pattern: restricted syntax, text cut to rules.MAX_TEXT
    if not m:
        return posted
    try:
        mo, d, y = int(m.group('m')), int(m.group('d')), int(m.group('y'))
    except (IndexError, ValueError):
        return posted
    if y < 100:
        y += 2000
    try:
        bought = date(y, mo, d)
    except ValueError:
        return posted
    if bought > posted:              # "Dec 30" read in a January posting with a 2-digit year typo: keep posted
        return posted
    return bought


def read_card(data: bytes, filename: str, mapping: BankMapping | None, categories: Sequence[CardCategory]
              ) -> list[CardTxn]:
    """Card export (CSV with a mapping or a built-in layout, or OFX/QFX) -> categorised card transactions."""
    from hbudget.core.cards import categorise

    if not data.strip():
        raise ImportProblem('The file has no transactions.')
    if mapping is not None and mapping.purchase_date_regex:   # a bad pattern is a clear error before any row
        try:
            compile_safe(mapping.purchase_date_regex)
        except ValueError as e:
            raise ImportProblem('Purchase date pattern: %s' % e) from None
    if filename.lower().endswith(('.ofx', '.qfx')):
        from .ofx import read_ofx
        bank = read_ofx(data)
        rows = [(t, '') for t in bank.txns]
    else:
        if mapping is None:
            header = read_rows(data)[:1]
            prof = detect_profile(header[0] if header else [], ())
            if prof is None or not isinstance(prof.mapping, BankMapping):
                raise ImportProblem('The card export layout is not recognised; map its columns first.')
            mapping = prof.mapping
        bank = read_bank_csv(data, mapping)
        rows = [(t, t.category) for t in bank.txns]
    regex = card_date_pattern(mapping)
    out = []
    for t, cat in rows:
        bought = purchase_date(t.description, t.posted, regex)
        category = categorise(t.description, cat, categories, amount=t.amount)
        out.append(CardTxn(id=t.id, purchased=bought, posted=t.posted, amount=t.amount, description=t.description,
                           category=category))
    return out

