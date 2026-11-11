from dataclasses import replace

import pytest
from recon_fixture import export, household

from factories import D, budget, chain, txn
from hbudget.core.reconcile import ReconcileError, prepare


def code(fn):
    with pytest.raises(ReconcileError) as e:
        fn()
    return e.value.code


def test_basis_new_and_latest():
    p = prepare(export(), household())
    assert p.basis.id == 't1' and not p.basis_ambiguous and p.balance_checked
    assert [t.id for t in p.new] == ['t%d' % i for i in range(2, 12)]
    assert p.latest_balance == 263400 and p.through == D(2029, 3, 16)
    assert p.txns[0].id == 't11'


def test_oldest_first_input_gives_the_same_result():
    assert prepare(list(reversed(export())), household()) == prepare(export(), household())


def test_empty():
    assert code(lambda: prepare([], household())) == 'empty'


def test_chain_break_when_a_row_is_dropped():
    rows = export()
    del rows[4]
    with pytest.raises(ReconcileError) as e:
        prepare(rows, household())
    assert e.value.code == 'chain_break' and 'missing from the export' in str(e.value)


def test_basis_not_found():
    assert code(lambda: prepare(export(), replace(household(), start_balance=123))) == 'no_basis'


def test_basis_ambiguous_uses_the_newest():
    rows = chain(1000, [('a', D(2029, 3, 1), 500, 'X'), ('b', D(2029, 3, 2), -500, 'Y'), ('c', D(2029, 3, 3), 500, 'Z'),
                        ('d', D(2029, 3, 4), -500, 'W'), ('e', D(2029, 3, 5), 100, 'V')])
    p = prepare(rows, budget(start=1000))
    assert p.basis.id == 'd' and p.basis_ambiguous and [t.id for t in p.new] == ['e']


def test_nothing_new():
    rows = export()
    assert code(lambda: prepare(rows, replace(household(), start_balance=rows[0].balance))) == 'nothing_new'


def test_same_day_postings_follow_file_order():
    day = D(2029, 3, 1)
    rows = chain(1000, [('a', day, -100, 'A'), ('b', day, -200, 'B'), ('c', day, 50, 'C')])
    p = prepare(rows, budget(start=1000))
    assert [t.id for t in p.new] == ['a', 'b', 'c']
    swapped = [replace(t, file_index=2 - t.file_index) for t in rows]   # file order reversed: the chain breaks
    assert code(lambda: prepare(swapped, budget(start=1000))) == 'chain_break'


def test_overlapping_upload_finds_its_basis():
    # the newer upload repeats two postings of the older one; the basis is inside the overlap
    older = chain(5000, [('o1', D(2029, 3, 1), -1000, 'A'), ('o2', D(2029, 3, 2), -1000, 'B')])
    newer = chain(4000, [('o2', D(2029, 3, 2), -1000, 'B'), ('n1', D(2029, 3, 4), -500, 'C')])
    b = budget(start=older[0].balance)
    p = prepare(newer, b)
    assert p.basis.id == 'o2' and [t.id for t in p.new] == ['n1'] and p.latest_balance == 2500


def test_no_balance_column_fallback():
    rows = [txn('a', D(2029, 3, 10), -100, 'A', i=2), txn('b', D(2029, 3, 10), -200, 'B', i=1),
            txn('c', D(2029, 3, 12), 50, 'C', i=0), txn('z', D(2029, 3, 9), -999, 'Z', i=3)]
    b = replace(budget(start=1000), last_reconciled_through=D(2029, 3, 10))
    p = prepare(rows, b, seen_ids=frozenset({'a'}), ignored=2)
    # 'b' posted later on the day of the last reconciliation is not lost; 'a' was already counted
    assert [t.id for t in p.new] == ['b', 'c'] and p.basis is None and not p.balance_checked
    assert p.latest_balance == 850 and p.ignored == 2
    assert code(lambda: prepare(rows, b, seen_ids=frozenset({'a', 'b', 'c'}))) == 'nothing_new'
    assert len(prepare(rows, budget(start=0)).new) == 4          # never reconciled: everything is new
