from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from dbfactory import PW, TODAY, household, txn

from hbudget.core.ledger import change_amount, ordered
from hbudget.core.models import (
    AlertSettings,
    CapRule,
    CardTxn,
    ExpectedItem,
    IncomeAlert,
    Kind,
    ReconcileSettings,
    Settings,
    Status,
)
from hbudget.db import repo, users
from hbudget.io import ImportProblem
from hbudget.io.bank import BankMapping, MappingProfile
from hbudget.io.workbook_import import WorkbookMapping

NOW = datetime(2029, 1, 5, 12, 0, tzinfo=UTC)


def test_households_and_members(conn: sqlite3.Connection) -> None:
    _, hid = household(conn)
    other = users.create_user(conn, 'partner', PW)
    repo.add_member(conn, hid, other, 'contributor')
    assert repo.get_role(conn, other, hid) == 'contributor'
    repo.set_role(conn, hid, other, 'viewer')
    repo.add_member(conn, hid, other, 'editor')                     # adding again updates the role
    assert [(m.username, m.role) for m in repo.list_members(conn, hid)] == [('admin', 'owner'), ('partner', 'editor')]
    assert [(h.name, r) for h, r in repo.households_for_user(conn, other)] == [('Example household', 'editor')]
    repo.remove_member(conn, hid, other)
    assert repo.get_role(conn, other, hid) is None
    with pytest.raises(ValueError):
        repo.add_member(conn, hid, other, 'boss')
    for fn in (lambda: repo.set_role(conn, hid, other, 'viewer'), lambda: repo.set_role(conn, hid, other, 'owner'),
               lambda: repo.remove_member(conn, hid, other),
               lambda: repo.rename_household(conn, 999, 'x'), lambda: repo.delete_household(conn, 999)):
        with pytest.raises(KeyError):
            fn()
    second = repo.create_household(conn, '  ', currency='EUR', as_of=date(2029, 2, 1), now=NOW)
    h = repo.get_household(conn, second)
    assert h is not None and h.name == 'Household' and h.currency == 'EUR'
    repo.rename_household(conn, second, 'Cabin')
    assert [x.name for x in repo.list_households(conn)] == ['Example household', 'Cabin']
    assert repo.load_budget(conn, second).as_of == date(2029, 2, 1)
    repo.delete_household(conn, second)
    assert repo.get_household(conn, second) is None
    with pytest.raises(KeyError):
        repo.load_budget(conn, second)


def test_save_budget_is_a_diff_and_audited(conn: sqlite3.Connection) -> None:
    uid, hid = household(conn)
    b = repo.load_budget(conn, hid)
    rows = [r for r in ordered(b.rows) if r.kind is Kind.BILL]   # a bill row, whatever the example's order
    b2 = change_amount(b, rows[0].id, rows[0].amount + 100)
    counts = repo.save_budget(conn, hid, b2, user_id=uid, action='row.change', row_id=rows[0].id, now=NOW)
    assert counts['ledger_rows'] == {'added': 0, 'changed': 1, 'removed': 0}
    assert counts['series'] == {'added': 0, 'changed': 0, 'removed': 0}
    entry = repo.list_audit(conn, hid, limit=1)[0]
    assert entry['action'] == 'row.change' and entry['row'] == rows[0].id and entry['username'] == 'admin'
    assert entry['detail'] == {'ledger_rows': {'added': 0, 'changed': 1, 'removed': 0}}
    loaded = repo.load_budget(conn, hid)
    assert ordered(loaded.rows) == ordered(b2.rows) and replace(loaded, rows=b2.rows) == b2
    fewer = replace(b2, rules=b2.rules[1:], series=b2.series[:-1])
    counts = repo.save_budget(conn, hid, fewer, user_id=uid, action='x')
    assert counts['payee_rules']['removed'] == 1 and counts['series']['removed'] == 1
    with pytest.raises(ValueError, match='duplicate'):
        repo.save_budget(conn, hid, replace(b2, rules=b2.rules + b2.rules[:1]), user_id=uid, action='x')
    with pytest.raises(KeyError):
        repo.save_budget(conn, 999, b2, user_id=uid, action='x')


