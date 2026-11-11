from __future__ import annotations

import io
import zipfile

import pytest

from hbudget.io import ImportProblem, clean_filename, limits


def _zip(members: dict[str, bytes], *, compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=compress) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_check_size_rejects_oversize_upload() -> None:
    limits.check_size(b'x' * 10, limit=10)
    with pytest.raises(ImportProblem, match='larger than'):
        limits.check_size(b'x' * (2 * 1024 * 1024 + 1), limit=2 * 1024 * 1024)


def test_open_zip_reads_normal_file() -> None:
    z = limits.open_zip(_zip({'a.xml': b'<a/>'}))
    assert limits.read_member(z, 'a.xml') == b'<a/>'


def test_zip_bomb_by_real_size_ratio() -> None:
    bomb = _zip({'content.xml': b'\0' * (3 * 1024 * 1024)})
    with pytest.raises(ImportProblem, match='zip bomb'):
        limits.open_zip(bomb)


def test_zip_total_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, 'MAX_UNCOMPRESSED', 1000)
    data = _zip({'a': b'x' * 600, 'b': b'y' * 600}, compress=zipfile.ZIP_STORED)
    with pytest.raises(ImportProblem, match='too large when unpacked'):
        limits.open_zip(data)


def test_too_many_zip_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, 'MAX_ZIP_MEMBERS', 3)
    with pytest.raises(ImportProblem, match='too many parts'):
        limits.open_zip(_zip({str(i): b'' for i in range(4)}))


def test_damaged_zip() -> None:
    with pytest.raises(ImportProblem, match='damaged'):
        limits.open_zip(b'PK\x03\x04 not really a zip')


def test_missing_member() -> None:
    z = limits.open_zip(_zip({'a': b'1'}))
    with pytest.raises(ImportProblem, match='missing'):
        limits.read_member(z, 'content.xml')


def test_clean_filename_masks_numbers_and_paths() -> None:
    assert clean_filename('../../etc/Export_123456789012.csv') == 'Export_########9012.csv'
    assert clean_filename('acct_4000_1111_2222_3333.csv') == 'acct_####_####_####_3333.csv'
    assert clean_filename('card+4000,1111+2222,3333.csv') == 'card_####_####_####_3333.csv'
    assert clean_filename('C:\\Users\\x\\bank <export>.csv') == 'bank _export_.csv'
    assert clean_filename('') == 'upload'
