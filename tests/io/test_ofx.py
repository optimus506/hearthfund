from __future__ import annotations

from datetime import date

import pytest

from hbudget.io import ImportProblem
from hbudget.io.ofx import read_ofx

SGML = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
CHARSET:1252

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><BANKID>999999999<ACCTID>123456789012<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20290101
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20290103120000.000[-5:EST]<TRNAMT>-100.00<FITID>f2<NAME>ELECTRIC CO
<MEMO>ACCT 987654321</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20290102<TRNAMT>2000.00<FITID>f1<NAME>EMPLOYER A PAYROLL</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20290103<TRNAMT>-4.00<FITID>f2<NAME>DUPLICATE ID</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20290104<TRNAMT>-4.00<NAME>CAF\xc9</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>2896.00<DTASOF>20290105</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""

XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD</CURDEF>
<BANKACCTFROM><BANKID>1</BANKID><ACCTID>123456789012</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20290210</DTPOSTED><TRNAMT>-50.00</TRNAMT><FITID>x1</FITID><NAME>WATER</NAME></STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>950.00</BALAMT><DTASOF>20290201</DTASOF></LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def test_sgml_without_closing_tags() -> None:
    f = read_ofx(SGML)
    assert [t.id for t in f.txns][1:] == ['f2', 'f1']
    assert f.duplicates == 1 and f.has_balance and f.latest_balance == 289600
    newest = f.txns[0]
    assert newest.posted == date(2029, 1, 4) and newest.description == 'CAFÉ' and newest.id.startswith('h:')
    assert [t.balance for t in f.txns] == [289600, 290000, 300000]       # computed backwards from LEDGERBAL
    assert f.txns[1].description == 'ELECTRIC CO ACCT #####4321'


def test_account_id_is_never_stored() -> None:
    for data in (SGML, XML):
        f = read_ofx(data)
        text = repr(f)
        assert '123456789012' not in text and '999999999' not in text


def test_xml_with_ledger_balance_older_than_the_newest_posting() -> None:
    f = read_ofx(XML)
    assert len(f.txns) == 1 and f.txns[0].amount == -5000 and f.txns[0].category == 'DEBIT'
    assert not f.has_balance and f.latest_balance is None


def test_qfx_header_and_intuit_tags() -> None:
    qfx = SGML.replace(b'<CURDEF>USD', b'<CURDEF>USD\n<INTU.BID>3000\n<INTU.USERID>someone')
    assert len(read_ofx(qfx).txns) == 3


def test_errors() -> None:
    with pytest.raises(ImportProblem, match='not an OFX'):
        read_ofx(b'Date,Amount\n')
    with pytest.raises(ImportProblem, match='no transactions'):
        read_ofx(b'<OFX><BANKTRANLIST></BANKTRANLIST></OFX>')
    with pytest.raises(ImportProblem, match='more than one account'):
        read_ofx(b'<OFX><STMTRS></STMTRS><STMTRS></STMTRS></OFX>')
    with pytest.raises(ImportProblem, match='DTPOSTED'):
        read_ofx(b'<OFX><STMTTRN><TRNAMT>-1.00<FITID>a</STMTTRN></OFX>')
    with pytest.raises(ImportProblem, match='TRNAMT'):
        read_ofx(b'<OFX><STMTTRN><DTPOSTED>20290101<TRNAMT>lots</STMTTRN></OFX>')
    with pytest.raises(ImportProblem, match='damaged'):
        read_ofx(b'<?xml version="1.0"?><OFX><STMTTRN></OFX>')
    with pytest.raises(ImportProblem, match='not allowed'):
        read_ofx(b'<?xml version="1.0"?><!DOCTYPE OFX [<!ENTITY a "x">]><OFX>&a;</OFX>')


def test_bad_ledger_balance_is_ignored() -> None:
    f = read_ofx(SGML.replace(b'<BALAMT>2896.00', b'<BALAMT>n/a'))
    assert not f.has_balance


def test_impossible_date_is_an_error() -> None:
    with pytest.raises(ImportProblem, match='DTPOSTED'):
        read_ofx(SGML.replace(b'<DTPOSTED>20290102<', b'<DTPOSTED>20291399<'))


def test_empty_file() -> None:
    with pytest.raises(ImportProblem, match='no transactions'):
        read_ofx(b'')


def test_entities_are_unescaped_and_deep_nesting_refused() -> None:
    f = read_ofx(b'<OFX><STMTTRN><DTPOSTED>20290101<TRNAMT>-1.00<FITID>a<NAME>A &amp; B &lt;X&gt;</STMTTRN></OFX>')
    assert f.txns[0].description == 'A & B <X>'
    with pytest.raises(ImportProblem, match='nested too deeply'):
        read_ofx(b'<OFX>' + b'<AGG>' * 70 + b'</OFX>')
    xml = b'<?xml version="1.0"?><OFX>' + b'<A>' * 70 + b'<B>1</B>' + b'</A>' * 70 + b'</OFX>'
    with pytest.raises(ImportProblem, match='nested too deeply'):
        read_ofx(xml)


@pytest.mark.parametrize(('raw', 'cents'), [('-12,50', -1250), ('1.234,50', 123450), ('1,234.50', 123450),
                                            ('-7.5', -750), ('+3', 300)])
def test_amount_formats(raw: str, cents: int) -> None:
    f = read_ofx(b'<OFX><STMTTRN><DTPOSTED>20290101<TRNAMT>' + raw.encode() + b'<FITID>a</STMTTRN></OFX>')
    assert f.txns[0].amount == cents


@pytest.mark.parametrize('raw', ['1,234', '12,3456', '1,2,3'])
def test_ambiguous_amounts_are_refused(raw: str) -> None:
    with pytest.raises(ImportProblem, match='TRNAMT'):
        read_ofx(b'<OFX><STMTTRN><DTPOSTED>20290101<TRNAMT>' + raw.encode() + b'<FITID>a</STMTTRN></OFX>')
