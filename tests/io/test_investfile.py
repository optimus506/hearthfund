"""Investment files (D-103): dates and totals out of a statement, a history table or a positions table."""
from __future__ import annotations

from datetime import date

import pytest

from hbudget.io import ImportProblem
from hbudget.io.investfile import read_invest_file

D = date

STATEMENT = b"""OFXHEADER:100
DATA:OFXSGML

<OFX><INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>
<DTASOF>20290228120000
<CURDEF>USD
<INVACCTFROM><BROKERID>broker.example<ACCTID>123456789</INVACCTFROM>
<INVPOSLIST>
<POSMF><INVPOS><SECID><UNIQUEID>111<UNIQUEIDTYPE>CUSIP</SECID><UNITS>10<UNITPRICE>100<MKTVAL>1000.00<DTPRICEASOF>20290228</INVPOS></POSMF>
<POSSTOCK><INVPOS><SECID><UNIQUEID>222<UNIQUEIDTYPE>CUSIP</SECID><UNITS>5<UNITPRICE>50.10<MKTVAL>250.50<DTPRICEASOF>20290228</INVPOS></POSSTOCK>
</INVPOSLIST>
<INVBAL><AVAILCASH>49.50<MARGINBALANCE>0<SHORTBALANCE>0</INVBAL>
</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1></OFX>
"""


def test_a_statement_gives_its_date_and_holdings_plus_cash() -> None:
    f = read_invest_file(STATEMENT, 'statement.qfx')
    assert f.kind == 'statement' and len(f.readings) == 1
    r = f.readings[0]
    assert r.points == ((D(2029, 2, 28), 130000),)
    assert (r.holdings, r.cash) == (125050, 4950)
    assert r.label == 'Account ending 6789' and '12345' not in repr(f)


def test_a_plan_statement_uses_its_own_total_and_cash_alone_is_a_balance() -> None:
    plan = STATEMENT.replace(b'<INVBAL>', b'<INV401KBAL><TOTAL>9000.00</INV401KBAL><INVBAL>')
    r = read_invest_file(plan, 'plan.ofx').readings[0]
    assert r.points == ((D(2029, 2, 28), 900000),) and r.cash is None
    start, end = STATEMENT.index(b'<INVPOSLIST>'), STATEMENT.index(b'<INVBAL>')
    r = read_invest_file(STATEMENT[:start] + STATEMENT[end:], 'cash.ofx').readings[0]
    assert r.points == ((D(2029, 2, 28), 4950),) and r.holdings is None


def test_a_bank_statement_gives_its_ledger_balance_and_transactions_alone_are_refused() -> None:
    bank = (b'<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKACCTFROM><ACCTID>998877</BANKACCTFROM>'
            b'<LEDGERBAL><BALAMT>5000.25<DTASOF>20290115</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>')
    r = read_invest_file(bank, 'cd.ofx').readings[0]
    assert (r.label, r.points) == ('Account ending 8877', ((D(2029, 1, 15), 500025),))
    with pytest.raises(ImportProblem, match='no account value'):
        read_invest_file(b'<OFX><INVSTMTRS><DTASOF>20290101</INVSTMTRS></OFX>', 'x.ofx')
    with pytest.raises(ImportProblem):
        read_invest_file(b'Date,Balance\n', 'x.ofx')


def test_a_history_table_gives_one_balance_per_date() -> None:
    f = read_invest_file(b'My plan\n\nDate,Ending Balance,Note\n01/31/2029,"$10,000.00",\n2029-02-28,10500.5,x\n'
                         b'soon,1,\n03/31/2029,,\nTotal,20500.50,\n', 'plan.csv')
    assert f.kind == 'history' and f.skipped == 3
    assert f.readings[0].label == '' and f.readings[0].points == ((D(2029, 1, 31), 1000000), (D(2029, 2, 28), 1050050))


def test_a_history_table_with_a_column_per_account_or_an_account_column() -> None:
    f = read_invest_file(b'Date,Plan at work,Brokerage,Note\n2029-01-31,100,200,a\n2029-02-28,110,,b\n', 'mine.csv')
    assert [(r.label, r.points) for r in f.readings] == [
        ('Plan at work', ((D(2029, 1, 31), 10000), (D(2029, 2, 28), 11000))), ('Brokerage', ((D(2029, 1, 31), 20000),))]
    f = read_invest_file(b'As of;Account;Value\n2029-01-31;IRA 123456789;100\n2029-01-31;Joint;50\n'
                         b'2029-02-28;Joint;60\n', 'two.csv')
    assert [(r.label, len(r.points)) for r in f.readings] == [('IRA #####6789', 1), ('Joint', 2)]


def test_a_positions_table_adds_up_the_values_per_account() -> None:
    f = read_invest_file(
        b'"Positions for all accounts as of 02/28/2029"\n\n'
        b'Account Name,Symbol,Description,Quantity,Price,Current Value\n'
        b'Rollover IRA,AAA,Fund A,10,$100.00,"$1,000.00"\n'
        b'Rollover IRA,BBB,Fund B,5,$50.10,$250.50\n'
        b'Rollover IRA,Pending Activity,,,,\n'
        b'Brokerage,CASH,Cash,,,$40.00\n'
        b'Account Total,,,,,"$1,290.50"\n'
        b'\n"Brokerage services are provided by the broker."\n', 'positions.csv')
    assert f.kind == 'positions' and f.skipped == 2      # the pending line and the total line
    assert [(r.label, r.points) for r in f.readings] == [('Rollover IRA', ((None, 125050),)),
                                                         ('Brokerage', ((None, 4000),))]
    f = read_invest_file(b'Symbol,Shares,Mkt Val (Market Value)\nAAA,1,10.00\nCash & Cash Investments,,5.00\n', 'p.csv')
    assert f.readings[0].label == '' and f.readings[0].points == ((None, 1500),)


def test_files_without_a_table_or_values_are_refused() -> None:
    for data in (b'hello\nworld\n', b'Name,Colour\na,b\n', b'Date,Note\n2029-01-01,x\n', b'Symbol,Value\nAAA,n/a\n'):
        with pytest.raises(ImportProblem):
            read_invest_file(data, 'x.csv')
