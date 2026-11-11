from __future__ import annotations

from datetime import date

import pytest

from hbudget.core.models import CardCategory
from hbudget.io import ImportProblem
from hbudget.io.bank import (
    BUILTIN_PROFILES,
    BankMapping,
    MappingProfile,
    detect_profile,
    finish,
    id_column,
    read_bank_csv,
    read_card,
    txn_id,
)

NEWEST_FIRST = b"""Id,Date,Description,Amount,Balance,Status
t5,2029-03-05,ELECTRIC CO PAYMENT,-100.00,1400.00,Posted
t4,2029-03-03,EMPLOYER A PAYROLL,2000.00,1500.00,Posted
t3,2029-03-03,HOME LOAN SERVICER,-1500.00,-500.00,Posted
t2,2029-03-01,TRANSFER TO CARD 4000 1234 5678 9010,-300.00,1000.00,Posted
t1,2029-02-28,COFFEE,-5.00,1300.00,Posted
t0,2029-03-06,PENDING THING,-1.00,,Pending
"""
AMOUNT_BALANCE = BankMapping('Date', amount_col='Amount', balance_col='Balance', id_col='Id',
                             status_col='Status')


def test_newest_first_with_balance_and_status() -> None:
    f = read_bank_csv(NEWEST_FIRST, AMOUNT_BALANCE)
    assert [t.id for t in f.txns] == ['t5', 't4', 't3', 't2', 't1']
    assert [t.file_index for t in f.txns] == [0, 1, 2, 3, 4]
    assert f.ignored == 1 and f.duplicates == 0 and f.has_balance and f.latest_balance == 140000
    assert (f.first, f.last) == (date(2029, 2, 28), date(2029, 3, 5))
    assert f.txns[3].description == 'TRANSFER TO CARD #### #### #### 9010'
    assert f.txns[0].amount == -10000 and f.txns[1].amount == 200000


def test_oldest_first_export_is_reversed_by_the_balance_chain() -> None:
    lines = NEWEST_FIRST.decode().strip().split('\n')
    body = [line for line in lines[1:] if 'Pending' not in line]
    data = '\n'.join([lines[0], *reversed(body)]).encode()
    f = read_bank_csv(data, AMOUNT_BALANCE)
    assert [t.id for t in f.txns] == ['t5', 't4', 't3', 't2', 't1']   # same-day t4/t3 kept in chain order


def test_debit_credit_columns_and_no_balance() -> None:
    data = b'Date,Description,Debit,Credit\n03/02/2029,RENT,1500.00,\n03/01/2029,PAY,,2000.00\n'
    f = read_bank_csv(data, BankMapping('Date', debit_col='Debit', credit_col='Credit'))
    assert [t.amount for t in f.txns] == [-150000, 200000]
    assert not f.has_balance and f.latest_balance is None and f.txns[0].balance is None


def test_negate_and_description_columns() -> None:
    data = b'Date,Payee,Memo,Amount\n2029-01-02,SHOP,order 1234567,25.00\n'
    f = read_bank_csv(data, BankMapping('Date', amount_col='Amount', description_cols=('Payee', 'Memo'), negate=True))
    assert f.txns[0].amount == -2500 and f.txns[0].description == 'SHOP order ###4567'


def test_identical_purchases_stay_two_and_duplicate_ids_are_dropped() -> None:
    same = b'Date,Description,Amount\n2029-01-02,COFFEE,-4.00\n2029-01-02,COFFEE,-4.00\n'
    f = read_bank_csv(same, BankMapping('Date', amount_col='Amount'))
    assert len(f.txns) == 2 and f.txns[0].id != f.txns[1].id and f.duplicates == 0
    assert f.txns[0].id.startswith('h:')
    dup = b'Id,Date,Description,Amount\nx1,2029-01-02,COFFEE,-4.00\nx1,2029-01-02,COFFEE,-4.00\n'
    f = read_bank_csv(dup, BankMapping('Date', amount_col='Amount', id_col='Id'))
    assert len(f.txns) == 1 and f.duplicates == 1


def test_txn_id_is_deterministic() -> None:
    a = txn_id(date(2029, 1, 2), -400, 'COFFEE', 1)
    assert a == txn_id(date(2029, 1, 2), -400, 'COFFEE', 1) != txn_id(date(2029, 1, 2), -400, 'COFFEE', 2)