def test_settings_watermarks_marks_and_paid_fields_round_trip(conn: sqlite3.Connection) -> None:
    uid, hid = household(conn)
    b = repo.load_budget(conn, hid)
    settings = Settings(currency='EUR', week_starts=6,
                        reconcile=ReconcileSettings(days_before=5, days_after=9, tolerance_bp=500, tolerance_abs=1000),
                        alerts=AlertSettings(cushion=50000, income=(IncomeAlert('pay-a', 9000, 3, 2500),),
                                             caps=(CapRule('c', 'Cap', ('online-shopping',), 'month', 20000),)),
                        status_colours=((Status.AUTOPAY, '#00aa00'), (Status.MANUAL, '#aa0000')))
    r = ordered(b.rows)[1]
    marked = replace(r, status_override=Status.MANUAL, paid=500, paid_txn_ids=('t1', 't2'), paid_at_change=100,
                     amount_was=r.amount + 1, overridden=True, note='n', cleared_on=TODAY, cleared_txn_id='t3')
    b2 = replace(b, settings=settings, rows=tuple(marked if x.id == r.id else x for x in b.rows),
                 expected=(ExpectedItem('e1', 'Refund', 3000, 'in', TODAY),),
                 series=tuple(replace(s, generated_through=date(2030, 1, 1)) for s in b.series),
                 last_reconciled_through=TODAY)
    repo.save_budget(conn, hid, b2, user_id=uid, action='x')
    loaded = repo.load_budget(conn, hid)
    assert loaded.settings == settings and loaded.expected == b2.expected and loaded.series == b2.series
    assert next(x for x in loaded.rows if x.id == r.id) == marked
    assert loaded.last_reconciled_through == TODAY
    repo.set_expected_status(conn, hid, 'e1', 'done', user_id=uid)
    assert repo.load_budget(conn, hid).expected[0].status == 'done'
    with pytest.raises(ValueError):
        repo.set_expected_status(conn, hid, 'e1', 'maybe', user_id=uid)  # type: ignore[arg-type]


def test_household_scoping(conn: sqlite3.Connection) -> None:
    uid_a, a = household(conn, 'alice')
    uid_b, b = household(conn, 'bob')
    repo.create_upload(conn, a, upload_id='u1', kind='bank', filename='a.csv', uploaded_by='alice', rows=1,
                       date_from=TODAY, date_to=TODAY, latest_balance=100, has_balance=True)
    repo.insert_txns(conn, a, 'u1', [txn('t1', 5, -100, 100, 0)])
    repo.insert_card_txns(conn, a, 'u1', [CardTxn('k1', TODAY, TODAY, -100, 'CAFE', 'Other')])
    repo.save_profile(conn, a, MappingProfile('p1', 'Mine', 'bank', BankMapping('Date', amount_col='Amount')))
    with_item = replace(repo.load_budget(conn, a), expected=(ExpectedItem('e1', 'X', 100, 'out', TODAY),))
    repo.save_budget(conn, a, with_item, user_id=uid_a, action='x')
    repo.save_reconciliation(conn, a, rec_id='r1', by_user='alice', upload_id='u1', through=TODAY, new_balance=100,
                             result={}, report_md='#')
    repo.save_notes(conn, a, 'alice notes', user_id=uid_a)
    a_budget = repo.load_budget(conn, a)
    a_extras = repo.load_extras(conn, a)
    a_audit = repo.list_audit(conn, a, limit=10_000)
    # nothing of A is visible through B
    assert repo.get_upload(conn, b, 'u1') is None and repo.list_uploads(conn, b, include_hidden=True) == []
    assert repo.upload_txns(conn, b, 'u1') == [] and repo.list_history(conn, b) == []
    assert repo.list_card_txns(conn, b) == [] and repo.seen_txn_ids(conn, b) == frozenset()
    assert repo.get_profile(conn, b, 'p1') is None and repo.list_profiles(conn, b) == []
    assert repo.get_reconciliation(conn, b, 'r1') is None and repo.list_reconciliations(conn, b) == []
    assert repo.get_role(conn, uid_b, a) is None and repo.load_notes(conn, b) == ''
    b_extras = repo.load_extras(conn, b)
    assert b_extras.bank_txns == () and b_extras.card_txns == () and b_extras.uploads == ()
    assert b_extras.mapping_profiles == () and dict(b_extras.members) == {'bob': 'owner'}
    assert all(e['by'] != 'alice' for e in b_extras.audit)
    assert {x['id'] for x in repo.list_audit(conn, b, limit=10_000)}.isdisjoint({x['id'] for x in a_audit})
    assert repo.load_budget(conn, b).expected == ()
    for fn in (lambda: repo.set_expected_status(conn, b, 'e1', 'done', user_id=uid_b),
               lambda: repo.set_upload_status(conn, b, 'u1', 'imported'), lambda: repo.delete_upload(conn, b, 'u1'),
               lambda: repo.delete_profile(conn, b, 'p1')):
        with pytest.raises(KeyError):
            fn()
    # writes through B with A's ids land in B only (or fail), never touching A
    with pytest.raises(sqlite3.IntegrityError):                   # B has no upload u1: the link is refused
        repo.insert_txns(conn, b, 'u1', [txn('t1', 5, -999, 1, 0)])
    stolen = replace(a_budget.rows[0], amount=1)
    b_budget = repo.load_budget(conn, b)
    assert any(r.id == stolen.id for r in b_budget.rows)                # same example: B has a row with that id
    repo.save_budget(conn, b, replace(b_budget, rows=tuple(stolen if r.id == stolen.id else r for r in b_budget.rows),
                                      accounts=(replace(a_budget.accounts[0], name='B copy'), *b_budget.accounts[1:])),
                     user_id=uid_b, action='x')
    repo.save_profile(conn, b, MappingProfile('p1', 'Same id, other household', 'card', BankMapping('Date')))
    repo.wipe_household(conn, b, user_id=uid_b)
    assert repo.load_budget(conn, a) == a_budget and repo.load_extras(conn, a) == a_extras
    assert repo.get_profile(conn, a, 'p1').name == 'Mine'  # type: ignore[union-attr]
    assert [x['id'] for x in repo.list_audit(conn, a, limit=10_000)] == [x['id'] for x in a_audit]


