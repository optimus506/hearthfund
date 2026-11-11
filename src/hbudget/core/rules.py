"""Payee rules: which rule a bank posting belongs to (ARCHITECTURE section 9.3, D-032)."""
from __future__ import annotations

import re
import re._parser as _parser  # type: ignore[import-not-found]  # the stdlib regex parser (no stubs)
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from .models import BankTxn, Direction, PayeeRule
from .money import Cents

__all__ = ['MAX_COST', 'MAX_PATTERN', 'MAX_TEXT', 'classify', 'compile_safe', 'matches', 'pattern_cost',
           'safe_search', 'test']

MAX_PATTERN = 200     # absolute limit on the pattern length
MAX_TEXT = 500        # every matcher cuts the text to this many characters first
MAX_REPEAT = 100      # no repeat count above this
MAX_COST = 5_000      # worst-case backtracking budget per start position (see pattern_cost)
_REPEAT = re.compile(r'\{(\d*)(,?)(\d*)\}')


def _quantifier_at(p: str, i: int) -> tuple[int, bool] | None:
    """If a quantifier starts at p[i], return (its length, whether it repeats more than once)."""
    if i >= len(p):
        return None
    c = p[i]
    if c in '*+':
        return 1, True
    if c == '?':
        return 1, False
    if c == '{':
        m = _REPEAT.match(p, i)
        if m and (m.group(1) or m.group(3)):
            lo, comma, hi = m.group(1), m.group(2), m.group(3)
            top = int(hi) if hi else (None if comma else int(lo or 0))
            if (top is not None and top > MAX_REPEAT) or (lo and int(lo) > MAX_REPEAT):
                raise ValueError('repeat counts above %d are not allowed' % MAX_REPEAT)
            return len(m.group()), top is None or top > 1
    return None


def _check(p: str) -> None:
    if len(p) > MAX_PATTERN:
        raise ValueError('patterns are limited to %d characters' % MAX_PATTERN)
    # stack of [has_quantifier, has_alternation] per open group
    stack: list[list[bool]] = [[False, False]]
    i = 0
    while i < len(p):
        c = p[i]
        if c == '\\':
            nxt = p[i + 1:i + 2]
            if (nxt.isdigit() and nxt != '0') or nxt in ('g', 'k'):
                raise ValueError('backreferences are not allowed')
            i += 2
            continue
        if c == '[':                       # character class: skip to its end
            j = i + 1
            if p[j:j + 1] == '^':
                j += 1
            if p[j:j + 1] == ']':
                j += 1
            while j < len(p) and p[j] != ']':
                j += 2 if p[j] == '\\' else 1
            i = j + 1
            continue
        if c == '(':
            ext = p[i + 1:i + 4]
            if ext.startswith(('?=', '?!', '?<=', '?<!')):
                raise ValueError('lookarounds are not allowed')
            if ext.startswith('?>'):
                raise ValueError('atomic groups are not allowed')
            if ext.startswith('?P='):
                raise ValueError('backreferences are not allowed')
            if ext.startswith('?(') :
                raise ValueError('conditional groups are not allowed')
            stack.append([False, False])
            i += 1
            continue
        if c == ')':
            if len(stack) == 1:
                raise ValueError('unbalanced parenthesis')
            inner_q, inner_alt = stack.pop()
            q = _quantifier_at(p, i + 1)
            if q and q[1] and (inner_q or inner_alt):
                raise ValueError('a repeated group may not contain another repeat or an alternation')
            if q:
                stack[-1][0] = True
                i += q[0]
                if p[i + 1:i + 2] == '+':
                    raise ValueError('possessive quantifiers are not allowed')
            else:
                stack[-1][0] = stack[-1][0] or inner_q
            stack[-1][1] = stack[-1][1] or inner_alt
            i += 1
            continue
        if c == '|':
            stack[-1][1] = True
            i += 1
            continue
        q = _quantifier_at(p, i)
        if q:
            stack[-1][0] = True
            if p[i + q[0]:i + q[0] + 1] == '+':
                raise ValueError('possessive quantifiers are not allowed')
            i += q[0]
            continue
        i += 1
    if len(stack) != 1:
        raise ValueError('unbalanced parenthesis')


