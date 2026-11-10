"""SCUMM charsets (CHAR blocks): 1, 2, 4 or 8 bits per pixel, MSB first."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

HEADER = 21      # size, 2 unknown bytes and the 15-entry colour map


@dataclass
class Glyph:
    code: int
    width: int
    height: int
    x_offset: int
    y_offset: int
    pixels: np.ndarray = field(repr=False)     # colour numbers, 0 == clear


@dataclass
class Charset:
    number: int
    height: int
    bits: int
    colors: bytes
    glyphs: dict[int, Glyph]


def parse_charset(number: int, block) -> Charset:
    p = block.payload
    colors = p[6:HEADER]
    bits, height, count = struct.unpack_from("<BBH", p, HEADER)
    offsets = struct.unpack_from(f"<{count}I", p, HEADER + 4)
    glyphs = {}
    for code, offset in enumerate(offsets):
        if not offset:
            continue
        pos = HEADER + offset
        w, h, xo, yo = struct.unpack_from("<BBbb", p, pos)
        size = (w * h * bits + 7) // 8
        raw = np.unpackbits(np.frombuffer(p, np.uint8, size, pos + 4))
        values = raw[:w * h * bits].reshape(-1, bits)
        pixels = values.dot(1 << np.arange(bits - 1, -1, -1)).astype(np.uint8)
        glyphs[code] = Glyph(code, w, h, xo, yo, pixels.reshape(h, w))
    return Charset(number, height, bits, colors, glyphs)
