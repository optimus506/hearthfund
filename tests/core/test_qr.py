"""The QR encoder (D-095). The two hashes are of codes checked cell by cell against an independent encoder
(all eight masks, versions 1 to 10) when this module was written; they pin the output."""
import hashlib

import pytest

from hbudget.core import qr

LINK = b'otpauth://totp/Example:alex?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Example'


def digest(m: list[list[int]]) -> str:
    return hashlib.sha256(bytes(c for row in m for c in row)).hexdigest()


def test_known_codes() -> None:
    small = qr.matrix(b'HELLO', mask=0)
    assert len(small) == 21 and digest(small) == 'af4cd98e64336cad58d26d0b62e8aef3de27f2d76185994291db885ed6d41952'
    link = qr.matrix(LINK, mask=2)
    assert len(link) == 37 and digest(link) == '86cda82490c775984615c4db189d8f5f49fb14ae2c855ef6a9fb3fd6fe98a322'


def test_structure_and_sizes() -> None:
    finder = [[1, 1, 1, 1, 1, 1, 1], [1, 0, 0, 0, 0, 0, 1], [1, 0, 1, 1, 1, 0, 1], [1, 0, 1, 1, 1, 0, 1],
              [1, 0, 1, 1, 1, 0, 1], [1, 0, 0, 0, 0, 0, 1], [1, 1, 1, 1, 1, 1, 1]]
    # the largest payload of each version: 14 bytes fit version 1, 15 need version 2, ...
    for version, most in enumerate((14, 26, 42, 62, 84, 106, 122, 152, 180, 213), 1):
        for n, want in ((most, version), (most + 1, version + 1)):
            if n > qr.MAX_BYTES:
                continue
            m = qr.matrix(b'x' * n)
            size = 17 + 4 * want
            assert len(m) == size and all(len(row) == size for row in m)
            for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
                assert [row[c0:c0 + 7] for row in m[r0:r0 + 7]] == finder
            assert [m[6][i] for i in range(8, size - 8)] == [(i + 1) % 2 for i in range(8, size - 8)]   # timing
            assert m[size - 8][8] == 1                                                                  # dark module
    assert qr.MAX_BYTES == 213
    with pytest.raises(ValueError, match='at most 213 bytes'):
        qr.matrix(b'x' * 214)
    assert len(qr.matrix(b'')) == 21
    # every mask gives a different, valid-sized code; the automatic choice is one of them
    all_masks = [qr.matrix(LINK, mask=k) for k in range(8)]
    assert len({digest(m) for m in all_masks}) == 8 and qr.matrix(LINK) in all_masks


def test_svg_is_black_on_white_with_a_quiet_zone() -> None:
    svg = qr.svg(LINK)
    assert svg.startswith('<svg ') and 'viewBox="0 0 45 45"' in svg            # 37 modules + 4 either side
    assert 'fill="#fff"' in svg and 'fill="#000"' in svg and 'style=' not in svg and '<script' not in svg
    assert svg.count('h1v1h-1z') == sum(map(sum, qr.matrix(LINK)))
