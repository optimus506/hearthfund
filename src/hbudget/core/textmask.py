"""Masking of account-like numbers in bank text (ARCHITECTURE section 11.4, D-015)."""
from __future__ import annotations

import re

# a digit run, optionally joined to more runs by any short run (1-3 characters) of non-alphanumeric characters
# other than a newline: "4000 1234", "4000-1234", "4000 - 1234", "4000--1234", "4000 | 1234", "4000·1234"
# (the text is whitespace-collapsed first, so there are no newlines; the date placeholder never joins)
# an optional space may also sit on each side of the run: "4000 -- 1234"
_RUN = re.compile(r'\d+(?: ?(?:[^\w\x00]|_){1,3} ?\d+)*')
# dates stay readable: 2029-03-01, 3/1/29, 03/01/2029. They are set aside before numbers are grouped, unless they
# are glued to more digits by a dash or slash (then they may be part of an account number)
_DATE = re.compile(r'(?<![\d/\-])(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))(?![\d/\-])')
_GROUP_MIN = 8   # a joined group with this many digits in total counts as one number
_RUN_MIN = 5     # numbers with this many digits are masked
_KEEP = 4        # the last 4 digits stay visible
_HOLD = '\x00'   # stands in for a date while numbers are masked


def _mask_digits(text: str) -> str:
    """Replace every digit but the last _KEEP with '#', keeping separators."""
    total = sum(c.isdigit() for c in text)
    out, seen = [], 0
    for c in text:
        if c.isdigit():
            seen += 1
            out.append(c if seen > total - _KEEP else '#')
        else:
            out.append(c)
    return ''.join(out)


def _mask_match(m: re.Match[str]) -> str:
    s = m.group()
    if sum(c.isdigit() for c in s) >= _GROUP_MIN and not s.isdigit():
        return _mask_digits(s)
    # not a long group: each plain run on its own
    return re.sub(r'\d{%d,}' % _RUN_MIN, lambda r: _mask_digits(r.group()), s)


def mask(text: str) -> str:
    """Collapse whitespace and hide all but the last 4 digits of every number with 5+ digits.

    "CARD 123456789012" -> "CARD ########9012"; "4000-1234-5678-9010" -> "####-####-####-9010", also with
    spaces, dots, underscores, slashes, commas or plus signs between the groups. Short runs (a "last 4" suffix)
    and dates (2029-03-01, 3/1/29, 03/01/2029) stay as they are.
    """
    text = ' '.join(text.replace(_HOLD, ' ').split())
    dates: list[str] = []

    def hold(m: re.Match[str]) -> str:
        dates.append(m.group())
        return _HOLD

    masked = _RUN.sub(_mask_match, _DATE.sub(hold, text))
    restored = iter(dates)
    return re.sub(_HOLD, lambda _: next(restored), masked)
