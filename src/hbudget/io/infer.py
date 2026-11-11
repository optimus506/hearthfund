"""Series inference for imported rows (ARCHITECTURE 12.3).

Rows are grouped by exact name and the due dates are tested against candidate schedules (weekly, biweekly,
monthly, semimonthly, yearly; for income also with a weekend shift). A schedule fits when every row date is one of
its occurrences and the rows cover at least three quarters of the occurrences between the first and last row.
Inference only labels rows: it never changes a row's amount, date or order.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import date, timedelta
from itertools import combinations, pairwise

from hbudget.core.models import Cadence, Kind, LedgerRow, Schedule, Series, Status, Weekend, slug
from hbudget.core.recurrence import Occurrence, occurrences

__all__ = ['MIN_COVERAGE', 'infer_series']

MIN_COVERAGE = 0.75
_SLACK = timedelta(days=3)   # a weekend shift moves a date by at most two days


def _fit(schedule: Schedule, dates: Sequence[date]) -> dict[date, Occurrence] | None:
    """Occurrence per row date when the schedule explains every date with enough coverage, else None."""
    occs = occurrences(schedule, dates[0] - _SLACK, dates[-1] + _SLACK)
    by_actual = {o.actual: o for o in occs}
    if not all(d in by_actual for d in dates):
        return None
    first, last = by_actual[dates[0]], by_actual[dates[-1]]
    span = [o for o in occs if first.nominal <= o.nominal <= last.nominal]
    if len(set(dates)) < MIN_COVERAGE * len(span):
        return None
    return by_actual


def _start(first: date) -> date:
    return first - _SLACK


def _candidates(dates: Sequence[date], shifts: Sequence[Weekend]) -> Iterable[Schedule]:
    first, last = dates[0], dates[-1]
    gaps = [(b - a).days for a, b in pairwise(dates)]
    if all(g % 7 == 0 for g in gaps):
        yield Schedule(Cadence.WEEKLY, first)
        if all(g % 14 == 0 for g in gaps):
            yield Schedule(Cadence.BIWEEKLY, first)
    days_seen = Counter(d.day for d in dates)
    for weekend in shifts:
        delta = (0,) if weekend is Weekend.NONE else ((1, 2) if weekend is Weekend.BEFORE else (-1, -2))
        candidates: set[int] = {d for d in days_seen if 1 <= d <= 31}
        if weekend is not Weekend.NONE:
            candidates |= {(d.day + k - 1) % 31 + 1 for d in dates for k in delta}
        candidates.add(max(days_seen))
        for day in sorted(candidates, key=lambda x: (-days_seen.get(x, 0), x)):
            yield Schedule(Cadence.MONTHLY, _start(first), day=day, weekend=weekend)
        common = [d for d, _ in days_seen.most_common(6)]
        pool = sorted(set(common) | (candidates if weekend is not Weekend.NONE else set()))[:10]
        for a, b in combinations(pool, 2):
            yield Schedule(Cadence.SEMIMONTHLY, _start(first), days=(a, b), weekend=weekend)
        if len({d.year for d in dates}) == len(dates) and last.year - first.year + 1 == len(dates):
            for d in sorted({(x.month, x.day) for x in dates}):
                yield Schedule(Cadence.YEARLY, _start(first), month=d[0], day=d[1], weekend=weekend)


def _detect(rows: Sequence[LedgerRow]) -> tuple[Schedule, dict[date, Occurrence]] | None:
    dates = sorted(r.due_date for r in rows)
    if len(dates) == 1:
        s = Schedule(Cadence.ONCE, dates[0])
        return s, {dates[0]: Occurrence(dates[0], dates[0], 0)}
    shifts = (Weekend.NONE, Weekend.BEFORE, Weekend.AFTER) if rows[0].kind is Kind.INCOME else (Weekend.NONE,)
    for schedule in _candidates(dates, shifts):
        fit = _fit(schedule, dates)
        if fit is not None:
            return schedule, fit
    return None


def _unique(base: str, used: set[str]) -> str:
    sid = base or 'item'
    n = 1
    while sid in used:
        n += 1
        sid = '%s-%d' % (base or 'item', n)
    used.add(sid)
    return sid


def infer_series(rows: Sequence[LedgerRow], *, horizon_end: date | None = None) -> tuple[list[Series], list[LedgerRow]]:
    """Group rows into series. Returns the series and ALL rows (series rows re-keyed, the rest unchanged).

    `horizon_end` is the import's horizon end (the latest due date in the whole sheet); it is every series'
    watermark and decides whether a series ended part-way through the sheet (it then gets schedule.end).
    """
    if not rows:
        return [], []
    end = horizon_end or max(r.due_date for r in rows)
    groups: dict[str, list[LedgerRow]] = {}
    for r in rows:
        groups.setdefault(r.name, []).append(r)
    used_series: set[str] = set()
    used_rows: set[str] = set()
    series_out: list[Series] = []
    replaced: dict[str, LedgerRow] = {}
    for name, group in groups.items():
        if len({r.kind for r in group}) != 1:
            continue                              # money in and out under one name: keep the rows standalone
        found = _detect(group)
        if found is None:
            continue
        schedule, fit = found
        sid = _unique(slug(name), used_series)
        group = sorted(group, key=lambda r: (r.due_date, r.seq))
        first_nominal = fit[group[0].due_date].nominal
        last_nominal = fit[group[-1].due_date].nominal
        schedule = replace(schedule, start=first_nominal)
        nxt = occurrences(schedule, last_nominal + timedelta(days=1), end)
        if schedule.cadence is not Cadence.ONCE and nxt:
            schedule = replace(schedule, end=last_nominal)
        amounts = Counter(abs(r.amount) for r in group)
        amount = amounts.most_common(1)[0][0]
        statuses = {r.status_override for r in group}
        all_autopay = statuses == {Status.AUTOPAY}
        cards = {r.card for r in group}
        series_out.append(Series(
            id=sid, name=name, kind=group[0].kind, amount=amount, schedule=schedule,
            status=Status.AUTOPAY if all_autopay else Status.NONE,
            card=cards.pop() if len(cards) == 1 else None, generated_through=end))
        for r in group:
            nominal = fit[r.due_date].nominal
            rid = '%s:%s' % (sid, nominal.isoformat())
            if rid in used_rows:
                rid = '%s:r%d' % (rid, r.seq)
            used_rows.add(rid)
            replaced[r.id] = replace(
                r, id=rid, series_id=sid, nominal_date=nominal, overridden=abs(r.amount) != amount,
                status_override=None if all_autopay else r.status_override)
    return series_out, [replaced.get(r.id, r) for r in rows]
