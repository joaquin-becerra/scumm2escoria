"""Room metadata of SCUMM v5: walk boxes, scale slots and objects."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BinBox:
    corners: list[tuple[int, int]]   # ul, ur, lr, ll
    mask: int
    flags: int
    scale: int


def parse_boxes(room) -> list[BinBox]:
    p = room.find("BOXD").payload
    (count,) = struct.unpack_from("<H", p, 0)
    boxes = []
    for i in range(count):
        v = struct.unpack_from("<8hBBH", p, 2 + 20 * i)
        boxes.append(BinBox([(v[0], v[1]), (v[2], v[3]), (v[4], v[5]), (v[6], v[7])],
                            v[8], v[9], v[10]))
    return boxes


@dataclass
class BinObject:
    number: int
    name: str
    x: int
    y: int
    width: int
    height: int
    parent: int
    walk_x: int
    walk_y: int
    direction: int
    entries: dict[int, int] = field(default_factory=dict)  # verb -> offset in `code`
    code: bytes = field(default=b"", repr=False)
    image_block: Optional[object] = field(default=None, repr=False)
    hotspots: list[tuple[int, int]] = field(default_factory=list)  # one per image state
    parent_state: int = 0       # shown only while the parent is in this state


def _code_header(p: bytes, version: int) -> tuple:
    """number, x, y, width, height, parent state, parent, walk x, walk y, direction."""
    if version >= 6:
        n, x, y, w, h, state, parent, _u1, _u2, d = struct.unpack_from("<HhhHHBBhhB", p, 0)
        return n, x, y, w, h, state, parent, 0, 0, d
    n, x, y, w, h, state, parent, wx, wy, d = struct.unpack_from("<HBBBBBBhhB", p, 0)
    return n, x * 8, y * 8, w * 8, h * 8, state, parent, wx, wy, d


def parse_verbs(block) -> tuple[dict[int, int], bytes]:
    """Handler table and code of a VERB block.

    Handlers can share code, so the code stays in one piece. Table offsets count
    from the block header; the ones returned count from the start of the code.
    """
    p = block.payload
    pos = 0
    table = {}
    while p[pos]:
        table[p[pos]] = struct.unpack_from("<H", p, pos + 1)[0] - 8
        pos += 3
    start = pos + 1
    return {verb: offset - start for verb, offset in table.items()}, p[start:]


def parse_objects(room, version: int) -> list[BinObject]:
    images = {}
    for obim in room.find_all("OBIM"):
        images[struct.unpack_from("<H", obim.find("IMHD").payload, 0)[0]] = obim
    objects = []
    for obcd in room.find_all("OBCD"):
        n, x, y, w, h, state, parent, wx, wy, d = _code_header(obcd.find("CDHD").payload, version)
        image = images.get(n)
        hotspots = []
        if version >= 6 and image is not None:
            # v6 keeps the walk-to points as hotspots of the image, per state.
            head = image.find("IMHD").payload
            count = struct.unpack_from("<H", head, 16)[0]
            hotspots = [struct.unpack_from("<hh", head, 18 + 4 * i) for i in range(count)
                        if 22 + 4 * i <= len(head)]
            if hotspots:
                wx, wy = hotspots[0]
        entries, code = parse_verbs(obcd.find("VERB"))
        name = obcd.find("OBNA").payload.split(b"\0")[0].decode("latin-1")
        objects.append(BinObject(n, name, x, y, w, h, parent, wx, wy, d, entries, code, image,
                                 hotspots, state))
    return objects
