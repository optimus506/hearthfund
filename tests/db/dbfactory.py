"""Shared helpers for the db tests: a household loaded from the example bill system."""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from hbudget.core.models import BankTxn
from hbudget.db import repo, users

EXAMPLE = Path(__file__).resolve().parents[2] / 'examples' / 'bill-system.example.yaml'
TODAY = date(2029, 1, 5)
PW = 'correct horse battery'


def household(conn: sqlite3.Connection, name: str = 'admin') -> tuple[int, int]:
    """(owner user id, household id) with the example bill system imported."""
    if users.user_count(conn) == 0:
        uid, hid = users.create_first_admin(conn, name, PW, 'Home')
    else:
        uid = users.create_user(conn, name, PW)
        hid = repo.create_household(conn, 'Home ' + name)
        repo.add_member(conn, hid, uid, 'owner')
    repo.import_bill_system(conn, hid, EXAMPLE.read_bytes(), EXAMPLE.name, today=TODAY, user_id=uid)
    return uid, hid


def txn(tid: str, day: int, amount: int, balance: int | None, index: int, desc: str = 'SHOP') -> BankTxn:
    return BankTxn(id=tid, posted=date(2029, 1, day), amount=amount, description=desc, balance=balance,
                   file_index=index)
