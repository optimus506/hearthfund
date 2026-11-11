"""io.plaid: the pure mapping (signs, masking, balances worked back) and the client against a fake transport."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from hbudget.io import plaid as pl

D = date


def raw(tid: str, day: str, amount: str, desc: str = 'SHOP', account: str = 'chk', **kw: object) -> dict[str, object]:
    return {'transaction_id': tid, 'account_id': account, 'date': day, 'amount': float(amount),
            'original_description': desc, 'name': 'Shop', 'pending': False, **kw}


def test_cents_rounds_half_up() -> None:
    assert pl.cents(Decimal('12.345')) == 1235 and pl.cents('0.1') == 10 and pl.cents(-4.5) == -450


def test_parse_txn_flips_the_sign_and_masks_numbers() -> None:
    t = pl.parse_txn(raw('t1', '2029-03-04', '25.10', 'CARD 123456789012 COFFEE', authorized_date='2029-03-02'))
    assert t is not None
    assert (t.amount, t.posted, t.authorized, t.pending) == (-2510, D(2029, 3, 4), D(2029, 3, 2), False)
    assert '12345678' not in t.description and 'COFFEE' in t.description
    pay = pl.parse_txn(raw('t2', '2029-03-05', '-1500.00', 'PAYROLL'))
    assert pay is not None and pay.amount == 150000                   # money in is positive in the bank's sign
    assert pl.parse_txn({'transaction_id': 'x'}) is None
    assert pl.parse_txn(raw('t3', 'not a date', '1')) is None
    named = pl.parse_txn({**raw('t4', '2029-03-05', '1'), 'original_description': None})
    assert named is not None and named.description == 'Shop'          # falls back to Plaid's name


def test_default_roles() -> None:
    accts = [pl.PlaidAccount('sav', 'Savings', '1', 'depository', 'savings', 0),
             pl.PlaidAccount('chk', 'Checking', '2', 'depository', 'checking', 0),
             pl.PlaidAccount('chk2', 'Checking 2', '3', 'depository', 'checking', 0),
             pl.PlaidAccount('cc', 'Card', '4', 'credit', 'credit card', 0)]
    assert pl.default_roles(accts) == {'sav': 'ignore', 'chk': 'bank', 'chk2': 'ignore', 'cc': 'card'}
    assert pl.default_roles(accts, have_bank=True)['chk'] == 'ignore'


def test_bank_file_works_balances_back_from_the_current_balance() -> None:
    txns = [('a', D(2029, 3, 1), -1000, 'A'), ('b', D(2029, 3, 3), -500, 'B'), ('c', D(2029, 3, 5), 2000, 'C')]
    # start balance 10000 before a; after a 9000, after b 8500, after c 10500 = current
    bf, ok = pl.bank_file(txns, 10500, 9000)
    assert ok and bf.has_balance and bf.latest_balance == 10500
    assert [(t.id, t.balance, t.file_index) for t in bf.txns] == [('c', 10500, 0), ('b', 8500, 1), ('a', 9000, 2)]
    assert (bf.first, bf.last) == (D(2029, 3, 1), D(2029, 3, 5))


def test_bank_file_drops_balances_that_do_not_line_up() -> None:
    txns = [('a', D(2029, 3, 1), -1000, 'A')]
    bf, ok = pl.bank_file(txns, 10500, 12345)
    assert not ok and not bf.has_balance and bf.latest_balance is None and bf.txns[0].balance is None
    bf, ok = pl.bank_file(txns, None, 0)
    assert not ok and not bf.has_balance


class Fake:
    def __init__(self, answers: dict[str, list[tuple[int, object]]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, body: bytes) -> tuple[int, bytes]:
        path = url.removeprefix('https://sandbox.plaid.com')
        assert url.startswith('https://sandbox.plaid.com/')
        self.calls.append((path, json.loads(body)))
        status, data = self.answers[path].pop(0)
        return status, json.dumps(data).encode()


def test_client_sends_credentials_to_the_fixed_host_and_reads_errors() -> None:
    f = Fake({'/link/token/create': [(200, {'link_token': 'link-1'}),
                                      (400, {'error_code': 'INVALID_FIELD', 'display_message': None,
                                             'error_message': 'bad'})]})
    c = pl.PlaidClient('sandbox', 'cid', 'sec', transport=f)
    url = 'https://b.example/plaid/oauth'
    assert c.link_token(user_ref='u', client_name='HB', days=180, redirect_uri=url) == 'link-1'
    body = f.calls[0][1]
    assert body['client_id'] == 'cid' and body['secret'] == 'sec' and body['products'] == ['transactions']
    assert body['transactions'] == {'days_requested': 180} and body['redirect_uri'] == 'https://b.example/plaid/oauth'
    with pytest.raises(pl.PlaidError) as e:
        c.link_token(user_ref='u', client_name='HB', days=5, redirect_uri=None, access_token='acc')
    assert e.value.code == 'INVALID_FIELD' and e.value.message == 'bad'
    assert 'products' not in f.calls[1][1] and f.calls[1][1]['access_token'] == 'acc'
    with pytest.raises(ValueError):
        pl.PlaidClient('development', 'a', 'b')


def test_sync_reads_every_page_and_restarts_after_a_change_during_paging() -> None:
    page1 = {'added': [raw('t1', '2029-03-01', '5')], 'modified': [], 'removed': [], 'next_cursor': 'c1',
             'has_more': True}
    page2 = {'added': [raw('t2', '2029-03-02', '6', pending=True)], 'modified': [raw('t0', '2029-02-27', '1')],
             'removed': [{'transaction_id': 'gone'}], 'next_cursor': 'c2', 'has_more': False}
    mutation = {'error_code': 'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION', 'error_message': 'retry'}
    f = Fake({'/transactions/sync': [(200, page1), (400, mutation), (200, page1), (200, page2)]})
    s = pl.PlaidClient('sandbox', 'cid', 'sec', transport=f).sync('acc', None)
    assert [t.plaid_id for t in s.added] == ['t1', 't2'] and s.added[1].pending
    assert [t.plaid_id for t in s.modified] == ['t0'] and s.removed == ('gone',) and s.cursor == 'c2' and s.ready
    assert 'cursor' not in f.calls[0][1] and f.calls[1][1]['cursor'] == 'c1' and 'cursor' not in f.calls[2][1]
    assert f.calls[0][1]['options'] == {'include_original_description': True}


def test_login_required_is_flagged() -> None:
    f = Fake({'/transactions/sync': [(400, {'error_code': 'ITEM_LOGIN_REQUIRED', 'display_message': 'Sign in'})]})
    with pytest.raises(pl.PlaidError) as e:
        pl.PlaidClient('sandbox', 'cid', 'sec', transport=f).sync('acc', 'c0')
    assert e.value.login_required and e.value.message == 'Sign in'


def test_a_redirect_is_never_followed() -> None:
    assert pl._NoRedirect().redirect_request() is None


def test_parse_account_keeps_what_the_app_needs() -> None:
    a = pl.parse_account({'account_id': 'acc-1', 'name': 'Everyday Checking' * 10, 'mask': '1234',
                          'type': 'depository', 'subtype': 'checking', 'balances': {'current': 1234.5}})
    assert (a.account_id, a.mask, a.type, a.subtype, a.current) == ('acc-1', '1234', 'depository', 'checking', 123450)
    assert len(a.name) == 80
    bare = pl.parse_account({'account_id': 7, 'balances': None})
    assert (bare.account_id, bare.name, bare.mask, bare.current) == ('7', '', '', None)
