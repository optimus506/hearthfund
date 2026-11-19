"""Labels (D-104): a few short words on a recurring item or on one row, shown as chips ("blue card", "paid early").

Free text, so a household names things its own way. A row shows its item's labels and then its own. Labels say
something to the people reading the list; nothing in the forecast or the alerts depends on them."""
from __future__ import annotations

from collections.abc import Iterable

from .models import LedgerRow, Series

__all__ = ['MAX_CHARS', 'MAX_LABELS', 'clean', 'in_use', 'shown']

MAX_LABELS = 5
MAX_CHARS = 24


def clean(values: Iterable[str]) -> tuple[str, ...]:
    """Trimmed, single-spaced, without commas, no label twice (whatever its capitals), in the order given.
    ValueError when one is too long or there are too many: the caller says so, nothing is cut silently."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        text = ' '.join(str(v).replace(',', ' ').split())
        if not text or text.casefold() in seen:
            continue
        if len(text) > MAX_CHARS:
            raise ValueError('A label has at most %d characters: "%s" is longer.' % (MAX_CHARS, text))
        seen.add(text.casefold())
        out.append(text)
    if len(out) > MAX_LABELS:
        raise ValueError('At most %d labels.' % MAX_LABELS)
    return tuple(out)


def shown(row: LedgerRow, series_by_id: dict[str, Series]) -> tuple[str, ...]:
    """The labels a row shows: its recurring item's, then its own (one that repeats the item's is not shown twice)."""
    s = series_by_id.get(row.series_id) if row.series_id else None
    out = list(s.labels) if s else []
    have = {x.casefold() for x in out}
    out.extend(x for x in row.labels if x.casefold() not in have)
    return tuple(out)


def in_use(series: Iterable[Series], rows: Iterable[LedgerRow]) -> tuple[str, ...]:
    """Every label of the budget, once, sorted: what the forms offer to pick from."""
    found: dict[str, str] = {}
    for labels in (*(s.labels for s in series), *(r.labels for r in rows)):
        for label in labels:
            found.setdefault(label.casefold(), label)
    return tuple(sorted(found.values(), key=str.casefold))