def test_overlapping_uploads_are_deduplicated_but_keep_their_own_order(conn: sqlite3.Connection) -> None:
    _, hid = household(conn)
    for uid in ('u1', 'u2'):
        repo.create_upload(conn, hid, upload_id=uid, kind='bank', filename=uid + '.csv', uploaded_by=None, rows=3,
                           date_from=None, date_to=None, latest_balance=None, has_balance=True, now=NOW)
    first = [txn('t3', 3, -10, 970, 0), txn('t2', 2, -10, 980, 1), txn('t1', 1, -20, 990, 2)]
    second = [txn('t5', 5, -10, 950, 0), txn('t4', 4, -10, 960, 1), txn('t3', 3, -10, 970, 2)]
    assert repo.insert_txns(conn, hid, 'u1', first) == (3, 0)
    assert repo.insert_txns(conn, hid, 'u2', second) == (2, 1)
    assert repo.insert_txns(conn, hid, 'u2', second) == (0, 3)            # re-running is harmless
    assert [t.id for t in repo.list_history(conn, hid)] == ['t1', 't2', 't3', 't4', 't5']
    u2 = repo.upload_txns(conn, hid, 'u2')
    assert [(t.id, t.file_index) for t in u2] == [('t5', 0), ('t4', 1), ('t3', 2)]   # t3 is part of the newer set too
    assert [t.file_index for t in repo.upload_txns(conn, hid, 'u1')] == [0, 1, 2]
    repo.save_reconciliation(conn, hid, rec_id='r', by_user=None, upload_id='u1', through=date(2029, 1, 3),
                             new_balance=970, result={'cleared': [], 'accepted': []}, report_md='')
    assert repo.seen_txn_ids(conn, hid) == {'t1', 't2', 't3'}
    rec = repo.get_reconciliation(conn, hid, 'r')
    assert rec is not None and rec.new_balance == 970 and rec.result == {'cleared': [], 'accepted': []}
    # u1 was reconciled: deleting it only hides it, its links and transactions stay (D-028)
    assert repo.delete_upload(conn, hid, 'u1') == 'hidden'
    assert [u.id for u in repo.list_uploads(conn, hid)] == ['u2'] and repo.get_upload(conn, hid, 'u1').hidden  # type: ignore[union-attr]
    assert len(repo.upload_txns(conn, hid, 'u1')) == 3 and len(repo.list_history(conn, hid)) == 5
    assert {u.id for u in repo.list_uploads(conn, hid, include_hidden=True)} == {'u1', 'u2'}


def _uploads(conn: sqlite3.Connection, hid: int, *ids: str, kind: str = 'bank') -> None:
    for uid in ids:
        repo.create_upload(conn, hid, upload_id=uid, kind=kind, filename=uid + '.csv', uploaded_by=None, rows=3,  # type: ignore[arg-type]
                           date_from=None, date_to=None, latest_balance=None, has_balance=True, now=NOW)


def test_the_same_posting_under_a_bank_id_and_a_content_id_is_stored_once(conn: sqlite3.Connection) -> None:
    """#87: one export carries the bank's ids, an overlapping one (mapped without its ID column) content ids."""
    _, hid = household(conn)
    _uploads(conn, hid, 'u1', 'u2')
    first = [txn('B3', 3, -10, 970, 0, 'COFFEE'), txn('B2', 3, -10, 980, 1, 'COFFEE'), txn('B1', 1, -20, 990, 2)]
    second = [txn('h:5', 5, -10, 950, 0), txn('h:3b', 3, -10, 960, 1, 'coffee '), txn('h:3a', 3, -10, 970, 2, 'Coffee'),
              txn('h:1', 1, -20, 990, 3)]
    assert repo.insert_txns(conn, hid, 'u1', first) == (3, 0)
    assert repo.insert_txns(conn, hid, 'u2', second) == (1, 3)           # only the 5th is new
    assert sorted(t.id for t in repo.list_history(conn, hid)) == ['B1', 'B2', 'B3', 'h:5']
    assert [t.id for t in repo.upload_txns(conn, hid, 'u2')] == ['h:5', 'B2', 'B3', 'B1']
    # a third coffee that day is a real, new posting
    _uploads(conn, hid, 'u3')
    assert repo.insert_txns(conn, hid, 'u3', [txn('h:x', 3, -10, 0, 0, 'COFFEE'), txn('h:y', 3, -10, 0, 1, 'COFFEE'),
                                              txn('h:z', 3, -10, 0, 2, 'COFFEE')]) == (1, 2)
    # two files with bank ids never merge different ids
    _uploads(conn, hid, 'u4')
    assert repo.insert_txns(conn, hid, 'u4', [txn('B9', 1, -20, 990, 0)]) == (1, 0)


