from __future__ import annotations

import pytest

from hbudget.io import ImportProblem, csvfile


def test_bom_and_utf8() -> None:
    rows = csvfile.read_rows('﻿Date,Description\n2029-01-02,Café\n'.encode())
    assert rows == [['Date', 'Description'], ['2029-01-02', 'Café']]


def test_windows_1252_fallback() -> None:
    assert csvfile.decode('Café €5'.encode('cp1252')) == 'Café €5'
    assert csvfile.decode(b'\x81\x8d') == '\x81\x8d'   # bytes undefined in cp1252: latin-1


def test_semicolon_and_blank_lines() -> None:
    rows = csvfile.read_rows(b'a;b;c\n\n1;2;3\n ; ; \n')
    assert rows == [['a', 'b', 'c'], ['1', '2', '3']]


def test_ragged_rows_are_kept() -> None:
    assert csvfile.read_rows(b'a,b,c\n1,2\n1,2,3,4\n') == [['a', 'b', 'c'], ['1', '2'], ['1', '2', '3', '4']]


def test_binary_data_rejected() -> None:
    with pytest.raises(ImportProblem, match='binary'):
        csvfile.read_rows(b'a,b\n1,\x00\n')


def test_oversize_field() -> None:
    with pytest.raises(ImportProblem, match='longer than') as e:
        csvfile.read_rows(b'a\n' + b'x' * 10_001 + b'\n')
    assert e.value.line == 2


def test_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(csvfile, 'MAX_ROWS', 3)
    with pytest.raises(ImportProblem, match='more than 3 rows'):
        csvfile.read_rows(b'a\n1\n2\n3\n')


def test_broken_quoting() -> None:
    with pytest.raises(ImportProblem, match='could not be read'):
        csvfile.read_rows(b'a,b\n"x' + b'y' * 200_000 + b'\n')


def test_read_csv_empty_and_name() -> None:
    with pytest.raises(ImportProblem, match='empty'):
        csvfile.read_csv(b'\n\n', 'x.csv')
    sheet = csvfile.read_csv(b'a,b\n1,2\n', 'folder/bank.csv')
    assert sheet.name == 'bank.csv' and sheet.rows[1][1].text == '2'
