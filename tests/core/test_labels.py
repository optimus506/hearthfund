"""Labels and items with nothing to schedule (D-104)."""
from dataclasses import replace

import pytest

from factories import D, budget, row, schedule, series
from hbudget.core import alerts, labels
from hbudget.core.forecast import compute

TODAY = D(2029, 3, 1)


def test_clean_trims_dedupes_and_refuses_what_is_too_much():
    assert labels.clean(['  blue   card ', 'Blue Card', '', 'paid, early']) == ('blue card', 'paid early')
    assert labels.clean([]) == ()
    with pytest.raises(ValueError, match='at most 24'):
        labels.clean(['x' * 25])
    with pytest.raises(ValueError, match='At most 5'):
        labels.clean('abcdef')


def test_a_row_shows_its_items_labels_then_its_own():
    s = series('s', 'bill', 5000, schedule('monthly', D(2029, 1, 3)), labels=('blue card',))
    by_id = {'s': s}
    r = row('r1', 'Allowance', 5000, D(2029, 3, 3), series_id='s', labels=('paid early', 'Blue Card'))
    assert labels.shown(r, by_id) == ('blue card', 'paid early')
    assert labels.shown(row('r2', 'One-off', 100, D(2029, 3, 3), labels=('once',)), by_id) == ('once',)
    assert labels.in_use([s], [replace(r, labels=('paid early', 'BLUE CARD'))]) == ('blue card', 'paid early')


def test_an_item_with_nothing_to_schedule_stays_out_of_the_alert():
    s = series('s', 'bill', 5000, schedule('monthly', D(2029, 1, 3)))
    b = budget(series=(s,), rows=(row('r1', 'Allowance', 5000, D(2029, 3, 3), series_id='s'),
                                  row('r2', 'One-off', 100, D(2029, 3, 4))))
    assert [r.id for r in alerts.unscheduled(b, today=TODAY)] == ['r1', 'r2']
    quiet = replace(b, series=(replace(s, no_schedule=True),))
    assert [r.id for r in alerts.unscheduled(quiet, today=TODAY)] == ['r2']
    found = alerts.evaluate(quiet, compute(quiet, today=TODAY), today=TODAY)
    assert [a.row_id for a in found if a.code == 'unscheduled'] == ['r2']
