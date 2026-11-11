"""OFX 1.x (SGML, unclosed leaf tags), OFX 2.x (XML) and QFX bank statements (ARCHITECTURE 12.4).

Only STMTTRN and LEDGERBAL are read. Account ids (ACCTID) and other account details are never read or stored.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from hbudget.core import money
from hbudget.core.models import BankTxn, Cents
from hbudget.core.textmask import mask

from . import ImportProblem
from .bank import BankFile, finish, txn_id
from .csvfile import decode
from .limits import MAX_DEPTH, MAX_ROWS, check_size

__all__ = ['read_ofx']

_TOKEN = re.compile(r'<(/?)([A-Za-z0-9._]+)[^>]*>([^<]*)')


@dataclass
class _Node:
    tag: str
    values: dict[str, str] = field(default_factory=dict)
    children: list[_Node] = field(default_factory=list)

    def iter(self, tag: str) -> list[_Node]:
        """Every node with this tag, in document order (iterative: no recursion limit)."""
        out: list[_Node] = []
        stack = [self]
        while stack:
            node = stack.pop()
            if node.tag == tag:
                out.append(node)
            stack.extend(reversed(node.children))
        return out


def _sgml(text: str) -> _Node:
    """Tolerant OFX tree: a tag followed by text is a leaf value; a tag without text opens an aggregate."""
    root = _Node('ROOT')
    stack = [root]
    for closing, tag, content in _TOKEN.findall(text):
        tag = tag.upper()
        if closing:
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == tag:
                    del stack[i:]
                    break
            continue
        value = html.unescape(content.strip())
        if value:
            stack[-1].values.setdefault(tag, value)
        else:
            if len(stack) > MAX_DEPTH:
                raise ImportProblem('The OFX file is nested too deeply.')
            node = _Node(tag)
            stack[-1].children.append(node)
            stack.append(node)
    return root


def _xml(text: str) -> _Node:
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ImportProblem('The OFX file contains XML features that are not allowed.')
    body = text[text.find('<OFX'):] if '<OFX' in text else text
    try:
        el = SafeET.fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException:
        raise ImportProblem('The OFX file contains XML features that are not allowed.') from None
    except SafeET.ParseError:
        raise ImportProblem('The OFX file is damaged.') from None

    def conv(e: object, depth: int) -> _Node:
        if depth > MAX_DEPTH:
            raise ImportProblem('The OFX file is nested too deeply.')
        node = _Node(str(getattr(e, 'tag', '')).upper())
        for child in list(e):  # type: ignore[call-overload]
            if len(child):
                node.children.append(conv(child, depth + 1))
            else:
                node.values.setdefault(str(child.tag).upper(), (child.text or '').strip())
        return node
    root = _Node('ROOT')
    root.children.append(conv(el, 1))
    return root


def _date(raw: str | None) -> date | None:
    m = re.match(r'\s*(\d{4})(\d{2})(\d{2})', raw or '')
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _amount(raw: str) -> Cents:
    """OFX amounts use a point, but some banks write a decimal comma ("-12,50", "1.234,50")."""
    v = raw.strip().replace(' ', '')
    if ',' in v and ('.' not in v or v.rfind(',') > v.rfind('.')):
        if not re.fullmatch(r'[+-]?\d{1,3}(\.\d{3})*,\d{1,2}|[+-]?\d+,\d{1,2}', v):
            raise ValueError('ambiguous amount')
        v = v.replace('.', '').replace(',', '.')
    elif ',' in v:
        if not re.fullmatch(r'[+-]?\d{1,3}(,\d{3})+(\.\d{1,2})?', v):
            raise ValueError('a comma in an OFX amount')
        v = v.replace(',', '')       # thousands separators: "1,234.50"
    return money.parse(v)


def read_ofx(data: bytes) -> BankFile:
    check_size(data)
    if not data.strip():
        raise ImportProblem('The file has no transactions.')
    head = data[:512].decode('latin-1', 'replace').upper()
    text = data.decode('cp1252', 'replace') if 'CHARSET:1252' in head else decode(data)
    if '<OFX' not in text.upper():
        raise ImportProblem('This is not an OFX or QFX file.')
    is_xml = text.lstrip().startswith('<?xml') or '<?OFX' in text[:1024].upper()
    root = _xml(text) if is_xml else _sgml(text)
    statements = root.iter('STMTRS') + root.iter('CCSTMTRS')
    if len(statements) > 1:
        raise ImportProblem('The file holds more than one account; export one account at a time.')
    txn_nodes = root.iter('STMTTRN')
    if len(txn_nodes) > MAX_ROWS:
        raise ImportProblem('The file has more than %d transactions.' % MAX_ROWS)
    counts: Counter[tuple[date, Cents, str]] = Counter()
    seen: set[str] = set()
    duplicates = 0
    txns: list[BankTxn] = []
    for node in txn_nodes:
        v = node.values
        posted = _date(v.get('DTPOSTED'))
        if posted is None:
            raise ImportProblem('A transaction has no readable DTPOSTED date.')
        try:
            amount = _amount(v.get('TRNAMT', ''))
        except ValueError:
            raise ImportProblem('A transaction has no readable TRNAMT amount.') from None
        desc = mask(' '.join(x for x in (v.get('NAME', ''), v.get('MEMO', '')) if x))
        tid = v.get('FITID', '').strip()
        if not tid:
            counts[(posted, amount, desc)] += 1
            tid = txn_id(posted, amount, desc, counts[(posted, amount, desc)])
        if tid in seen:
            duplicates += 1
            continue
        seen.add(tid)
        txns.append(BankTxn(id=tid, posted=posted, amount=amount, description=desc, category=v.get('TRNTYPE', '')))
    result = finish(txns, duplicates=duplicates)
    ledger = root.iter('LEDGERBAL')
    if not ledger:
        return result
    as_of = _date(ledger[0].values.get('DTASOF'))
    try:
        balance = _amount(ledger[0].values.get('BALAMT', ''))
    except ValueError:
        return result
    if as_of is None or as_of < result.last:
        return result                      # the balance is older than the newest posting: no running balances
    out: list[BankTxn] = []
    running = balance
    for t in result.txns:                  # newest first: each older balance = newer balance - newer amount
        out.append(replace(t, balance=running))
        running -= t.amount
    return replace(result, txns=tuple(out), latest_balance=balance, has_balance=True)
