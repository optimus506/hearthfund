"""The command line on a temporary data dir (same db and core code as the web app)."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from hbudget import cli

FIX = Path(__file__).parents[1] / 'fixtures'


@pytest.fixture
def data(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv('HBUDGET_DATA_DIR', str(tmp_path))
    return tmp_path


def run(*args: str) -> int:
    return cli.main(list(args))


def test_migrate_create_admin_import_reconcile_export(data, monkeypatch, capsys):
    assert run('migrate') == 0
    monkeypatch.setattr('sys.stdin', io.StringIO('a long password\n'))
    assert run('create-admin', 'boss', '--password-stdin') == 0
    monkeypatch.setattr('sys.stdin', io.StringIO('a long password\n'))
    with pytest.raises(SystemExit):
        run('create-admin', 'other', '--password-stdin')
    monkeypatch.setattr('sys.stdin', io.StringIO('new long password\n'))
    assert run('create-admin', 'boss', '--reset-password', '--password-stdin') == 0
    hh = str(FIX / 'synthetic' / 'household.yaml')
    assert run('import-bills', hh, '--dry-run') == 0
    assert run('import-bills', hh, '--replace') == 0
    bank = str(FIX / 'synthetic' / 'bank.csv')
    capsys.readouterr()
    assert run('reconcile', bank) == 0
    out = capsys.readouterr().out
    assert 'dry run' in out
    assert run('reconcile', bank, '--accept', 'P1:clear,P2:clear', '--write') == 0
    assert run('reconcile', bank) == 2          # nothing new now
    target = data / 'out.yaml'
    assert run('export', '-o', str(target)) == 0
    assert 'start_balance: 635' in target.read_text()
    assert run('export', '-o', str(target)) == 2     # never overwrites
    assert run('export', '--format', 'csv', '-o', str(data / 'f.csv')) == 0
    with pytest.raises(SystemExit):
        run('wipe', '--household', '1')
    assert run('wipe', '--household', '1', '--yes') == 0


def test_check_workbook(data, capsys):
    import json

    from hbudget.io import billsystem, tabular, workbook_import
    path = FIX / 'workbook' / 'checkbook.xlsx'
    guess = workbook_import.guess_mapping(tabular.read_table(path.read_bytes(), path.name))
    rc = run('check-workbook', str(path), '--mapping', json.dumps(billsystem._mapping_doc(guess)))
    out = capsys.readouterr().out
    assert rc == 0 and 'match' in out and 'MISMATCH' not in out, out


def test_unknown_household(data):
    run('migrate')
    with pytest.raises(SystemExit):
        run('export', '--household', '42')


def guess_mapping_json() -> str:
    import json

    from hbudget.io import billsystem, tabular, workbook_import
    path = FIX / 'workbook' / 'checkbook.xlsx'
    return json.dumps(billsystem._mapping_doc(workbook_import.guess_mapping(tabular.read_table(path.read_bytes(),
                                                                                              path.name))))


def test_import_workbook_dry_run_and_real(data, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.StringIO('a long password\n'))
    run('create-admin', 'boss', '--password-stdin')
    book = str(FIX / 'workbook' / 'checkbook.xlsx')
    assert run('import-workbook', book, '--mapping', guess_mapping_json(), '--dry-run') == 0
    out = capsys.readouterr().out
    assert 'rows' in out and 'saved' not in out
    assert run('import-workbook', book, '--mapping', guess_mapping_json()) == 0
    assert 'saved into household 1' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run('import-workbook', book, '--profile', 'missing')


def test_mapping_json_text_never_touches_the_file_system(data, monkeypatch, capsys, tmp_path):
    # Python 3.12/3.13 raise ENAMETOOLONG from Path.exists() for long JSON text (3.14 returns False);
    # simulate that so this test fails on any version if the CLI hands JSON text to the file system again.
    real_exists = Path.exists

    def boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if len(self.name) > 255:
            raise OSError(36, 'File name too long')
        return real_exists(self, *args, **kwargs)
    monkeypatch.setattr('sys.stdin', io.StringIO('a long password\n'))
    run('create-admin', 'boss', '--password-stdin')
    book = str(FIX / 'workbook' / 'checkbook.xlsx')
    mapping = guess_mapping_json()
    with monkeypatch.context() as m:
        m.setattr(Path, 'exists', boom)
        assert run('check-workbook', book, '--mapping', mapping) == 0
        assert run('import-workbook', book, '--mapping', mapping, '--dry-run') == 0
    # the file form still works
    f = tmp_path / 'mapping.json'
    f.write_text(mapping)
    assert run('check-workbook', book, '--mapping', str(f)) == 0
    assert 'MISMATCH' not in capsys.readouterr().out


def test_create_admin_error_paths(data, monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO('short\n'))
    with pytest.raises(SystemExit, match='at least 10'):
        run('create-admin', 'boss', '--password-stdin')
    monkeypatch.setattr('sys.stdin', io.StringIO('a long password\n'))
    with pytest.raises(SystemExit, match='No user'):
        run('create-admin', 'nobody', '--reset-password', '--password-stdin')


def test_demo_command(data, capsys):
    assert run('demo', '--household', 'Try me') == 0
    assert 'Created demo household 1' in capsys.readouterr().out
    assert run('households') == 0
    assert 'Try me' in capsys.readouterr().out


def test_bills_csv_from_stdin(data, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.StringIO('a long password\n'))
    run('create-admin', 'boss', '--password-stdin')

    class Stdin(io.StringIO):
        buffer = io.BytesIO((Path(__file__).parents[2] / 'examples' / 'bills.example.csv').read_bytes())
    monkeypatch.setattr('sys.stdin', Stdin())
    assert run('import-bills', '-') == 0
    assert 'imported 6 bills' in capsys.readouterr().out


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_password_stdin_strips_any_line_ending(data, monkeypatch, ending):
    from hbudget.db import connection, users
    monkeypatch.setattr('sys.stdin', io.StringIO('a long password' + ending))
    assert run('create-admin', 'boss', '--password-stdin') == 0
    conn = connection.connect(data / 'hbudget.db')
    try:
        assert users.verify_login(conn, 'boss', 'a long password') is not None
    finally:
        conn.close()


def test_serve_and_commands_refuse_a_newer_database(data, monkeypatch, capsys):
    from hbudget.db import migrate
    assert run('migrate') == 0
    every = migrate.available()
    monkeypatch.setattr(migrate, 'available', lambda: every[:-1])
    capsys.readouterr()
    assert run('serve') == 2 and run('households') == 2                    # no server is started
    err = capsys.readouterr().err
    assert err.count('error: This database was last used by a newer version of the app') == 2
