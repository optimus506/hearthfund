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


def test_balance_gap_says_what_explains_a_difference() -> None:
    # amounts are bank sign, newest first; the checkbook stands at 100000
    assert pl.balance_gap(100000, 100000, [-2000, 5000], [700]) == pl.Gap(0, 'agree', 0, 1, 700)
    # the two newest postings lead from the checkbook to the bank: +25000 pay, -5000 fuel (older ones are behind it)
    assert pl.balance_gap(120000, 100000, [-5000, 25000, -1000]) == pl.Gap(20000, 'postings', 2)
    assert pl.balance_gap(99000, 100000, [-1000]) == pl.Gap(-1000, 'postings', 1)        # the oldest one closes it
    # the bank already counts a pending deposit that Plaid has not sent as posted
    assert pl.balance_gap(125000, 100000, [-1000], [25000]) == pl.Gap(25000, 'pending', 0, 1, 25000)
    assert pl.balance_gap(123000, 100000, [-2000, -1000], [25000]).reason == 'pending'   # one new posting as well
    assert pl.balance_gap(123000, 100000, [-2000, -1000], [25000]).postings == 1
    # nothing stored explains it: a posting has not been sent yet
    assert pl.balance_gap(125000, 100000, []) == pl.Gap(25000, 'unexplained')
    assert pl.balance_gap(125000, 100000, [-1000], [-999]) == pl.Gap(25000, 'unexplained', 0, 1, -999)
    assert pl.balance_gap(125000, 100000, [], [10000, -10000]).reason == 'unexplained'   # pending that nets to zero


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


def test_liabilities_are_parsed_without_account_numbers_or_addresses() -> None:
    from datetime import date

    from hbudget.io.plaid import Liability, PlaidError, parse_liabilities
    raw = {'credit': [{'account_id': 'c', 'aprs': [{'apr_type': 'purchase_apr', 'apr_percentage': 15.24}],
                       'minimum_payment_amount': 20, 'next_payment_due_date': '2029-05-28',
                       'last_statement_balance': 1708.77},
                      {'account_id': 'd', 'aprs': [{'apr_type': 'cash_apr', 'apr_percentage': 300}]}],
           'mortgage': [{'account_id': 'm', 'account_number': '3120194154', 'interest_rate': {'percentage': 3.99},
                         'next_monthly_payment': 3141.54, 'next_payment_due_date': '2029-04-01',
                         'property_address': {'postal_code': '99999'}}],
           'student': [{'account_id': 's', 'interest_rate_percentage': 5.25, 'minimum_payment_amount': None,
                        'next_payment_due_date': 'soon'}, {'account_id': None}],
           'loan': [{'account_id': 'x'}]}
    assert parse_liabilities(raw) == [
        Liability('c', 1524, 2000, date(2029, 5, 28), 170877), Liability('d'),
        Liability('m', 399, 314154, date(2029, 4, 1)), Liability('s', 525)]
    assert '3120194154' not in repr(parse_liabilities(raw)) and '99999' not in repr(parse_liabilities(raw))
    assert parse_liabilities({}) == []
    assert 'HBUDGET_PLAID_LIABILITIES' in PlaidError('PRODUCT_NOT_ENABLED', 'x').liabilities_note
    assert PlaidError('OTHER', 'Plaid said no.').liabilities_note == 'Plaid said no.'
