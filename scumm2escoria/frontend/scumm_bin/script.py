"""Common form of disassembled SCUMM scripts, shared by the v5 and v6 readers.

An argument is JSON-ready: an int, a variable ({"var": "V12"}), a string
({"text": "..."}), a nested call ({"call": name, "args": [...]}) or a list.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Any, Optional

# Message escapes: 0xFF, a code and, except for these codes, a 16-bit value.
NO_VALUE = {1: "\n", 2: "{keep}", 3: "{wait}", 8: "{unknown8}"}
VALUE_NAMES = {4: "int", 5: "verb", 6: "name", 7: "string", 9: "anim", 10: "sound",
               12: "color", 13: "unknown13", 14: "charset"}


@dataclass
class Instr:
    offset: int
    opcode: int
    name: str
    args: list[Any] = field(default_factory=list)
    target: Optional[int] = None     # jump destination, as a script offset
    result: Any = None               # variable that receives the value

    def to_dict(self) -> dict:
        out = {"offset": self.offset, "opcode": self.opcode, "name": self.name, "args": self.args}
        if self.target is not None:
            out["target"] = self.target
        if self.result is not None:
            out["result"] = self.result
        return out


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    @property
    def done(self) -> bool:
        return self.pos >= len(self.data)

    def u8(self) -> int:
        value = self.data[self.pos]
        self.pos += 1
        return value

    def u16(self) -> int:
        (value,) = struct.unpack_from("<H", self.data, self.pos)
        self.pos += 2
        return value

    def i16(self) -> int:
        (value,) = struct.unpack_from("<h", self.data, self.pos)
        self.pos += 2
        return value

    def jump(self) -> int:
        offset = self.i16()
        return self.pos + offset

    def text(self) -> dict:
        """A zero-terminated message, with its escapes in readable form."""
        out: list[str] = []
        while True:
            c = self.u8()
            if c == 0:
                return {"text": "".join(out)}
            if c not in (0xFE, 0xFF):       # both mark an escape, up to SCUMM v6
                out.append(chr(c))
                continue
            code = self.u8()
            if code in NO_VALUE:
                out.append(NO_VALUE[code])
            else:
                value = self.u16()
                out.append(f"{{{VALUE_NAMES.get(code, f'esc{code}')}:{value}}}")


def var_name(word: int) -> str:
    """Name of a variable from its encoded number: bit, local or global."""
    if word & 0x8000:
        return f"B{word & 0x7FFF}"
    if word & 0x4000:
        return f"L{word & 0x0FFF}"
    return f"V{word & 0x1FFF}"


def format_arg(arg: Any) -> str:
    if isinstance(arg, dict):
        if "var" in arg:
            index = f"[{format_arg(arg['index'])}]" if "index" in arg else ""
            return arg["var"] + index
        if "text" in arg:
            return json.dumps(arg["text"], ensure_ascii=False)
        if "call" in arg:
            return f"{arg['call']}({', '.join(format_arg(a) for a in arg['args'])})"
    if isinstance(arg, list):
        return "[" + ", ".join(format_arg(a) for a in arg) + "]"
    return str(arg)


def format_instr(i: Instr) -> str:
    line = f"[{i.offset:04X}] "
    if i.result is not None:
        line += format_arg(i.result) + " = "
    line += f"{i.name}({', '.join(format_arg(a) for a in i.args)})"
    if i.target is not None:
        line += f" -> {i.target:04X}"
    return line
