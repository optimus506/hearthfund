"""A small QR code encoder: byte mode, error correction level M, versions 1 to 10 (up to 213 bytes).

Enough for an otpauth:// link (D-095). The app loads nothing from the internet, so the code is drawn by the
server; this keeps that to the standard library. `matrix` gives the modules, `svg` a picture of them.
ISO/IEC 18004: the numbers below (codeword counts, alignment positions, generator polynomials) are from it.
"""
from __future__ import annotations

__all__ = ['MAX_BYTES', 'matrix', 'svg']

# version -> (error-correction codewords per block, [(blocks, data codewords per block), ...]) for level M
_BLOCKS: dict[int, tuple[int, tuple[tuple[int, int], ...]]] = {
    1: (10, ((1, 16),)), 2: (16, ((1, 28),)), 3: (26, ((1, 44),)), 4: (18, ((2, 32),)), 5: (24, ((2, 43),)),
    6: (16, ((4, 27),)), 7: (18, ((4, 31),)), 8: (22, ((2, 38), (2, 39))), 9: (22, ((3, 36), (2, 37))),
    10: (26, ((4, 43), (1, 44))),
}
_ALIGN = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34), 7: (6, 22, 38), 8: (6, 24, 42),
          9: (6, 26, 46), 10: (6, 28, 50)}
_MASKS = (
    lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0, lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0, lambda r, c: r * c % 2 + r * c % 3 == 0,
    lambda r, c: (r * c % 2 + r * c % 3) % 2 == 0, lambda r, c: ((r + c) % 2 + r * c % 3) % 2 == 0,
)


def _data_codewords(version: int) -> int:
    return sum(n * size for n, size in _BLOCKS[version][1])


def _count_bits(version: int) -> int:
    return 8 if version < 10 else 16


MAX_BYTES = _data_codewords(10) - 3

# GF(256) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _generator(degree: int) -> list[int]:
    g = [1]
    for i in range(degree):
        nxt = [0] * (len(g) + 1)
        for j, coef in enumerate(g):
            nxt[j] ^= coef
            if coef:
                nxt[j + 1] ^= _EXP[_LOG[coef] + i]
        g = nxt
    return g


def _remainder(data: list[int], degree: int) -> list[int]:
    g = _generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = [*rem[1:], 0]
        if factor:
            for i in range(degree):
                if g[i + 1]:
                    rem[i] ^= _EXP[_LOG[g[i + 1]] + _LOG[factor]]
    return rem


def _codewords(data: bytes, version: int) -> list[int]:
    """Data and error-correction codewords, interleaved by block, in the order they are placed."""
    capacity = _data_codewords(version)
    bits: list[int] = [0, 1, 0, 0]                                        # byte mode
    n = _count_bits(version)
    bits += [(len(data) >> (n - 1 - i)) & 1 for i in range(n)]
    for byte in data:
        bits += [(byte >> (7 - i)) & 1 for i in range(8)]
    bits += [0] * min(4, capacity * 8 - len(bits))                         # terminator
    bits += [0] * (-len(bits) % 8)
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    words += [pad[i % 2] for i in range(capacity - len(words))]
    ec_len, groups = _BLOCKS[version]
    blocks: list[list[int]] = []
    at = 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[at:at + size])
            at += size
    ec = [_remainder(b, ec_len) for b in blocks]
    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_len):
        out += [e[i] for e in ec]
    return out


def _bch(value: int, bits: int, poly: int) -> int:
    """`value` followed by its BCH remainder (`bits` check bits, generator `poly`)."""
    rem = value << bits
    top = poly.bit_length() - 1
    for shift in range(rem.bit_length() - 1, top - 1, -1):
        if rem >> shift & 1:
            rem ^= poly << (shift - top)
    return value << bits | rem


