"""Draw the app icon (static/icon.svg) as PNGs for phones and the web app manifest. Pure Python, no image library:
a rounded square with the rising line, supersampled 4x for smooth edges. Run after changing the icon:

    python scripts/make_icons.py
"""
from __future__ import annotations

import itertools
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / 'src' / 'hbudget' / 'web' / 'static'
BLUE, WHITE = (0x3F, 0x6F, 0xB0), (0xFF, 0xFF, 0xFF)
LINE = [(7, 22), (13, 15), (18, 19), (25, 10)]      # the SVG path, in its 32x32 box
SS = 4


def _seg_dist2(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def draw(size: int, *, full_bleed: bool = False) -> bytes:
    """RGBA rows. full_bleed fills the square (for maskable and apple icons, which round it themselves)."""
    scale = size / 32
    radius = 0 if full_bleed else 7
    half = 1.5            # stroke width 3
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            bg = line = 0
            for sy in range(SS):
                for sx in range(SS):
                    u, v = (x + (sx + .5) / SS) / scale, (y + (sy + .5) / SS) / scale
                    qx, qy = min(u, 32 - u), min(v, 32 - v)
                    inside = not (qx < radius and qy < radius and (radius - qx) ** 2 + (radius - qy) ** 2 > radius ** 2)
                    if not inside:
                        continue
                    bg += 1
                    if min(_seg_dist2(u, v, a, b) for a, b in itertools.pairwise(LINE)) <= half * half:
                        line += 1
            n = SS * SS
            if bg == 0:
                row += bytes((0, 0, 0, 0))
                continue
            f = line / bg
            rgb = [round(BLUE[i] * (1 - f) + WHITE[i] * f) for i in range(3)]
            row += bytes((*rgb, round(255 * bg / n)))
        rows.append(bytes(row))
    return png(size, rows)


def png(size: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b''.join(b'\x00' + r for r in rows)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


if __name__ == '__main__':
    for name, size, bleed in (('icon-192.png', 192, False), ('icon-512.png', 512, False),
                              ('icon-maskable-512.png', 512, True), ('apple-touch-icon.png', 180, True)):
        (OUT / name).write_bytes(draw(size, full_bleed=bleed))
        print('wrote', name)