MAXREPEAT = _parser.MAXREPEAT
_OPEN = MAX_TEXT + 1   # branching of *, + and {n,}: it can stop at any of the MAX_TEXT + 1 positions


def _capped_pow(base: int, n: int) -> int:
    out = 1
    for _ in range(n):
        out = min(out * base, MAX_COST + 1)
        if out > MAX_COST:
            break
    return out


def _cost(items: Any) -> int:
    """Worst-case branching of a parsed (sub)pattern: the product of every choice point, capped above MAX_COST."""
    total = 1
    for op, av in items:
        name = str(op)
        if name in ('MAX_REPEAT', 'MIN_REPEAT'):
            lo, hi, sub = av
            inner = _cost(sub)
            if hi == MAXREPEAT:
                factor = _OPEN * inner
            elif hi == lo:
                factor = _capped_pow(inner, lo)
            else:
                factor = (hi - lo + 1) * _capped_pow(inner, hi)
        elif name == 'SUBPATTERN':
            factor = _cost(av[-1])
        elif name in ('BRANCH',):
            factor = sum(_cost(branch) for branch in av[1])
        elif name in ('ASSERT', 'ASSERT_NOT', 'ATOMIC_GROUP', 'POSSESSIVE_REPEAT', 'GROUPREF', 'GROUPREF_EXISTS'):
            raise ValueError('this regex construct is not allowed')
        else:
            factor = 1
        total = min(total * factor, MAX_COST + 1)
    return total


def pattern_cost(pattern: str) -> int:
    """Worst-case cost of a pattern: ? = 2, {m,n} = n - m + 1, *, + and {n,} = MAX_TEXT + 1; alternatives add up,
    nested repeats and sequences multiply. Values above MAX_COST are reported as MAX_COST + 1."""
    try:
        parsed = _parser.parse(pattern, re.IGNORECASE)
    except re.error as e:
        raise ValueError('not a valid pattern: %s' % e) from None
    return _cost(parsed)


@lru_cache(maxsize=512)
def compile_safe(pattern: str) -> re.Pattern[str]:
    """Compile a user regex (case-insensitive) after checking it against the restricted syntax (D-032) and the
    worst-case cost budget (D-046)."""
    _check(pattern)
    if pattern_cost(pattern) > MAX_COST:
        raise ValueError('this pattern could take too long to match: use fewer optional or repeated parts')
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ValueError('not a valid pattern: %s' % e) from None


def _text(description: str) -> str:
    return ' '.join(description.upper().split())[:MAX_TEXT]


def safe_search(pattern: str, text: str) -> re.Match[str] | None:
    """Search a user pattern (checked by compile_safe) in text cut to MAX_TEXT characters. For the io layer."""
    return compile_safe(pattern).search(text[:MAX_TEXT])


def matches(rule: PayeeRule, description: str, amount: Cents) -> bool:
    """Does this rule match a posting (bank sign: - out, + in)?"""
    text = _text(description)
    if not rule.contains and not rule.regex:
        return False
    if rule.contains and not any(' '.join(k.upper().split()) in text for k in rule.contains):
        return False
    if rule.regex and not compile_safe(rule.regex).search(text):
        return False
    if (rule.direction is Direction.IN and amount <= 0) or (rule.direction is Direction.OUT and amount >= 0):
        return False
    size = abs(amount)
    if rule.min_amount is not None and size < rule.min_amount:
        return False
    return not (rule.max_amount is not None and size > rule.max_amount)


def classify(t: BankTxn, rules: Sequence[PayeeRule]) -> PayeeRule | None:
    """The first rule that matches the posting, or None."""
    for rule in rules:
        if matches(rule, t.description, t.amount):
            return rule
    return None


def test(rules: Sequence[PayeeRule], description: str, amount: Cents) -> list[tuple[PayeeRule, bool]]:
    """Every rule with whether it matches; the first True is the one classify() picks."""
    return [(r, matches(r, description, amount)) for r in rules]
