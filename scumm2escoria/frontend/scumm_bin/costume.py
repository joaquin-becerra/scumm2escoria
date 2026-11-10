"""SCUMM v5 costumes: palette, per-limb cels and animation tables.

Offsets count from 6 bytes before the animation count. In v6 files those bytes
are a size and the letters "CO"; in v5 they're absent, so parse_costume pads the
payload to use offsets as they are. An animation number is frame * 4 + dir.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

LIMBS = 16
FORMAT_COLORS = {0x58: 16, 0x59: 32, 0x60: 16, 0x61: 32}


@dataclass
class Cel:
    width: int
    height: int
    rel_x: int
    rel_y: int
    move_x: int
    move_y: int
    pixels: np.ndarray = field(repr=False)     # costume palette slots, 0 == clear


@dataclass
class LimbAnim:
    limb: int
    start: Optional[int]        # into the command list; None == limb stops
    length: int = 0
    loop: bool = True


@dataclass
class Costume:
    number: int
    mirror: bool                # west frames are east ones flipped
    palette: bytes              # slot -> room palette index
    commands: bytes
    limb_tables: list[int]
    anims: dict[int, list[LimbAnim]]
    payload: bytes = field(repr=False, default=b"")

    def cel(self, limb: int, index: int) -> Optional[Cel]:
        table = self.limb_tables[limb]
        (offset,) = struct.unpack_from("<H", self.payload, table + 2 * index)
        return decode_cel(self.payload, offset, len(self.palette)) if offset else None


def decode_cel(p: bytes, offset: int, colors: int) -> Cel:
    w, h, rx, ry, mx, my = struct.unpack_from("<HHhhhh", p, offset)
    shift, mask = (4, 0x0F) if colors == 16 else (3, 0x07)
    flat = np.zeros(w * h, dtype=np.uint8)     # column by column
    pos, i, total = offset + 12, 0, w * h
    while i < total:
        b = p[pos]
        pos += 1
        run = b & mask
        if not run:
            run = p[pos]
            pos += 1
        flat[i:i + run] = b >> shift
        i += run
    return Cel(w, h, rx, ry, mx, my, flat.reshape((w, h)).T)


def parse_costume(number: int, block) -> Costume:
    p = block.payload if block.payload[4:6] == b"CO" else bytes(6) + block.payload
    n_anims = p[6] + 1
    fmt = p[7] & 0x7F
    colors = FORMAT_COLORS.get(fmt)
    if colors is None:
        raise ValueError(f"costume {number}: unknown format 0x{fmt:02x}")
    palette = p[8:8 + colors]
    pos = 8 + colors
    (cmd_offset,) = struct.unpack_from("<H", p, pos)
    limb_tables = list(struct.unpack_from(f"<{LIMBS}H", p, pos + 2))
    anim_offsets = struct.unpack_from(f"<{n_anims}H", p, pos + 2 + 2 * LIMBS)
    anims: dict[int, list[LimbAnim]] = {}
    for number_, off in enumerate(anim_offsets):
        if not off:
            continue
        (limb_mask,) = struct.unpack_from("<H", p, off)
        cursor = off + 2
        steps = []
        for limb in range(LIMBS):
            if not limb_mask & (0x8000 >> limb):
                continue
            (start,) = struct.unpack_from("<H", p, cursor)
            cursor += 2
            if start == 0xFFFF:
                steps.append(LimbAnim(limb, None))
            else:
                extra = p[cursor]
                cursor += 1
                steps.append(LimbAnim(limb, start, extra & 0x7F, not extra & 0x80))
        anims[number_] = steps
    return Costume(number, not p[7] & 0x80, palette, p[cmd_offset:], limb_tables, anims, p)