def test_card_purchases_under_both_kinds_of_id_are_stored_once(conn: sqlite3.Connection) -> None:
    _, hid = household(conn)
    _uploads(conn, hid, 'c1', 'c2', kind='card')
    d = date(2029, 1, 2)
    assert repo.insert_card_txns(conn, hid, 'c1', [CardTxn('K1', d, d, -500, 'CAFE', 'Other')]) == (1, 0)
    assert repo.insert_card_txns(conn, hid, 'c2', [CardTxn('h:k', d, d, -500, 'CAFE', 'Other')]) == (0, 1)
    assert [t.id for t in repo.list_card_txns(conn, hid)] == ['K1']


def test_merge_duplicates_stored_before_the_fix(conn: sqlite3.Connection) -> None:
    _, hid = household(conn)
    _uploads(conn, hid, 'u1', 'u2')
    conn.execute('INSERT INTO bank_txns (household_id, id, first_upload_id, posted, amount, description, file_index) '
                 "VALUES (?, 'h:1', 'u2', '2029-01-03', -10, 'Coffee', 0), "
                 "(?, 'h:2', 'u2', '2029-01-04', -99, 'GYM', 1)",
                 (hid, hid))
    conn.execute("INSERT INTO upload_txns (household_id, upload_id, txn_id, file_index) VALUES (?, 'u2', 'h:1', 0), "
                 "(?, 'u2', 'h:2', 1)", (hid, hid))
    repo.insert_txns(conn, hid, 'u1', [])
    conn.execute('INSERT INTO bank_txns (household_id, id, first_upload_id, posted, amount, description, file_index) '
                 "VALUES (?, 'B1', 'u1', '2029-01-03', -10, 'COFFEE', 0), (?, 'B2', 'u1', '2029-01-04', -99, 'GYM', 1)",
                 (hid, hid))
    conn.execute("INSERT INTO upload_txns (household_id, upload_id, txn_id, file_index) VALUES (?, 'u1', 'B1', 0), "
                 "(?, 'u1', 'B2', 1)", (hid, hid))
    d = date(2029, 1, 2)
    repo.insert_card_txns(conn, hid, 'u1', [CardTxn('K1', d, d, -500, 'CAFE', 'Other')])
    conn.execute("INSERT INTO card_txns (household_id, id, purchased, posted, amount, description, category, by_hand) "
                 "VALUES (?, 'h:k', '2029-01-02', '2029-01-02', -500, 'CAFE', 'Treats', 1)", (hid,))
    # a ledger row cleared by the content-id copy of the gym payment: that copy is the one kept
    b = repo.load_budget(conn, hid)
    gym = ordered(b.rows)[0]
    repo.save_budget(conn, hid, replace(b, rows=tuple(replace(r, cleared_txn_id='h:2') if r.id == gym.id else r
                                                       for r in b.rows)), user_id=None, action='test')
    assert repo.merge_duplicate_txns(conn, hid) == (2, 1)
    assert sorted(t.id for t in repo.list_history(conn, hid)) == ['B1', 'h:2']
    assert [t.id for t in repo.upload_txns(conn, hid, 'u2')] == ['B1', 'h:2']
    assert [t.id for t in repo.upload_txns(conn, hid, 'u1')] == ['B1', 'h:2']
    assert [(t.id, t.category) for t in repo.list_card_txns(conn, hid)] == [('h:k', 'Treats')]   # moved by hand
    assert repo.merge_duplicate_txns(conn, hid) == (0, 0)


def test_deleting_a_waiting_upload_keeps_what_others_use(conn: sqlite3.Connection) -> None:
    _, hid = household(conn)
    for uid in ('u1', 'u2'):
        repo.create_upload(conn, hid, upload_id=uid, kind='bank', filename=uid, uploaded_by=None, rows=2,
                           date_from=None, date_to=None, latest_balance=None, has_balance=True)
    repo.insert_txns(conn, hid, 'u1', [txn('t3', 3, -10, 970, 0), txn('t2', 2, -10, 980, 1), txn('t1', 1, -20, 990, 2)])
    repo.insert_txns(conn, hid, 'u2', [txn('t4', 4, -10, 960, 0), txn('t3', 3, -10, 970, 1)])
    b = repo.load_budget(conn, hid)
    used = replace(ordered(b.rows)[0], cleared_txn_id='t1')
    paid = replace(ordered(b.rows)[1], paid_txn_ids=('t2',))
    b = replace(b, rows=tuple({used.id: used, paid.id: paid}.get(r.id, r) for r in b.rows))
    repo.save_budget(conn, hid, b, user_id=None, action='x')
    # #88: what deleting with postings would remove is said first; plain delete only hides and keeps them
    r = repo.upload_removal(conn, hid, 'u2')
    assert (r.can_delete, r.bank, r.card, r.first, r.last) == (True, 1, 0, date(2029, 1, 4), date(2029, 1, 4))
    assert repo.upload_removal(conn, hid, 'u1').bank == 0              # t3 is in u2 too, t1 and t2 are used
    assert repo.delete_upload(conn, hid, 'u2') == 'hidden'
    assert [t.id for t in repo.list_history(conn, hid)] == ['t1', 't2', 't3', 't4']
    conn.execute('UPDATE uploads SET hidden = 0 WHERE household_id = ?', (hid,))
    assert repo.delete_upload(conn, hid, 'u2', postings=True) == 'deleted'   # waiting and never reconciled
    assert [t.id for t in repo.list_history(conn, hid)] == ['t1', 't2', 't3']   # t3 is still in u1; t4 goes
    repo.insert_txns(conn, hid, 'u1', [txn('t9', 9, -1, 1, 3)])
    assert repo.delete_upload(conn, hid, 'u1', postings=True) == 'deleted'
    assert [t.id for t in repo.list_history(conn, hid)] == ['t1', 't2']        # cleared and paid rows keep theirs
    assert repo.get_upload(conn, hid, 'u1') is None


