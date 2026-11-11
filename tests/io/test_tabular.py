from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from hbudget.io import ImportProblem
from hbudget.io.tabular import Cell, Sheet, cell_at, col_index, col_letter, read_table, split_ref


def test_col_letters_round_trip() -> None:
    assert [col_letter(i) for i in (0, 25, 26, 51, 701, 702)] == ['A', 'Z', 'AA', 'AZ', 'ZZ', 'AAA']
    for i in (0, 5, 26, 300, 1000):
        assert col_index(col_letter(i)) == i
    assert col_index(' b ') == 1
    with pytest.raises(ValueError):
        col_index('A1')
    with pytest.raises(ValueError):
        col_letter(-1)


def test_split_ref_and_cell_at() -> None:
    assert split_ref('B1') == (1, 1)
    assert split_ref('$C$4') == (4, 2)
    with pytest.raises(ValueError):
        split_ref('nope')
    s = Sheet('S', ((Cell('a', 'a'), Cell(1, '1')),))
    assert cell_at(s, 1, 1) == Cell(1, '1')
    assert cell_at(s, 2, 0) is None
    assert cell_at(s, 1, 5) is None
    assert cell_at(s, 0, 0) is None


def test_read_table_dispatch(fixtures: Path) -> None:
    for ext in ('ods', 'xlsx', 'csv'):
        sheets = read_table((fixtures / 'workbook' / ('checkbook.' + ext)).read_bytes(), 'checkbook.' + ext)
        assert sheets[0].rows, ext


def test_read_table_rejects_empty_and_wrong_files() -> None:
    with pytest.raises(ImportProblem, match='empty'):
        read_table(b'  \n', 'x.csv')
    with pytest.raises(ImportProblem, match='not a valid xlsx'):
        read_table(b'hello', 'x.xlsx')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('readme.txt', 'hi')
    with pytest.raises(ImportProblem, match=r'not an \.ods or \.xlsx'):
        read_table(buf.getvalue(), 'x.zip')
