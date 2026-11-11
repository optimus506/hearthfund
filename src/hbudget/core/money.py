"""Money as integer cents, and percentages as basis points (ARCHITECTURE section 3)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

Cents = int

_HUNDRED = Decimal(100)


def _decimal(value: object) -> Decimal:
    """Exact decimal for an int, float, Decimal or numeric string (floats go through repr, never through * 100)."""
    if isinstance(value, bool):
        raise TypeError('a bool is not an amount')
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            raise ValueError('not a number: %r' % value) from None
    raise TypeError('not an amount: %r' % type(value).__name__)


def _two_places(d: Decimal, text: object) -> int:
    if not d.is_finite():
        raise ValueError('not a finite number: %r' % text)
    scaled = d * _HUNDRED
    if scaled != scaled.to_integral_value():
        raise ValueError('more than two decimals: %r' % text)
    return int(scaled)


def parse(value: str | int | float | Decimal) -> Cents:
    """Dollars to cents. Accepts 1800, 1800.5, "1,800.50", "$1,800.50", "(1,800.50)" and "-1800.50"."""
    if isinstance(value, str):
        s = value.strip().replace(',', '').replace(' ', '')
        negative = False
        if s.startswith('(') and s.endswith(')'):
            negative, s = True, s[1:-1]
        if s.startswith(('-', '+')):
            negative, s = negative ^ (s[0] == '-'), s[1:]
        if s.startswith('$'):
            s = s[1:]
        if s.startswith(('-', '+')):            # "$-12.00"
            negative, s = negative ^ (s[0] == '-'), s[1:]
        if not s or not all(c.isdigit() or c == '.' for c in s) or s.count('.') > 1:
            raise ValueError('not an amount: %r' % value)
        cents = _two_places(Decimal(s), value)
        return -cents if negative else cents
    return _two_places(_decimal(value), value)


def fmt(cents: Cents, *, sign: Literal['minus', 'parens'] = 'minus') -> str:
    """"$1,800.50", "-$1,800.50" or "($1,800.50)"."""
    body = '$%s.%02d' % (format(abs(cents) // 100, ','), abs(cents) % 100)
    if cents >= 0:
        return body
    return '(%s)' % body if sign == 'parens' else '-' + body


def percent_to_bp(value: str | int | float | Decimal) -> int:
    """Percent to basis points: 24.99 -> 2499, 10 -> 1000. More than two decimals raises ValueError."""
    if isinstance(value, str):
        value = value.strip().rstrip('%').strip()
    return _two_places(_decimal(value), value)


def bp_to_percent(bp: int) -> str:
    """Basis points as a percent string without trailing zeros: 2499 -> "24.99", 1000 -> "10"."""
    d = Decimal(bp) / _HUNDRED
    text = format(d, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text