def test_upload_metadata_and_status(conn: sqlite3.Connection) -> None:
    _, hid = household(conn)
    repo.create_upload(conn, hid, upload_id='c1', kind='card', filename='card.csv', uploaded_by='admin', rows=2,
                       date_from=date(2029, 1, 1), date_to=date(2029, 1, 2), latest_balance=None, has_balance=False,
                       note='x' * 600, now=NOW)
    u = repo.get_upload(conn, hid, 'c1')
    assert u is not None and u.status == 'waiting' and len(u.note) == 500 and u.date_to == date(2029, 1, 2)
    assert repo.list_uploads(conn, hid, kind='bank') == [] and len(repo.list_uploads(conn, hid, kind='card')) == 1
    repo.create_upload(conn, hid, upload_id='c2', kind='card', filename='card2.csv', uploaded_by='admin', rows=1,
                       date_from=None, date_to=None, latest_balance=None, has_balance=False, now=NOW)
    cards = [CardTxn('k1', date(2029, 1, 1), date(2029, 1, 2), -500, 'CAFE', 'Eating out'),
             CardTxn('k2', date(2029, 1, 2), date(2029, 1, 3), -900, 'MARKET', 'Grocery')]
    assert repo.insert_card_txns(conn, hid, 'c1', cards) == (2, 0)
    assert repo.insert_card_txns(conn, hid, 'c2', cards[:1]) == (0, 1)       # k1 is in both uploads
    assert repo.insert_card_txns(conn, hid, None, cards[:1]) == (0, 1)
    assert repo.list_card_txns(conn, hid) == cards
    assert repo.upload_removal(conn, hid, 'c1').card == 1                  # k2 only
    assert repo.delete_upload(conn, hid, 'c1', postings=True) == 'deleted'
    assert repo.list_card_txns(conn, hid) == cards[:1]                       # k1 stays: c2 still holds it
    repo.set_upload_status(conn, hid, 'c2', 'rejected', 'the balance chain breaks')
    assert repo.get_upload(conn, hid, 'c2').status == 'rejected'  # type: ignore[union-attr]
    assert repo.delete_upload(conn, hid, 'c2', postings=True) == 'hidden'    # not waiting any more
    assert not repo.upload_removal(conn, hid, 'c2').can_delete
    assert repo.list_card_txns(conn, hid) == cards[:1]


def test_profiles(conn: sqlite3.Connection) -> None:
    _, hid = household(conn)
    wb = MappingProfile('w', 'Sheet', 'workbook', WorkbookMapping(sheet='S', header_row=1, first_row=2, name_col='A',
                                                                   amount_col='B', date_col='C', skip_names=('^x$',)))
    bank = MappingProfile('b', 'Bank', 'bank', BankMapping('Date', amount_col='Amount'), header=('Date', 'Amount'))
    repo.save_profile(conn, hid, wb)
    repo.save_profile(conn, hid, bank)
    repo.save_profile(conn, hid, replace(bank, name='Bank 2'))
    assert repo.list_profiles(conn, hid) == [replace(bank, name='Bank 2'), wb]
    assert repo.list_profiles(conn, hid, kind='workbook') == [wb] and repo.get_profile(conn, hid, 'w') == wb
    with pytest.raises(ValueError, match='built-in'):
        repo.save_profile(conn, hid, replace(bank, builtin=True))
    repo.delete_profile(conn, hid, 'w')
    assert repo.get_profile(conn, hid, 'w') is None


def test_notes_settings_and_audit(conn: sqlite3.Connection) -> None:
    uid, hid = household(conn)
    repo.save_notes(conn, hid, 'Check the gutters.', user_id=uid)
    assert repo.load_notes(conn, hid) == 'Check the gutters.'
    with pytest.raises(ValueError):
        repo.save_notes(conn, hid, 'x' * 100_001, user_id=uid)
    for fn in (lambda: repo.save_notes(conn, 999, '', user_id=uid), lambda: repo.load_notes(conn, 999)):
        with pytest.raises(KeyError):
            fn()
    repo.set_setting(conn, 'setup_token_hash', 'abc')
    repo.set_setting(conn, 'setup_token_hash', 'def')
    assert repo.get_setting(conn, 'setup_token_hash') == 'def'
    repo.delete_setting(conn, 'setup_token_hash')
    assert repo.get_setting(conn, 'setup_token_hash') is None
    old = datetime(1999, 1, 1, tzinfo=UTC)
    repo.audit(conn, household_id=None, user_id=uid, action='login.ok', now=old)
    assert repo.list_audit(conn, None)[0]['action'] == 'login.ok'
    assert repo.purge_audit(conn, older_than=old + timedelta(days=1)) == 1
    assert repo.list_audit(conn, None) == []


