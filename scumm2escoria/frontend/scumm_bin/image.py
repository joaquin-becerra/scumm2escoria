"""Decoders for SCUMM v5 room and object graphics (SMAP strips, ZPnn masks)."""
from __future__ import annotations

import struct

import numpy as np
from PIL import Image

STRIP_W = 8


class _Bits:
    """LSB-first bit reader."""
    __slots__ = ("data", "pos", "acc", "count")

    def __init__(self, data: bytes, pos: int):
        self.data, self.pos, self.acc, self.count = data, pos, 0, 0

    def read(self, n: int) -> int:
        while self.count < n:
            byte = self.data[self.pos] if self.pos < len(self.data) else 0
            self.acc |= byte << self.count
            self.pos += 1
            self.count += 8
        value = self.acc & ((1 << n) - 1)
        self.acc >>= n
        self.count -= n
        return value


def _basic(data: bytes, pos: int, total: int, shift: int) -> bytearray:
    """Delta coding: keep colour, new colour, or step by a signed increment."""
    out = bytearray(total)
    color = data[pos]
    bits = _Bits(data, pos + 1)
    inc = -1
    for i in range(total):
        out[i] = color
        if bits.read(1):
            if not bits.read(1):
                color = bits.read(shift)
                inc = -1
            else:
                if bits.read(1):
                    inc = -inc
                color = (color + inc) & 0xFF
    return out


def _complex(data: bytes, pos: int, total: int, shift: int) -> bytearray:
    """Like _basic, with 3-bit colour deltas and explicit run lengths."""
    out = bytearray(total)
    color = data[pos]
    bits = _Bits(data, pos + 1)
    i = 0
    while i < total:
        out[i] = color
        i += 1
        while i < total:
            if not bits.read(1):
                break
            if not bits.read(1):
                color = bits.read(shift)
                break
            delta = bits.read(3) - 4
            if delta:
                color = (color + delta) & 0xFF
                break
            run = bits.read(8)
            for _ in range(run):
                if i >= total:
                    break
                out[i] = color
                i += 1
    return out


def decode_strip(data: bytes, pos: int, height: int) -> tuple[np.ndarray, bool]:
    """Decode one 8-pixel-wide strip. Returns (height x 8 indexes, transparent?)."""
    code = data[pos]
    pos += 1
    total = height * STRIP_W
    shift = code % 10
    vertical = False
    transparent = False
    if code in (1, 8):                       # raw
        out = bytearray(data[pos:pos + total])
        transparent = code == 8
    elif 14 <= code <= 18 or 34 <= code <= 38:
        out, vertical = _basic(data, pos, total, shift), True
        transparent = code >= 34
    elif 24 <= code <= 28 or 44 <= code <= 48:
        out = _basic(data, pos, total, shift)
        transparent = code >= 44
    elif 64 <= code <= 68 or 104 <= code <= 108:
        out = _complex(data, pos, total, shift)
    elif 84 <= code <= 88 or 124 <= code <= 128:
        out, transparent = _complex(data, pos, total, shift), True
    else:
        raise ValueError(f"unknown strip code {code}")
    arr = np.frombuffer(bytes(out), dtype=np.uint8)
    if vertical:
        return arr.reshape((STRIP_W, height)).T, transparent
    return arr.reshape((height, STRIP_W)), transparent


def decode_smap(block, width: int, height: int) -> tuple[np.ndarray, list[bool]]:
    """Decode an SMAP block. Offsets count from the start of the block header."""
    p = block.data
    base = block.offset
    strips = width // STRIP_W
    offsets = struct.unpack_from(f"<{strips}I", p, base + 8)
    out = np.zeros((height, width), dtype=np.uint8)
    flags = []
    for i, off in enumerate(offsets):
        strip, transparent = decode_strip(p, base + off, height)
        out[:, i * STRIP_W:(i + 1) * STRIP_W] = strip
        flags.append(transparent)
    return out, flags


def decode_zplane(block, width: int, height: int) -> np.ndarray:
    """Decode a ZPnn mask to a boolean array; a strip offset of 0 means empty."""
    p = block.data
    base = block.offset
    strips = width // STRIP_W
    offsets = struct.unpack_from(f"<{strips}H", p, base + 8)
    out = np.zeros((height, width), dtype=bool)
    for i, off in enumerate(offsets):
        if not off:
            continue
        pos = base + off
        rows = bytearray()
        while len(rows) < height:
            b = p[pos]
            pos += 1
            n = b & 0x7F
            if b & 0x80:
                rows += bytes([p[pos]]) * n
                pos += 1
            else:
                rows += p[pos:pos + n]
                pos += n
        col = np.frombuffer(bytes(rows[:height]), dtype=np.uint8)
        out[:, i * STRIP_W:(i + 1) * STRIP_W] = np.unpackbits(col[:, None], axis=1)
    return out


def palette_of(room_block) -> bytes:
    """The room palette: a CLUT block in v5, PALS/WRAP/APAL in v6."""
    clut = room_block.find("CLUT")
    if clut is not None:
        return clut.payload[:768]
    return room_block.find("PALS").find("WRAP").find("APAL").payload[:768]


def to_png(pixels: np.ndarray, palette: bytes, path: str, clear=None) -> None:
    """Save indexed pixels; `clear` is a boolean mask of transparent pixels."""
    rgb = np.frombuffer(palette, dtype=np.uint8).reshape(-1, 3)[pixels]
    if clear is None:
        Image.fromarray(rgb, "RGB").save(path)
        return
    alpha = np.where(clear, 0, 255).astype(np.uint8)
    Image.fromarray(np.dstack([rgb, alpha]), "RGBA").save(path)


def mask_to_png(mask: np.ndarray, path: str) -> None:
    Image.fromarray((mask * 255).astype(np.uint8), "L").save(path)
