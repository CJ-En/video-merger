"""Makes the Windows program's icon (video-merger.ico) from the tool's mark, with Python alone (no image library).

The mark is the one in the page's top bar and favicon.svg, on a 16 x 16 grid: a rounded tile in the accent color
with two parts, an arrow and the joined video in the dark ink color. Each size is drawn with supersampling (several
samples per pixel, so the edges are smooth), stored as a PNG, and the PNGs are packed into one .ico (Windows Vista
and later read PNG images in icons).

    python packaging/windows/make_icon.py [output.ico]
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)
TILE = (0x5E, 0xC4, 0xFA)  # the accent color (favicon.svg, page --ui-accent in Dark)
INK = (0x04, 0x12, 0x1F)  # on the accent (--ui-on-accent in Dark)
RADIUS = 2.0  # the tile's corners: M2 0L14 0A2 2 0 0 1 16 2 ... (favicon.svg)
# The mark: polygons on the 16 x 16 grid, the same points as the ink path in favicon.svg and index.html.
INK_SHAPES = (
    ((2.5, 3.5), (7, 3.5), (7, 7), (2.5, 7)),  # part 1
    ((2.5, 9), (7, 9), (7, 12.5), (2.5, 12.5)),  # part 2
    ((8.25, 5.5), (11, 8), (8.25, 10.5)),  # the arrow
    ((11.75, 3.5), (13.5, 3.5), (13.5, 12.5), (11.75, 12.5)),  # the joined video
)
OUTPUT = Path(__file__).resolve().with_name("video-merger.ico")


def in_tile(x: float, y: float) -> bool:
    if not (0 <= x <= 16 and 0 <= y <= 16):
        return False
    cx = min(max(x, RADIUS), 16 - RADIUS)  # the nearest point of the inner square: the corners are round
    cy = min(max(y, RADIUS), 16 - RADIUS)
    return (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS ** 2


def in_polygon(x: float, y: float, points) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def render(size: int) -> bytes:
    """RGBA pixels, rows top to bottom (non-premultiplied, as PNG stores them)."""
    samples = 8 if size <= 48 else 4  # per side of a pixel
    scale = 16 / size
    rows = bytearray()
    for py in range(size):
        for px in range(size):
            tile = ink = 0
            for sy in range(samples):
                y = (py + (sy + 0.5) / samples) * scale
                for sx in range(samples):
                    x = (px + (sx + 0.5) / samples) * scale
                    if in_tile(x, y):
                        tile += 1
                        if any(in_polygon(x, y, shape) for shape in INK_SHAPES):
                            ink += 1
            if not tile:
                rows += b"\0\0\0\0"
                continue
            # Ink over the tile: the color is their mix inside the tile, the tile's coverage is the alpha.
            rows += bytes(round(((tile - ink) * t + ink * i) / tile) for t, i in zip(TILE, INK))
            rows.append(round(255 * tile / samples ** 2))
    return bytes(rows)


def png(size: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    stride = size * 4
    raw = b"".join(b"\0" + rgba[row * stride:(row + 1) * stride] for row in range(size))  # filter "none" per row
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 bits, RGBA, no interlace
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(images: dict[int, bytes]) -> bytes:
    """An .ico holding one PNG per size (width and height 0 mean 256)."""
    entries, offset = b"", 6 + 16 * len(images)
    for size, data in images.items():
        side = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(images)) + entries + b"".join(images.values())


def make(path: Path = OUTPUT, sizes=SIZES) -> Path:
    path.write_bytes(ico({size: png(size, render(size)) for size in sizes}))
    return path


if __name__ == "__main__":
    print(make(Path(sys.argv[1]) if len(sys.argv) > 1 else OUTPUT))