def test_malformed_files() -> None:
    m = BankMapping('Date', amount_col='Amount')
    with pytest.raises(ImportProblem, match='no transactions'):
        read_bank_csv(b'Date,Description,Amount\n', m)
    with pytest.raises(ImportProblem, match='no transactions'):
        read_bank_csv(b'', m)
    with pytest.raises(ImportProblem, match='no "Amount" column'):
        read_bank_csv(b'Date,Description,Value\n2029-01-01,X,1\n', m)
    with pytest.raises(ImportProblem, match='date could not be read') as e:
        read_bank_csv(b'Date,Description,Amount\n2029-01-01,A,1\nlater,B,2\n', m)
    assert e.value.line == 3
    with pytest.raises(ImportProblem, match='amount could not be read'):
        read_bank_csv(b'Date,Description,Amount\n2029-01-01,A,one\n', m)
    with pytest.raises(ImportProblem, match='no posted transactions'):
        read_bank_csv(b'Date,Description,Amount,Status\n2029-01-01,A,1,Pending\n', replace_status(m))
    with pytest.raises(ImportProblem, match='needs an amount column'):
        read_bank_csv(b'Date,Description\n2029-01-01,A\n', BankMapping('Date'))
    with pytest.raises(ImportProblem, match='never to be read'):
        read_bank_csv(b'Date,Description,Amount,Card\n2029-01-01,A,1,4000\n',
                      BankMapping('Date', amount_col='Amount', description_cols=('Card',), ignore_cols=('card',)))


def replace_status(m: BankMapping) -> BankMapping:
    return BankMapping(m.date_col, amount_col=m.amount_col, status_col='Status')


def test_ragged_rows_read_missing_fields_as_empty() -> None:
    f = read_bank_csv(b'Date,Description,Amount,Balance\n2029-01-01,A,-1.00\n',
                      BankMapping('Date', amount_col='Amount', balance_col='Balance'))
    assert f.txns[0].balance is None and not f.has_balance


def test_finish_errors() -> None:
    with pytest.raises(ImportProblem, match='no transactions'):
        finish([])
    with pytest.raises(ImportProblem, match='no posted'):
        finish([], ignored=2)


def test_detect_profile_saved_then_builtin() -> None:
    saved = MappingProfile('p1', 'My bank', 'bank', BankMapping('Posted on', amount_col='Value'),
                           header=('Posted on', 'Text', 'Value'))
    assert detect_profile(['posted on', 'TEXT', 'value'], [saved]) is saved
    p = detect_profile(['Ref', 'Date', 'Description', 'Amount', 'Balance', 'Status'], [saved])
    assert p is not None and p.builtin and p.id == 'builtin-date-desc-amount-balance'
    assert isinstance(p.mapping, BankMapping)
    assert (p.mapping.id_col, p.mapping.status_col, p.mapping.balance_col) == ('Ref', 'Status', 'Balance')
    p = detect_profile(['date', 'description', 'debit', 'credit', 'balance'], [])
    assert p is not None and isinstance(p.mapping, BankMapping) and p.mapping.debit_col == 'debit'
    assert detect_profile(['When', 'What'], []) is None
    assert len(BUILTIN_PROFILES) == 3


CATEGORIES = (CardCategory('Grocery', ('GROCERY', 'MARKET')), CardCategory('Eating out', ('CAFE',),
                                                                           bank_categories=('Food & Dining',)))


def test_read_card_with_purchase_date_across_year_end() -> None:
    data = (b'Date,Description,Amount,Category\n'
            b'2029-01-02,CORNER MARKET BOUGHT ON 12/30/2028,-40.00,\n'
            b'2029-01-02,PAYMENT THANK YOU,200.00,\n'
            b'2029-01-03,BISTRO BOUGHT ON 01/02/29,-12.00,Food & Dining\n'
            b'2029-01-04,HARDWARE BOUGHT ON 02/30/2029,-9.00,\n'
            b'2029-01-05,SHOP BOUGHT ON 01/09/2029,-1.00,\n')
    m = BankMapping('Date', amount_col='Amount', category_col='Category',
                    purchase_date_regex=r'BOUGHT ON (?P<m>\d\d)/(?P<d>\d\d)/(?P<y>\d{2,4})')
    txns = {t.description.split()[0]: t for t in read_card(data, 'card.csv', m, CATEGORIES)}
    assert txns['CORNER'].purchased == date(2028, 12, 30) and txns['CORNER'].category == 'Grocery'
    assert txns['PAYMENT'].category == 'Payment'
    assert txns['BISTRO'].category == 'Eating out' and txns['BISTRO'].purchased == date(2029, 1, 2)
    assert txns['HARDWARE'].purchased == date(2029, 1, 4) and txns['HARDWARE'].category == 'Other'   # Feb 30
    assert txns['SHOP'].purchased == date(2029, 1, 5)         # a purchase "after" its posting is not believed


def test_read_card_builtin_layout_and_unknown_layout() -> None:
    txns = read_card(b'Date,Description,Amount\n2029-01-02,CAFE,-3.00\n', 'card.csv', None, CATEGORIES)
    assert txns[0].category == 'Eating out' and txns[0].purchased == txns[0].posted
    with pytest.raises(ImportProblem, match='not recognised'):
        read_card(b'When,What,How much\n2029-01-02,CAFE,-3.00\n', 'card.csv', None, CATEGORIES)


