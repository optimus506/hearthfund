import pytest

import private_support as ps

SECRET_NAME = 'household-secret-workbook.ods'


def skip_reason(fn, *args):
    with pytest.raises(pytest.skip.Exception) as e:
        fn(*args)
    reason = str(e.value)
    assert reason.startswith(ps.PREFIX)
    return reason


def test_skips_when_variable_unset(monkeypatch):
    monkeypatch.delenv(ps.ENV, raising=False)
    assert 'is not set' in skip_reason(ps.private_file, '*.ods')


def test_skips_when_folder_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(ps.ENV, str(tmp_path / 'nope'))
    reason = skip_reason(ps.private_file, '*.ods')
    assert 'not a folder' in reason and str(tmp_path) not in reason


def test_skips_on_empty_folder(monkeypatch, tmp_path):
    monkeypatch.setenv(ps.ENV, str(tmp_path))
    reason = skip_reason(ps.private_file, '*.ods')
    assert 'no *.ods file' in reason and str(tmp_path) not in reason


def test_skips_when_named_file_missing_without_naming_it(monkeypatch, tmp_path):
    (tmp_path / 'other.csv').write_text('x')
    monkeypatch.setenv(ps.ENV, str(tmp_path))
    reason = skip_reason(ps.private_file, SECRET_NAME)
    assert SECRET_NAME not in reason and str(tmp_path) not in reason


def test_skips_on_ambiguous_pattern(monkeypatch, tmp_path):
    (tmp_path / 'a.ods').write_text('x')
    (tmp_path / 'b.ods').write_text('x')
    monkeypatch.setenv(ps.ENV, str(tmp_path))
    assert 'more than one' in skip_reason(ps.private_file, '*.ods')


def test_finds_files(monkeypatch, tmp_path):
    (tmp_path / 'a.ods').write_text('x')
    (tmp_path / 'bank.csv').write_text('x')
    monkeypatch.setenv(ps.ENV, str(tmp_path))
    assert ps.private_file('*.ods').name == 'a.ods'
    assert ps.private_file('bank.csv').name == 'bank.csv'


def test_report_hides_values(monkeypatch):
    monkeypatch.delenv('PRIVATE_SHOW', raising=False)
    assert ps.report('low', True, got=1, want=1) == ''
    assert ps.report('low', False, got=12345, want=67890) == 'low: mismatch'
    with pytest.raises(AssertionError) as e:
        ps.check('end', 12345, 67890)
    assert '12345' not in str(e.value) and '67890' not in str(e.value)
    ps.check('end', 5, 5)


def test_report_shows_values_when_asked(monkeypatch):
    monkeypatch.setenv('PRIVATE_SHOW', '1')
    assert '12345' in ps.report('low', False, got=12345, want=1)
