"""Generate static_pages/icon.png (512x512): a rain cloud with three drops over a green ball field.

Pure Python (zlib + struct), no third-party art or libraries, deterministic output.
Run: python scripts/make_icon.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZE = 512
OUT = Path(__file__).resolve().parents[1] / "static_pages" / "icon.png"

SKY = (11, 99, 206)       # blue background
CLOUD = (245, 247, 250)   # off-white
DROP = (120, 196, 255)    # light blue
GRASS = (46, 160, 67)     # green field
DIRT = (196, 140, 82)     # infield
LINE = (255, 255, 255)


def _in_circle(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _in_drop(x, y, cx, cy, r):
    # round bottom plus a pointed top
    if _in_circle(x, y, cx, cy, r):
        return True
    if cy - 2.2 * r <= y <= cy:
        half = r * (y - (cy - 2.2 * r)) / (2.2 * r)
        return abs(x - cx) <= half
    return False


def pixel(x: int, y: int) -> tuple[int, int, int]:
    # rounded-square mask is left to the platforms; background fills the square
    # field: a wide arc at the bottom
    if _in_circle(x, y, 256, 900, 520):
        if _in_circle(x, y, 256, 900, 470) and not _in_circle(x, y, 256, 900, 455):
            return LINE
        if _in_circle(x, y, 256, 520, 70):
            return DIRT
        return GRASS
    cloud = (
        _in_circle(x, y, 190, 200, 70) or _in_circle(x, y, 270, 165, 90)
        or _in_circle(x, y, 345, 210, 65) or (130 <= x <= 400 and 200 <= y <= 270
                                              and (_in_circle(x, y, 160, 240, 30) or _in_circle(x, y, 380, 240, 30) or 160 <= x <= 380))
    )
    if cloud:
        return CLOUD
    for cx in (190, 265, 340):
        if _in_drop(x, y, cx, 345, 18):
            return DROP
    return SKY


def render() -> bytes:
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)  # filter: none
        for x in range(SIZE):
            raw.extend(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(render())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
