"""Bills page listing: removed rows dated on or before the last reconciliation drop off; ?removed=1 lists
every removed row so each can still be undone."""
from __future__ import annotations

from dataclasses import replace

from factories import D, budget, row
from hbudget.core import forecast as fc
from hbudget.core.models import RowState
from hbudget.web import views

SINCE = D(2029, 3, 10)


def _budget(since=SINCE):
    rows = (row('old', 'Water', 5000, D(2029, 3, 1), state=RowState.REMOVED),
            row('edge', 'Gym', 4000, D(2029, 3, 10), state=RowState.REMOVED),
            row('next', 'Insurance', 9000, D(2029, 4, 15), state=RowState.REMOVED),
            row('open', 'Rent', 100000, D(2029, 3, 20)))
    return replace(budget(as_of=D(2029, 3, 11), rows=rows), last_reconciled_through=since)


def ids(b, **kw):
    out = views.ledger(b, fc.compute(b, today=b.as_of), today=b.as_of, **kw)
    return [r['id'] for r in out['rows']], out['removed_total']


def test_removed_rows_up_to_the_last_reconciliation_drop_off():
    assert ids(_budget()) == (['open', 'next'], 3)


def test_removed_asks_for_every_removed_row():
    assert ids(_budget(), removed=True) == (['old', 'edge', 'open', 'next'], 3)


def test_never_reconciled_keeps_every_removed_row():
    assert ids(_budget(since=None)) == (['old', 'edge', 'open', 'next'], 3)


def test_ledger_route_counts_and_lists_removed_rows(admin):
    r = admin.api('POST', '/series', {'name': 'Electric', 'kind': 'bill', 'amount': 15000,
                                      'schedule': {'cadence': 'monthly', 'start': '2029-03-01', 'day': 6}})
    assert r.status_code == 200
    rid = admin.api('GET', '/ledger').json()['rows'][0]['id']
    assert admin.api('POST', '/rows/%s/remove' % rid, {}).status_code == 200
    plain, every = admin.api('GET', '/ledger').json(), admin.api('GET', '/ledger?removed=1').json()
    assert plain['removed_total'] == every['removed_total'] == 1
    assert rid in [x['id'] for x in every['rows']]
