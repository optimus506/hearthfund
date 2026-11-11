"""The shipped example: examples/bill-system.example.yaml reconciles against examples/sample-bank.csv."""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest
from test_reconcile_flow import upload

from hbudget import cli
from hbudget.db import connection
from webhelpers import set_start

EX = Path(__file__).parents[2] / 'examples'
# hand-checked: start 2500, pay +2400, mortgage -1800, electric -195 (P1, budget 150), online -30 (tracked),
# unknown -25, internet -80, card -405 (P2: fuel 50 + grocery 160 + fuel 35 + grocery 160) -> 2365.00
WANT = {'balance': 236500, 'low': 232500, 'low_date': '2029-03-14', 'end': 3759000}


def import_example(c, **form: str):  # type: ignore[no-untyped-def]
    data = (EX / 'bill-system.example.yaml').read_bytes()
    return c.post('/api/h/%d/import/billsystem' % c.hid, files={'file': ('example.yaml', data)},
                  data={'mode': 'merge', 'dry_run': '0', **form}, headers={'X-CSRF-Token': c.csrf})


def test_example_reconciles_to_known_numbers(admin, app, tmp_path):
    conn = connection.connect(tmp_path / 'hbudget.db')
    set_start(conn, admin.hid, balance=0)        # a new household: the file's balance and as-of are taken
    conn.close()
    r = import_example(admin)
    assert r.status_code == 200, r.text
    assert r.json()['balance'] == {'taken': True, 'start_balance': 250000, 'as_of': '2029-03-01'}
    app.state.clock = lambda: date(2029, 3, 13)
    up = upload(admin, 'sample-bank.csv', (EX / 'sample-bank.csv').read_bytes()).json()
    p = admin.api('POST', '/reconcile/preview', {'upload_id': up['id']}).json()
    assert p['basis']['description'] == 'DEPOSIT'
    assert sorted(a['row']['name'] for a in p['auto']) == ['Internet', 'Mortgage', 'Pay (A)']
    assert [(x['id'], x['kind']) for x in p['proposals']] == [('P1', 'tolerance'), ('P2', 'card_bundle')]
    assert len(p['proposals'][1]['rows']) == 4
    assert [t['rule'] for t in p['tracked']] == ['Online shopping'] and len(p['unknown']) == 1
    r = admin.api('POST', '/reconcile/apply', {'upload_id': up['id'], 'accept': {'P1': 'clear', 'P2': 'clear'},
                                               'fingerprint': p['fingerprint']})
    assert r.status_code == 200, r.text
    s = admin.api('GET', '/summary').json()
    assert {k: s[k] for k in WANT} == WANT


def test_balance_is_kept_unless_asked(admin):
    # the household already has a balance (the fixture sets $1,000): merge keeps it, the dry run says so
    r = import_example(admin, dry_run='1')
    b = r.json()['balance']
    assert b['taken'] is False and b['start_balance'] == 100000 and b['file_start_balance'] == 250000
    r = import_example(admin, dry_run='1', set_balance='1')
    assert r.json()['balance']['taken'] is True and r.json()['balance']['start_balance'] == 250000
    r = import_example(admin)
    assert r.json()['balance']['taken'] is False
    assert admin.api('GET', '/summary').json()['balance'] == 100000
    r = import_example(admin, mode='replace', set_balance='1')
    assert r.json()['balance'] == {'taken': True, 'start_balance': 250000, 'as_of': '2029-03-01'}
    assert admin.api('GET', '/summary').json()['balance'] == 250000


def test_no_basis_keeps_the_upload_waiting(admin):
    import_example(admin, set_balance='1')
    body = (EX / 'sample-bank.csv').read_text().replace(',2500.00', ',2600.00').replace('02/28/2029,DEPOSIT,500.00',
                                                                                        '02/28/2029,DEPOSIT,600.00')
    lines = body.splitlines()
    up = upload(admin, 'x.csv', '\n'.join(lines[:-1]).encode()).json()     # drops the posting that left $2,500
    r = admin.api('POST', '/reconcile/preview', {'upload_id': up['id']})
    assert r.status_code == 400 and r.json()['error'] == 'no_basis'
    u = admin.api('GET', '/uploads').json()['uploads'][0]
    assert u['status'] == 'waiting' and 'checkbook balance' in u['note']


@pytest.fixture
def data(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv('HBUDGET_DATA_DIR', str(tmp_path))
    return tmp_path


def test_cli_example_from_stdin_and_households(data, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.StringIO('a long password\n'))
    assert cli.main(['create-admin', 'boss', '--password-stdin', '--household', 'Home']) == 0

    class Stdin(io.StringIO):
        buffer = io.BytesIO((EX / 'bill-system.example.yaml').read_bytes())
    monkeypatch.setattr('sys.stdin', Stdin())
    assert cli.main(['import-bills', '-', '--household', 'home']) == 0
    assert 'taken from the file' in capsys.readouterr().out
    assert cli.main(['households']) == 0
    assert capsys.readouterr().out.strip() == '1\tHome'
    assert cli.main(['reconcile', str(EX / 'sample-bank.csv'), '--household', '1', '--accept', 'P1,P2']) == 0
    out = capsys.readouterr().out
    assert '$2,365.00' in out
    with pytest.raises(SystemExit, match='No household'):
        cli.main(['export', '--household', 'Elsewhere'])
