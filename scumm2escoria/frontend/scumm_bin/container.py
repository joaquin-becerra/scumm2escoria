"""Reader for compiled SCUMM v5 resource files (GAME.000 index, GAME.001 data).

Files are XOR-obfuscated. Both are a tree of blocks: a 4-char tag, a big-endian
32-bit size that includes the 8-byte header, then the payload.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Iterator, Optional

XOR_KEY = 0x69
FIRST_BLOCKS = {b"RNAM", b"LECF"}       # how the index and data files start

# Blocks whose payload is made of more blocks.
CONTAINERS = {b"LECF", b"LFLF", b"ROOM", b"RMIM", b"OBIM", b"OBCD", b"SOUN", b"PALS", b"WRAP"}
CONTAINER_PREFIXES = (b"IM",)   # IM00, IM01...: SMAP + ZPnn


def read_scumm_file(path: str, key: int = XOR_KEY) -> bytes:
    """Read a resource file and undo its XOR obfuscation, if it has it."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] in FIRST_BLOCKS:
        return raw
    return bytes(b ^ key for b in raw) if key else raw


@dataclass
class Block:
    tag: str
    offset: int                 # of the header, in the decoded file
    size: int                   # including the header
    data: bytes = field(repr=False, default=b"")   # the whole file, shared
    children: list["Block"] = field(default_factory=list, repr=False)

    @property
    def payload(self) -> bytes:
        return self.data[self.offset + 8:self.offset + self.size]

    def find(self, tag: str) -> Optional["Block"]:
        return next((c for c in self.children if c.tag == tag), None)

    def find_all(self, tag: str) -> list["Block"]:
        return [c for c in self.children if c.tag == tag]

    def walk(self) -> Iterator["Block"]:
        yield self
        for c in self.children:
            yield from c.walk()


def _is_container(tag: bytes) -> bool:
    return tag in CONTAINERS or (tag[:2] in CONTAINER_PREFIXES and tag[2:].isdigit())


def parse_blocks(data: bytes, start: int = 0, end: Optional[int] = None) -> list[Block]:
    """Parse the sibling blocks in data[start:end], recursing into containers."""
    end = len(data) if end is None else end
    blocks: list[Block] = []
    pos = start
    while pos + 8 <= end:
        tag = data[pos:pos + 4]
        size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        if size < 8 or pos + size > end or not tag.isalnum():
            raise ValueError(f"bad block {tag!r} size {size} at 0x{pos:x}")
        block = Block(tag.decode("ascii"), pos, size, data)
        if _is_container(tag):
            # SOUN wraps either more blocks or raw data, depending on the sound.
            try:
                block.children = parse_blocks(data, pos + 8, pos + size)
            except ValueError:
                if tag != b"SOUN":
                    raise
        blocks.append(block)
        pos += size
    return blocks


@dataclass
class DirEntry:
    room: int
    offset: int


@dataclass
class Index:
    room_names: dict[int, str] = field(default_factory=dict)
    max_vars: dict[str, int] = field(default_factory=dict)
    rooms: list[DirEntry] = field(default_factory=list)      # DROO
    scripts: list[DirEntry] = field(default_factory=list)    # DSCR
    sounds: list[DirEntry] = field(default_factory=list)     # DSOU
    costumes: list[DirEntry] = field(default_factory=list)   # DCOS
    charsets: list[DirEntry] = field(default_factory=list)   # DCHR
    objects: list[tuple[int, int, int]] = field(default_factory=list)  # DOBJ: owner, state, class
    version: int = 5            # 6 files add an AARY block


def _directory(p: bytes) -> list[DirEntry]:
    n = struct.unpack("<H", p[:2])[0]
    rooms = p[2:2 + n]
    offs = struct.unpack(f"<{n}I", p[2 + n:2 + n + 4 * n])
    return [DirEntry(rooms[i], offs[i]) for i in range(n)]


def parse_index(path: str) -> Index:
    data = read_scumm_file(path)
    idx = Index()
    for b in parse_blocks(data):
        p = b.payload
        if b.tag == "AARY":
            idx.version = 6
        elif b.tag == "RNAM":
            pos = 0
            while p[pos] != 0:
                name = bytes(c ^ 0xFF for c in p[pos + 1:pos + 10]).split(b"\0")[0]
                idx.room_names[p[pos]] = name.decode("latin-1")
                pos += 10
        elif b.tag == "MAXS":
            names = ("variables", "unknown", "bit_variables", "local_objects",
                     "new_names", "charsets", "verbs", "array", "inventory")
            vals = struct.unpack(f"<{len(p) // 2}H", p[:len(p) // 2 * 2])
            idx.max_vars = dict(zip(names, vals))
        elif b.tag == "DROO":
            idx.rooms = _directory(p)
        elif b.tag == "DSCR":
            idx.scripts = _directory(p)
        elif b.tag == "DSOU":
            idx.sounds = _directory(p)
        elif b.tag == "DCOS":
            idx.costumes = _directory(p)
        elif b.tag == "DCHR":
            idx.charsets = _directory(p)
        elif b.tag == "DOBJ":
            n = struct.unpack("<H", p[:2])[0]
            owners = p[2:2 + n]
            classes = struct.unpack(f"<{n}I", p[2 + n:2 + n + 4 * n])
            idx.objects = [(o & 0x0F, o >> 4, c) for o, c in zip(owners, classes)]
    return idx


@dataclass
class Game:
    """An opened game: index plus the block tree of the data file."""
    index: Index
    data: bytes = field(repr=False)
    tree: list[Block] = field(repr=False)

    @property
    def lflfs(self) -> list[Block]:
        return self.tree[0].find_all("LFLF")

    def room_numbers(self) -> dict[int, Block]:
        """Room number -> LFLF, from the LOFF offset table."""
        loff = self.tree[0].find("LOFF").payload
        by_room_offset = {}
        for i in range(loff[0]):
            room, off = struct.unpack("<BI", loff[1 + 5 * i:6 + 5 * i])
            by_room_offset[off] = room
        out = {}
        for lflf in self.lflfs:
            room_block = lflf.find("ROOM")
            if room_block is not None and room_block.offset in by_room_offset:
                out[by_room_offset[room_block.offset]] = lflf
        return out


    def room_offsets(self) -> dict[int, int]:
        loff = self.tree[0].find("LOFF").payload
        return {loff[1 + 5 * i]: struct.unpack_from("<I", loff, 2 + 5 * i)[0]
                for i in range(loff[0])}

    def resources(self, directory: list[DirEntry], tag: str) -> dict[int, Block]:
        """Resource number -> block. Directory offsets count from the room."""
        rooms = self.room_offsets()
        blocks = {b.offset: b for b in self.tree[0].walk() if b.tag == tag}
        out = {}
        for number, entry in enumerate(directory):
            block = blocks.get(rooms.get(entry.room, -1) + entry.offset)
            if entry.room and block is not None:
                out[number] = block
        return out


def open_game(directory: str, stem: Optional[str] = None) -> Game:
    """Open the STEM.000/STEM.001 pair in a directory."""
    if stem is None:
        stem = next(f[:-4] for f in sorted(os.listdir(directory)) if f.upper().endswith(".000"))
    index = parse_index(os.path.join(directory, stem + ".000"))
    data = read_scumm_file(os.path.join(directory, stem + ".001"))
    return Game(index, data, parse_blocks(data))
