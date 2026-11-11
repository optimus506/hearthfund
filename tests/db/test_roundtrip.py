"""Export -> wipe -> import -> export with the real db layer (ARCHITECTURE 20.6), for YAML and JSON."""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
from dbfactory import TODAY, household, txn

from hbudget.core.forecast import compute
from hbudget.core.ledger import change_amount, mark_status, ordered, remove_row
from hbudget.core.models import CardTxn, ExpectedItem, Status
from hbudget.db import repo, users
from hbudget.io import billsystem
from hbudget.io.bank import BankMapping, MappingProfile

STAMP = datetime(2029, 1, 6, 9, 0, tzinfo=UTC)
ROUND_TRIP_ACTIONS = {'backup.export', 'household.wipe', 'import.replace'}


def _populate(conn: sqlite3.Connection) -> tuple[int, int]:
    uid, hid = household(conn)
    partner = users.create_user(conn, 'partner', 'another long password')
    repo.add_member(conn, hid, partner, 'contributor')
    b = repo.load_budget(conn, hid)
    rows = ordered(b.rows)
    b = change_amount(b, rows[2].id, rows[2].amount + 1500)
    b = remove_row(b, rows[4].id)
    b = mark_status(b, rows[5].id, Status.MANUAL)
    b = replace(b, expected=(ExpectedItem('e1', 'Refund', 4000, 'in', date(2029, 1, 20)),),
                last_reconciled_through=date(2029, 1, 3))
    repo.save_budget(conn, hid, b, user_id=uid, action='edits')
    repo.create_upload(conn, hid, upload_id='u1', kind='bank', filename='bank.csv', uploaded_by='admin', rows=2,
                       date_from=date(2029, 1, 2), date_to=date(2029, 1, 3), latest_balance=240000, has_balance=True,
                       status='imported', note='dry run', now=STAMP)
    repo.insert_txns(conn, hid, 'u1', [txn('t2', 3, -5000, 240000, 0, 'ELECTRIC CO'),
                                       txn('t1', 2, -2500, 245000, 1, 'COFFEE')])
    repo.create_upload(conn, hid, upload_id='c1', kind='card', filename='card.csv', uploaded_by='partner', rows=1,
                       date_from=None, date_to=None, latest_balance=None, has_balance=False, now=STAMP)
    repo.insert_card_txns(conn, hid, 'c1', [CardTxn('k1', date(2029, 1, 1), date(2029, 1, 2), -900, 'CAFE',
                                                    'Eating out')])
    repo.save_reconciliation(conn, hid, rec_id='rc1', by_user='admin', upload_id='u1', through=date(2029, 1, 3),
                             new_balance=240000, result={'cleared': [{'row': rows[0].id, 'txn': 't2', 'how': 'auto'}],
                                                         'accepted': ['P1']}, report_md='# Report', now=STAMP)
    repo.save_profile(conn, hid, MappingProfile('p1', 'Bank layout', 'bank', BankMapping('Date', amount_col='Amount'),
                                                ('Date', 'Description', 'Amount')))
    repo.save_notes(conn, hid, 'Remember the gutters.', user_id=uid)
    assert repo.delete_upload(conn, hid, 'u1') == 'hidden'          # reconciled: hidden, kept in the history
    return uid, hid


def _without_round_trip_audit(doc: dict[str, Any], keep: int) -> dict[str, Any]:
    backup = dict(doc['backup'])
    added = backup['audit'][keep:]
    assert {a['action'] for a in added} <= ROUND_TRIP_ACTIONS, added   # the round trip only adds its own entries
    backup['audit'] = backup['audit'][:keep]
    backup.pop('exported_at')
    return {**doc, 'backup': backup}


@pytest.mark.parametrize('fmt', ['yaml', 'json'])
def test_export_wipe_import_gives_the_same_document_and_forecast(conn: sqlite3.Connection, fmt: str) -> None:
    uid, hid = _populate(conn)
    before = compute(repo.load_budget(conn, hid), today=TODAY)
    text = repo.backup(conn, hid, fmt=fmt, app_version='0.1.0', exported_at=STAMP, user_id=uid)  # type: ignore[arg-type]
    first = billsystem.parse(text.encode(), 'b.' + fmt)
    repo.wipe_household(conn, hid, user_id=uid, today=date(2029, 2, 1))
    assert repo.load_budget(conn, hid).rows == ()
    result = repo.import_bill_system(conn, hid, text.encode(), 'b.' + fmt, today=date(2029, 2, 1), user_id=uid)
    assert result.extras is not None and dict(result.extras.members) == {'admin': 'owner', 'partner': 'contributor'}
    after = compute(repo.load_budget(conn, hid), today=TODAY)
    assert after == before
    assert repo.get_upload(conn, hid, 'u1').hidden  # type: ignore[union-attr]   # still hidden after the restore
    assert [u.id for u in repo.list_uploads(conn, hid)] == ['c1']
    again = billsystem.parse(repo.backup(conn, hid, fmt=fmt, app_version='0.1.0',  # type: ignore[arg-type]
                                         exported_at=datetime(2030, 1, 1, tzinfo=UTC)).encode(), 'c.' + fmt)
    keep = len(first['backup']['audit'])  # type: ignore[index]
    assert _without_round_trip_audit(again, keep) == _without_round_trip_audit(first, keep)


def test_restore_never_creates_memberships_and_marks_audit_imported(conn: sqlite3.Connection) -> None:
    uid, hid = _populate(conn)
    text = repo.backup(conn, hid, app_version='0.1.0', exported_at=STAMP)
    fresh = repo.create_household(conn, 'Fresh')
    repo.import_bill_system(conn, fresh, text.encode(), 'b.yaml', today=TODAY, user_id=uid)
    assert repo.list_members(conn, fresh) == []                              # people are re-added by hand
    log = repo.list_audit(conn, fresh, limit=10_000)
    in_backup = billsystem.parse(text.encode())['backup']['audit']  # type: ignore[index]
    assert sum(a['imported'] for a in log) == len(in_backup) > 0          # every restored entry is marked imported
    assert any(a['action'] == 'import.replace' and not a['imported'] for a in log)   # this restore itself is not
    assert compute(repo.load_budget(conn, fresh), today=TODAY) == compute(repo.load_budget(conn, hid), today=TODAY)
    assert repo.get_household(conn, fresh).name == 'Example household'  # type: ignore[union-attr]


def test_audit_of_a_missing_user_keeps_the_name(conn: sqlite3.Connection) -> None:
    uid, hid = _populate(conn)
    doc = billsystem.parse(repo.backup(conn, hid, app_version='t', exported_at=STAMP).encode())
    doc['backup']['audit'].append({'at': '2029-01-01T00:00:00+00:00', 'by': 'ghost', 'action': 'old',  # type: ignore[index]
                                   'row': None, 'detail': {}, 'imported': False})
    fresh = repo.create_household(conn, 'Fresh')
    repo.import_bill_system(conn, fresh, billsystem.dump_json(doc).encode(), 'b.json', today=TODAY, user_id=uid)
    entries = repo.load_extras(conn, fresh).audit
    ghost = next(a for a in entries if a['action'] == 'old')
    assert ghost['by'] == 'ghost' and ghost['detail'] == {} and ghost['imported'] is True