def test_read_card_from_ofx() -> None:
    ofx = (b'OFXHEADER:100\nDATA:OFXSGML\n\n<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><BANKTRANLIST>'
           b'<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20290103<TRNAMT>-8.00<FITID>c1<NAME>CAFE</STMTTRN>'
           b'</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>')
    txns = read_card(ofx, 'card.qfx', None, CATEGORIES)
    assert [(t.id, t.category, t.amount) for t in txns] == [('c1', 'Eating out', -800)]


def test_read_card_empty_and_bad_pattern() -> None:
    with pytest.raises(ImportProblem, match='no transactions'):
        read_card(b'', 'card.csv', None, CATEGORIES)
    bad = BankMapping('Date', amount_col='Amount', purchase_date_regex='(a+)+')
    with pytest.raises(ImportProblem, match='Purchase date pattern'):
        read_card(b'Date,Description,Amount\n2029-01-02,X,-1.00\n', 'card.csv', bad, CATEGORIES)


def test_purchase_date_pattern_only_sees_the_first_500_characters() -> None:
    far = 'X' * 600 + ' BOUGHT ON 01/01/2029'
    data = ('Date,Description,Amount\n2029-01-02,%s,-1.00\n' % far).encode()
    m = BankMapping('Date', amount_col='Amount', purchase_date_regex=r'BOUGHT ON (?P<m>\d\d)/(?P<d>\d\d)/(?P<y>\d{4})')
    assert read_card(data, 'card.csv', m, CATEGORIES)[0].purchased == date(2029, 1, 2)


def test_built_in_layouts_read_a_date_in_the_description() -> None:
    """#63: "... Date 10/30/29 ..." is the purchase date without a saved layout; a far-off date is not believed."""
    data = (b'Date,Description,Amount\n'
            b'2029-10-31,GROCER #12 Date 10/30/29 Card 1234,-180.00\n'
            b'2029-10-31,OLD THING Date 01/02/29,-5.00\n'
            b'2029-10-31,CAFE,-3.00\n')
    txns = {t.description.split()[0]: t for t in read_card(data, 'card.csv', None, CATEGORIES)}
    assert txns['GROCER'].purchased == date(2029, 10, 30)
    assert txns['OLD'].purchased == date(2029, 10, 31) and txns['CAFE'].purchased == date(2029, 10, 31)
    ofx = (b'OFXHEADER:100\nDATA:OFXSGML\n\n<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><BANKTRANLIST>'
           b'<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20290103<TRNAMT>-8.00<FITID>c1<NAME>CAFE DATE 01/02/2029</STMTTRN>'
           b'</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>')
    assert read_card(ofx, 'card.qfx', None, CATEGORIES)[0].purchased == date(2029, 1, 2)


def test_transaction_id_column_is_recognised() -> None:
    """#87: a bank's "Transaction ID" column (and saved layouts made without picking it) give the bank's ids."""
    header = ['Transaction ID', 'Posting Date', 'Effective Date', 'Reference Number', 'Description', 'Amount',
              'Balance']
    assert id_column(header) == 'Transaction ID'
    assert id_column(['Date', 'Reference Number', 'Amount']) == 'Reference Number'
    assert id_column(['Date', 'Amount']) is None
    saved = MappingProfile('p', 'Mine', 'bank', BankMapping('Posting Date', amount_col='Amount'), tuple(header))
    found = detect_profile(header, [saved])
    assert found is not None and found.id == 'p' and found.mapping.id_col == 'Transaction ID'  # type: ignore[union-attr]


def test_card_date_pattern_and_purchase_date() -> None:
    from hbudget.io.bank import DEFAULT_PURCHASE_DATE, BankMapping, card_date_pattern, purchase_date
    assert card_date_pattern(None) == DEFAULT_PURCHASE_DATE
    assert card_date_pattern(BankMapping(date_col='Date', amount_col='Amount')) == DEFAULT_PURCHASE_DATE
    own = r'ON (?P<m>\d\d)-(?P<d>\d\d)-(?P<y>\d\d)'
    assert card_date_pattern(BankMapping(date_col='Date', amount_col='Amount', purchase_date_regex=own)) == own
    posted = date(2029, 3, 12)
    assert purchase_date('SHOP DATE 03/09/29', posted, DEFAULT_PURCHASE_DATE) == date(2029, 3, 9)
    assert purchase_date('SHOP ON 03-08-29', posted, own) == date(2029, 3, 8)
    assert purchase_date('SHOP', posted, DEFAULT_PURCHASE_DATE) == posted                 # none in the text
    assert purchase_date('SHOP DATE 03/09/29', posted, None) == posted                    # no pattern
    # a default-pattern date far before the posting is a reference number, not a purchase date
    assert purchase_date('SHOP DATE 01/02/28', posted, DEFAULT_PURCHASE_DATE) == posted