def test_wipe_keeps_household_members_and_audit(conn: sqlite3.Connection) -> None:
    uid, hid = household(conn)
    repo.save_notes(conn, hid, 'n', user_id=uid)
    before = len(repo.list_audit(conn, hid))
    repo.wipe_household(conn, hid, user_id=uid, today=TODAY)
    b = repo.load_budget(conn, hid)
    assert b.rows == () and b.series == () and b.start_balance == 0 and repo.load_notes(conn, hid) == ''
    assert repo.get_role(conn, uid, hid) == 'owner'
    log = repo.list_audit(conn, hid)
    assert len(log) == before + 1 and log[0]['action'] == 'household.wipe'
    with pytest.raises(KeyError):
        repo.wipe_household(conn, 999)


def test_merge_import(conn: sqlite3.Connection) -> None:
    uid, hid = household(conn)
    before = repo.load_budget(conn, hid)
    doc = (b'format: household-budget/bill-system\nversion: 1\nhousehold: {name: Ignored}\n'
           b'bills:\n  - {id: internet, name: Internet, amount: 95, schedule: {cadence: monthly, day: 12}}\n'
           b'  - {id: gym, name: Gym, amount: 30, schedule: {cadence: monthly, day: 2}}\n')
    repo.import_bill_system(conn, hid, doc, 'm.yaml', today=date(2029, 3, 1), user_id=uid, mode='merge')
    after = repo.load_budget(conn, hid)
    by = after.series_by_id()
    assert by['internet'].amount == 9500 and 'gym' in by and len(after.series) == len(before.series) + 1
    later = [r for r in after.rows if r.series_id == 'internet' and r.due_date >= date(2029, 3, 1)]
    earlier = [r for r in after.rows if r.series_id == 'internet' and r.due_date < date(2029, 3, 1)]
    assert later and all(r.amount == 9500 for r in later) and all(r.amount == 8000 for r in earlier)
    assert any(r.series_id == 'gym' for r in after.rows)
    assert {r.id for r in before.rows} <= {r.id for r in after.rows}       # nothing is deleted
    assert repo.get_household(conn, hid).name == 'Example household'  # type: ignore[union-attr]
    backup = repo.backup(conn, hid, app_version='t').encode()
    with pytest.raises(ImportProblem, match='only replace'):
        repo.import_bill_system(conn, hid, backup, 'b.yaml', today=TODAY, user_id=uid, mode='merge')
    with pytest.raises(ValueError, match='replace or merge'):
        repo.import_bill_system(conn, hid, doc, 'm.yaml', today=TODAY, user_id=uid, mode='other')  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        repo.import_bill_system(conn, 999, doc, 'm.yaml', today=TODAY, user_id=uid)
    with pytest.raises(KeyError):
        repo.backup(conn, 999, app_version='t')


def test_the_last_owner_stays(conn: sqlite3.Connection) -> None:
    uid, hid = household(conn)
    with pytest.raises(repo.LastOwner):
        repo.remove_member(conn, hid, uid)
    with pytest.raises(repo.LastOwner):
        repo.set_role(conn, hid, uid, 'editor')
    with pytest.raises(repo.LastOwner):
        repo.add_member(conn, hid, uid, 'viewer')
    second = users.create_user(conn, 'second', PW)
    repo.add_member(conn, hid, second, 'owner')
    repo.set_role(conn, hid, uid, 'editor')                                # another owner exists now
    with pytest.raises(repo.LastOwner):
        repo.remove_member(conn, hid, second)
    repo.set_role(conn, hid, uid, 'owner')
    repo.remove_member(conn, hid, second)
    assert [(m.username, m.role) for m in repo.list_members(conn, hid)] == [('admin', 'owner')]


def test_edit_budget_is_atomic_and_versioned(conn: sqlite3.Connection) -> None:
    uid, hid = household(conn)
    v = repo.budget_version(conn, hid)
    row = next(r for r in ordered(repo.load_budget(conn, hid).rows) if r.kind is Kind.BILL)
    _, v2 = repo.edit_budget(conn, hid, lambda b: change_amount(b, row.id, 12300), user_id=uid,
                               action='row.change', row_id=row.id, expected_version=v)
    assert v2 == v + 1 == repo.budget_version(conn, hid)
    assert next(r for r in repo.load_budget(conn, hid).rows if r.id == row.id).amount == 12300
    with pytest.raises(repo.Conflict):                                   # someone saved in between
        repo.edit_budget(conn, hid, lambda b: b, user_id=uid, action='x', expected_version=v)

    def boom(b: object) -> object:
        raise ValueError('not allowed')
    with pytest.raises(ValueError):
        repo.edit_budget(conn, hid, boom, user_id=uid, action='x')  # type: ignore[arg-type]
    assert repo.budget_version(conn, hid) == v2 and not conn.in_transaction   # nothing saved, lock released
    with pytest.raises(KeyError):
        repo.budget_version(conn, 999)