def _blank(version: int) -> tuple[list[list[int]], list[list[bool]]]:
    """The fixed patterns of a version: (modules, which cells are reserved and not for data)."""
    size = 17 + 4 * version
    m = [[0] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(r: int, c: int, dark: bool) -> None:
        m[r][c] = int(dark)
        fixed[r][c] = True

    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):                   # finders with their separators
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < size and 0 <= cc < size:
                    ring = max(abs(r - 3), abs(c - 3))
                    put(rr, cc, ring <= 1 or ring == 3)
    for i in range(8, size - 8):                                             # timing
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    centres = _ALIGN[version]
    for r0 in centres:                                                       # alignment
        for c0 in centres:
            if (r0, c0) in ((6, 6), (6, centres[-1]), (centres[-1], 6)):     # the finders are there
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    put(r0 + r, c0 + c, max(abs(r), abs(c)) != 1)
    put(size - 8, 8, True)                                                   # the dark module
    for i in range(9):                                                       # format information (filled in later)
        for r, c in ((8, i), (i, 8)):
            fixed[r][c] = True
    for i in range(8):
        fixed[8][size - 1 - i] = True
        fixed[size - 1 - i][8] = True
    if version >= 7:                                                         # version information
        info = _bch(version, 12, 0x1F25)
        for i in range(18):
            dark = bool(info >> i & 1)
            put(i // 3, size - 11 + i % 3, dark)
            put(size - 11 + i % 3, i // 3, dark)
    return m, fixed


def _place(m: list[list[int]], fixed: list[list[bool]], words: list[int]) -> None:
    size = len(m)
    bits = [(w >> (7 - i)) & 1 for w in words for i in range(8)]
    at = 0
    col = size - 1
    up = True
    while col > 0:
        if col == 6:                         # the vertical timing column is skipped
            col -= 1
        for r in (range(size - 1, -1, -1) if up else range(size)):
            for c in (col, col - 1):
                if not fixed[r][c]:
                    m[r][c] = bits[at] if at < len(bits) else 0
                    at += 1
        up = not up
        col -= 2


def _format(m: list[list[int]], mask: int) -> None:
    size = len(m)
    info = _bch(mask, 10, 0x537) ^ 0x5412       # level M is 00, so the five data bits are just the mask
    bits = [info >> i & 1 for i in range(15)]     # bit 0 first
    for i in range(6):
        m[i][8] = bits[i]
    m[7][8], m[8][8], m[8][7] = bits[6], bits[7], bits[8]
    for i in range(9, 15):
        m[8][14 - i] = bits[i]
    for i in range(8):
        m[8][size - 1 - i] = bits[i]
    for i in range(8, 15):
        m[size - 15 + i][8] = bits[i]


def _penalty(m: list[list[int]]) -> int:
    size = len(m)
    score = 0
    lines = [row for row in m] + [[m[r][c] for r in range(size)] for c in range(size)]
    for line in lines:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
        for i in range(size - 10):
            window = line[i:i + 11]
            if window in ([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]):
                score += 40
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(map(sum, m))
    score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
    return score


def matrix(data: bytes, *, mask: int | None = None) -> list[list[int]]:
    """The modules of the smallest code that holds `data` (1 = dark), without the quiet zone. The mask with the
    lowest penalty is used unless one is given."""
    version = next((v for v in _BLOCKS if len(data) <= _data_codewords(v) - (2 if v < 10 else 3)), None)
    if version is None:
        raise ValueError('at most %d bytes fit in the QR codes this module draws' % MAX_BYTES)
    blank, fixed = _blank(version)
    _place(blank, fixed, _codewords(data, version))
    best: tuple[int, list[list[int]]] | None = None
    for k in ([mask] if mask is not None else range(8)):
        m = [[cell ^ int(not fixed[r][c] and _MASKS[k](r, c)) for c, cell in enumerate(row)]
             for r, row in enumerate(blank)]
        _format(m, k)
        score = _penalty(m)
        if best is None or score < best[0]:
            best = (score, m)
    assert best is not None
    return best[1]


def svg(data: bytes, *, scale: int = 5, quiet: int = 4) -> str:
    """An inline SVG of the code: black on white whatever the page's theme, with the quiet zone scanners need."""
    m = matrix(data)
    size = len(m) + 2 * quiet
    path = ''.join('M%d %dh1v1h-1z' % (c + quiet, r + quiet) for r, row in enumerate(m) for c, v in enumerate(row) if v)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
            'aria-label="QR code for the authenticator app" shape-rendering="crispEdges">'
            '<rect width="100%%" height="100%%" fill="#fff"/><path d="%s" fill="#000"/></svg>'
            % (size, size, size * scale, size * scale, path))