def test_save_extras_directly(conn: sqlite3.Connection) -> None:
    from hbudget.io import billsystem
    _, hid = household(conn)
    extras = billsystem.BackupExtras(
        bank_txns=((txn('t1', 2, -100, 900, 0), 'u1'),), upload_links=(('u1', 't1', 0, 900, ''),),
        card_txns=((CardTxn('k1', TODAY, TODAY, -50, 'CAFE', 'Other'), 'u1'),),
        uploads=({'id': 'u1', 'kind': 'bank', 'filename': 'x.csv', 'uploaded_at': '2029-01-05T10:00:00+00:00',
                  'uploaded_by': 'someone', 'rows': 1, 'from': '2029-01-02', 'to': '2029-01-02', 'latest_balance': 9,
                  'has_balance': True, 'status': 'imported', 'note': ''},),
        reconciliations=({'id': 'r1', 'at': '2029-01-05T10:05:00+00:00', 'by': 'someone', 'upload': 'u1',
                          'through': '2029-01-02', 'new_balance': 9, 'cleared': [], 'accepted': [], 'report': '#'},),
        mapping_profiles=(MappingProfile('p', 'P', 'bank', BankMapping('Date', amount_col='Amount')),),
        notes='restored', audit=({'at': '2029-01-01T00:00:00+00:00', 'by': 'admin', 'action': 'old', 'row': None,
                                  'detail': {'k': 1}, 'imported': False},), members=(('stranger', 'owner'),))
    repo.save_extras(conn, hid, extras)
    out = repo.load_extras(conn, hid)
    assert out.bank_txns == extras.bank_txns and out.upload_links == extras.upload_links
    assert out.card_txns == extras.card_txns and out.mapping_profiles == extras.mapping_profiles
    assert out.uploads == ({**extras.uploads[0], 'hidden': False},) and out.reconciliations == extras.reconciliations
    assert out.notes == 'restored' and 'stranger' not in dict(out.members)          # members are never created
    old = next(a for a in repo.list_audit(conn, hid, limit=10_000) if a['action'] == 'old')
    assert old['imported'] and old['user_id'] is None and old['username'] == 'admin'   # a name, not a user link
    assert repo.upload_txns(conn, hid, 'u1')[0].id == 't1' and repo.seen_txn_ids(conn, hid) == {'t1'}


def _bank_upload(conn: sqlite3.Connection, hid: int, upload_id: str, txns: list[object], *, at: datetime) -> None:
    repo.create_upload(conn, hid, upload_id=upload_id, kind='bank', filename=upload_id + '.csv', uploaded_by=None,
                       rows=len(txns), date_from=None, date_to=None, latest_balance=None, has_balance=True, now=at)
    repo.insert_txns(conn, hid, upload_id, txns)  # type: ignore[arg-type]


def test_a_corrected_re_export_is_previewed_with_its_own_balances(conn: sqlite3.Connection) -> None:
    from hbudget.core import reconcile
    _, hid = household(conn)
    b = repo.load_budget(conn, hid)
    start = b.start_balance                  # the basis: the balance before the new postings
    # the bad file: right postings, wrong running balances (and one posting the bank later dropped)
    bad = [txn('t3', 4, -3000, start + 1, 0, 'ELECTRIC CO'), txn('tx', 3, -1, start + 7, 1, 'ODD'),
           txn('t2', 3, -2000, start + 9, 2, 'INTERNET PROVIDER'), txn('t1', 2, 0, start, 3, 'OPENING')]
    _bank_upload(conn, hid, 'bad', bad, at=NOW)
    with pytest.raises(reconcile.ReconcileError) as e:
        reconcile.prepare(repo.upload_txns(conn, hid, 'bad'), b)
    assert e.value.code == 'chain_break'
    repo.set_upload_status(conn, hid, 'bad', 'rejected', 'the balance chain breaks')
    good = [txn('t3', 4, -3000, start - 5000, 0, 'ELECTRIC CO'),
            txn('t2', 3, -2000, start - 2000, 1, 'INTERNET PROVIDER'), txn('t1', 2, 0, start, 2, 'OPENING')]
    _bank_upload(conn, hid, 'good', good, at=NOW + timedelta(minutes=5))
    own = repo.upload_txns(conn, hid, 'good')
    assert [t.balance for t in own] == [start - 5000, start - 2000, start]      # this file's values, not the bad one's
    prepared = reconcile.prepare(own, b)                                        # the preview no longer breaks
    assert prepared.latest_balance == start - 5000 and [t.id for t in prepared.new] == ['t2', 't3']
    assert [t.balance for t in repo.upload_txns(conn, hid, 'bad')] == [start + 1, start + 7, start + 9, start]
    # the history keeps the newest non-rejected file's values and leaves out what only the rejected file had
    hist = {t.id: t for t in repo.list_history(conn, hid)}
    assert set(hist) == {'t1', 't2', 't3'} and hist['t3'].balance == start - 5000
    assert repo.history_counts(conn, hid)['bank_txns_in_history'] == 3
    assert repo.history_counts(conn, hid)['bank_txns'] == 4


def test_rejected_only_transactions_stay_out_of_check_up(conn: sqlite3.Connection) -> None:
    from hbudget.core import checkup
    _, hid = household(conn)
    _bank_upload(conn, hid, 'r1', [txn('e1', 6, -15000, 100, 0, 'ELECTRIC CO')], at=NOW)
    assert any(c.rule_id == 'electric' for c in checkup.compute(repo.list_history(conn, hid),
                                                                repo.load_budget(conn, hid)).bills)
    repo.set_upload_status(conn, hid, 'r1', 'rejected', 'wrong file')
    assert repo.list_history(conn, hid) == []
    result = checkup.compute(repo.list_history(conn, hid), repo.load_budget(conn, hid))
    assert result.transactions == 0 and result.bills == ()
    repo.set_upload_status(conn, hid, 'r1', 'waiting')                           # un-rejecting brings it back
    assert [t.id for t in repo.list_history(conn, hid)] == ['e1']


def test_history_counts_label_hidden_uploads(conn: sqlite3.Connection) -> None:
    _, hid = household(conn)
    _bank_upload(conn, hid, 'a', [txn('t1', 2, -100, 900, 0)], at=NOW)
    _bank_upload(conn, hid, 'b', [txn('t2', 3, -100, 800, 0)], at=NOW)
    repo.set_upload_status(conn, hid, 'a', 'imported')
    assert repo.delete_upload(conn, hid, 'a') == 'hidden'
    counts = repo.history_counts(conn, hid)
    assert counts['uploads'] == 2 and counts['uploads_hidden'] == 1 and len(repo.list_uploads(conn, hid)) == 1
    assert set(counts) == {'rows', 'bank_txns', 'bank_txns_in_history', 'card_txns', 'uploads', 'uploads_hidden',
                           'reconciliations', 'profiles'}


def test_merge_does_not_bring_back_a_bill_paid_early(conn: sqlite3.Connection) -> None:
    """#64: the household's first REDCARD row is 4/7 (3/7 was paid early and is gone); a file whose series starts on
    3/7 must not add 3/7 again."""
    from factories import budget, schedule, series
    from hbudget.core.ledger import generate
    from hbudget.web.billimport import row_changes
    hid = repo.create_household(conn, 'H', as_of=date(2029, 3, 1))
    redcard = series('redcard', 'bill', 50000, schedule('monthly', date(2029, 4, 7), day=7), name='REDCARD (Loan)')
    b = generate(budget(as_of=date(2029, 3, 1), series=(redcard,)), date(2029, 6, 30))
    repo.save_budget(conn, hid, b, user_id=None, action='test')
    old = repo.load_budget(conn, hid)
    doc = (b'format: household-budget/bill-system\nversion: 1\nhousehold: {name: H}\n'
           b'bills:\n  - {id: redcard, name: REDCARD (Loan), amount: 500, '
           b'schedule: {cadence: monthly, start: 2029-03-07, day: 7}}\n')
    repo.import_bill_system(conn, hid, doc, 'm.yaml', today=date(2029, 3, 1), user_id=None, mode='merge')
    new = repo.load_budget(conn, hid)
    dues = sorted(r.due_date for r in new.rows if r.series_id == 'redcard')
    assert dues[0] == date(2029, 4, 7) and date(2029, 3, 7) not in dues
    assert row_changes(old, new)['counts'] == {'added': 0, 'removed': 0, 'changed': 0, 'check': 0}


def test_row_changes_lists_added_removed_changed_and_flags_rows_before_existing_ones() -> None:
    from factories import budget, row

    old = budget(rows=(row('a', 'Rent', 100000, date(2029, 3, 5), series_id='rent'),
                       row('b', 'Phone', 5000, date(2029, 3, 9)),
                       row('c', 'Gym', 3000, date(2029, 3, 20), series_id='gym')))
    new = budget(rows=(row('a', 'Rent', 110000, date(2029, 3, 5), series_id='rent'),
                       row('g0', 'Gym', 3000, date(2029, 3, 6), series_id='gym'),     # before the existing 3/20
                       row('c', 'Gym', 3000, date(2029, 3, 20), series_id='gym'),
                       row('n', 'New', 700, date(2029, 3, 25), series_id='new')))
    from hbudget.web.billimport import row_changes
    ch = row_changes(old, new)
    assert [(r['id'], r['check']) for r in ch['added']] == [('g0', True), ('n', False)]
    assert [r['id'] for r in ch['removed']] == ['b']
    assert [(r['id'], r['amount_was'], r['amount']) for r in ch['changed']] == [('a', 100000, 110000)]
    assert ch['counts'] == {'added': 2, 'removed': 1, 'changed': 1, 'check': 1}
