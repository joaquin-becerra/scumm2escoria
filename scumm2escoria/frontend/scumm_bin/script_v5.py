"""SCUMM v5 bytecode. Operands are inline; the high bits of the opcode (or of a
sub-opcode) say which ones are variables instead of immediate values.

Operand specs, one letter each:
  b w   byte / word that may be a variable, per the next mask bit
  B W   immediate byte / word
  r     result variable        v  variable operand
  o     jump offset            a  word list ended by 0xFF
  s     message                |  a new sub-opcode byte; masks start over
"""
from __future__ import annotations

from typing import Callable

from .script import Instr, Reader, var_name

MASKS = (0x80, 0x40, 0x20)


def read_var(r: Reader) -> dict:
    word = r.u16()
    if word & 0x2000:
        index = r.u16()
        base = {"var": var_name(word & ~0x2000)}
        base["index"] = {"var": var_name(index & ~0x2000)} if index & 0x2000 else index
        return base
    return {"var": var_name(word)}


def word_list(r: Reader) -> list:
    out = []
    while True:
        sub = r.u8()
        if sub == 0xFF:
            return out
        out.append(read_var(r) if sub & 0x80 else r.i16())


def operands(r: Reader, spec: str, opcode: int, instr: Instr) -> list:
    out: list = []
    mask = 0
    for token in spec:
        if token in "bw":
            if opcode & MASKS[mask]:
                out.append(read_var(r))
            else:
                out.append(r.u8() if token == "b" else r.i16())
            mask += 1
        elif token == "B":
            out.append(r.u8())
        elif token == "W":
            out.append(r.u16())
        elif token == "r":
            instr.result = read_var(r)
        elif token == "v":
            out.append(read_var(r))
        elif token == "o":
            instr.target = r.jump()
        elif token == "a":
            out.append(word_list(r))
        elif token == "s":
            out.append(r.text())
        elif token == "|":
            opcode = r.u8()
            mask = 0
    return out


def sub(table: dict, mask: int = 0x1F) -> Callable:
    """One sub-opcode."""
    def read(r: Reader, instr: Instr) -> list:
        code = r.u8()
        name, spec = table[code & mask]
        return [{"call": name, "args": operands(r, spec, code, instr)}]
    return read


def sub_loop(table: dict, stop: tuple = ()) -> Callable:
    """Sub-opcodes until 0x1F in the low bits; `stop` ones also end the list."""
    def read(r: Reader, instr: Instr) -> list:
        out = []
        while True:
            code = r.u8()
            if code & 0x1F == 0x1F:
                return out
            name, spec = table[code & 0x1F]
            out.append({"call": name, "args": operands(r, spec, code, instr)})
            if code & 0x1F in stop:
                return out
    return read


TEXT_OPS = {0: ("at", "ww"), 1: ("color", "b"), 2: ("clipped", "w"), 3: ("erase", "ww"),
            4: ("center", ""), 6: ("left", ""), 7: ("overhead", ""), 8: ("voice", "ww"),
            15: ("text", "s")}
ACTOR_OPS = {0: ("dummy", "b"), 1: ("costume", "b"), 2: ("stepDist", "bb"), 3: ("sound", "b"),
             4: ("walkAnimation", "b"), 5: ("talkAnimation", "bb"), 6: ("standAnimation", "b"),
             7: ("animation", "bbb"), 8: ("default", ""), 9: ("elevation", "w"),
             10: ("animationDefault", ""), 11: ("palette", "bb"), 12: ("talkColor", "b"),
             13: ("name", "s"), 14: ("initAnimation", "b"), 16: ("width", "b"),
             17: ("scale", "bb"), 18: ("neverZclip", ""), 19: ("alwaysZclip", "b"),
             20: ("ignoreBoxes", ""), 21: ("followBoxes", ""), 22: ("animationSpeed", "b"),
             23: ("shadow", "b")}
ROOM_OPS = {1: ("scroll", "ww"), 2: ("color", "ww"), 3: ("screen", "ww"),
            4: ("palette", "www|b"), 5: ("shakeOn", ""), 6: ("shakeOff", ""),
            7: ("scale", "bb|bb|b"), 8: ("intensity", "bbb"), 9: ("saveGame", "bb"),
            10: ("fade", "w"), 11: ("rgbIntensity", "www|bb"), 12: ("shadow", "www|bb"),
            13: ("saveString", "bs"), 14: ("loadString", "bs"), 15: ("transform", "b|bb|b"),
            16: ("cycleSpeed", "bb")}
VERB_OPS = {1: ("image", "w"), 2: ("name", "s"), 3: ("color", "b"), 4: ("hiColor", "b"),
            5: ("at", "ww"), 6: ("on", ""), 7: ("off", ""), 8: ("delete", ""), 9: ("new", ""),
            16: ("dimColor", "b"), 17: ("dim", ""), 18: ("key", "b"), 19: ("center", ""),
            20: ("nameString", "w"), 22: ("imageRoom", "wb"), 23: ("backColor", "b")}
STRING_OPS = {1: ("assign", "bs"), 2: ("assignString", "bb"), 3: ("setChar", "bbb"),
              4: ("getChar", "rbb"), 5: ("create", "bb")}
CURSOR_OPS = {1: ("cursorOn", ""), 2: ("cursorOff", ""), 3: ("userputOn", ""),
              4: ("userputOff", ""), 5: ("cursorSoftOn", ""), 6: ("cursorSoftOff", ""),
              7: ("userputSoftOn", ""), 8: ("userputSoftOff", ""), 10: ("cursorImage", "bb"),
              11: ("cursorHotspot", "bbb"), 12: ("cursorSet", "b"), 13: ("charsetSet", "b"),
              14: ("charsetColors", "a")}
RESOURCE_OPS = {1: ("loadScript", "b"), 2: ("loadSound", "b"), 3: ("loadCostume", "b"),
                4: ("loadRoom", "b"), 5: ("nukeScript", "b"), 6: ("nukeSound", "b"),
                7: ("nukeCostume", "b"), 8: ("nukeRoom", "b"), 9: ("lockScript", "b"),
                10: ("lockSound", "b"), 11: ("lockCostume", "b"), 12: ("lockRoom", "b"),
                13: ("unlockScript", "b"), 14: ("unlockSound", "b"), 15: ("unlockCostume", "b"),
                16: ("unlockRoom", "b"), 17: ("clearHeap", ""), 18: ("loadCharset", "b"),
                19: ("nukeCharset", "b"), 20: ("loadObject", "bw"), 32: ("unknown32", "b"),
                33: ("unknown33", "b"), 35: ("unknown35", "bb"), 36: ("unknown36", "bbB"),
                37: ("unknown37", "bb")}
MATRIX_OPS = {1: ("setBoxFlags", "bb"), 2: ("setBoxScale", "bb"), 3: ("setBoxScaleB", "bb"),
              4: ("createBoxMatrix", "")}
WAIT_OPS = {1: ("actor", "b"), 2: ("message", ""), 3: ("camera", ""), 4: ("sentence", "")}
VERB_STATE_OPS = {1: ("save", "bbb"), 2: ("restore", "bbb"), 3: ("delete", "bbb")}
DRAW_OBJECT_OPS = {1: ("at", "ww"), 2: ("state", "w")}


def expression(r: Reader, instr: Instr) -> list:
    out: list = []
    while True:
        code = r.u8()
        if code == 0xFF:
            return out
        kind = code & 0x1F
        if kind == 1:
            out.append(operands(r, "w", code, instr)[0])
        elif kind in (2, 3, 4, 5):
            out.append({"call": "+-*/"[kind - 2], "args": []})
        elif kind == 6:
            nested = decode(r)
            out.append({"call": nested.name, "args": nested.args})
        else:
            raise ValueError(f"unknown expression op {code}")


def override(r: Reader, instr: Instr) -> list:
    return [r.u8()]


def system_ops(r: Reader, instr: Instr) -> list:
    return [r.u8()]


def pseudo_room(r: Reader, instr: Instr) -> list:
    out = [r.u8()]
    while True:
        value = r.u8()
        if value == 0:
            return out
        out.append(value)


def set_var_range(r: Reader, instr: Instr) -> list:
    instr.result = read_var(r)
    wide = instr.opcode & 0x80
    return [r.u16() if wide else r.u8() for _ in range(r.u8())]


def do_sentence(r: Reader, instr: Instr) -> list:
    if not instr.opcode & 0x80 and r.data[r.pos] == 0xFE:
        return [r.u8()]
    return operands(r, "bww", instr.opcode, instr)


def by_four(base: int) -> tuple:
    return (base, base + 0x40, base + 0x80, base + 0xC0)


def two(base: int) -> tuple:
    return (base, base + 0x80)


def all_masks(base: int) -> tuple:
    return tuple(base + 0x20 * i for i in range(8))


TABLE: dict[int, tuple] = {}


def define(name: str, spec, codes) -> None:
    for code in (codes if isinstance(codes, tuple) else (codes,)):
        TABLE[code] = (name, spec)


for _name, _spec, _codes in (
    ("stopObjectCode", "", (0x00, 0xA0)), ("stopMusic", "", 0x20), ("cutscene", "a", 0x40),
    ("freezeScripts", "b", (0x60, 0xE0)), ("breakHere", "", 0x80), ("endCutscene", "", 0xC0),
    ("putActor", "bww", all_masks(0x01)),
    ("startMusic", "b", two(0x02)), ("getAnimCounter", "rb", two(0x22)),
    ("chainScript", "ba", two(0x42)), ("stopScript", "b", two(0x62)),
    ("getActorRoom", "rb", two(0x03)), ("getActorY", "rw", two(0x23)),
    ("getActorX", "rw", two(0x43)), ("getActorFacing", "rb", two(0x63)),
    ("isGreaterEqual", "vwo", two(0x04)), ("isLess", "vwo", two(0x44)),
    ("loadRoomWithEgo", "wbWW", by_four(0x24)),
    ("pickupObject", "wb", by_four(0x25)),
    ("getActorElevation", "rb", two(0x06)), ("increment", "r", 0x46), ("decrement", "r", 0xC6),
    ("getClosestObjActor", "rw", two(0x66)),
    ("setState", "wb", by_four(0x07)), ("dummy", "", 0xA7),
    ("getStringWidth", "rb", two(0x67)),
    ("isNotEqual", "vwo", two(0x08)), ("equalZero", "vo", 0x28), ("notEqualZero", "vo", 0xA8),
    ("isEqual", "vwo", two(0x48)), ("isScriptRunning", "rb", two(0x68)),
    ("faceActor", "bw", by_four(0x09)), ("setOwnerOf", "wb", by_four(0x29)),
    ("startScript", "ba", all_masks(0x0A)),
    ("getVerbEntrypoint", "rww", by_four(0x0B)), ("delayVariable", "v", 0x2B),
    ("debug", "w", two(0x6B)[:1]),
    ("soundKludge", "a", 0x4C), ("getActorWidth", "rb", two(0x6C)),
    ("walkActorToActor", "bbB", by_four(0x0D)), ("putActorInRoom", "bb", by_four(0x2D)),
    ("putActorAtObject", "bw", by_four(0x0E)), ("delay", "BBB", 0x2E),
    ("stopObjectScript", "w", two(0x6E)),
    ("getState", "rw", two(0x0F)), ("ifNotState", "wbo", by_four(0x2F)),
    ("ifState", "wbo", two(0x4F)),
    ("getObjectOwner", "rw", two(0x10)), ("lights", "bBB", two(0x70)),
    ("animateActor", "bb", by_four(0x11)), ("getActorCostume", "rb", two(0x71)),
    ("getInventoryCount", "rb", two(0x31)),
    ("panCameraTo", "w", two(0x12)), ("setCameraAt", "w", two(0x32)),
    ("actorFollowCamera", "b", two(0x52)), ("loadRoom", "b", two(0x72)),
    ("setObjectName", "ws", two(0x54)), ("getDist", "rww", by_four(0x34)),
    ("actorFromPos", "rww", by_four(0x15)), ("findObject", "rbb", by_four(0x35)),
    ("getRandomNr", "rb", two(0x16)), ("getActorMoving", "rb", two(0x56)),
    ("walkActorToObject", "bw", by_four(0x36)),
    ("and", "rw", two(0x17)), ("or", "rw", two(0x57)), ("startObject", "wba", by_four(0x37)),
    ("jumpRelative", "o", 0x18), ("isLessEqual", "vwo", two(0x38)),
    ("isGreater", "vwo", two(0x78)),
    ("doSentence", do_sentence, all_masks(0x19)),
    ("move", "rw", two(0x1A)), ("subtract", "rw", two(0x3A)), ("add", "rw", two(0x5A)),
    ("multiply", "rw", two(0x1B)), ("getActorScale", "rb", two(0x3B)),
    ("divide", "rw", two(0x5B)), ("getActorWalkBox", "rb", two(0x7B)),
    ("startSound", "b", two(0x1C)), ("stopSound", "b", two(0x3C)),
    ("isSoundRunning", "rb", two(0x7C)),
    ("ifClassOfIs", "wao", two(0x1D)), ("findInventory", "rbb", by_four(0x3D)),
    ("setClass", "wa", two(0x5D)),
    ("walkActorTo", "bww", all_masks(0x1E)),
    ("isActorInBox", "bbo", by_four(0x1F)), ("drawBox", "ww|wwb", by_four(0x3F)),
):
    define(_name, _spec, _codes)

define("drawObject", ("w", sub_loop(DRAW_OBJECT_OPS, stop=(1, 2))), by_four(0x05))
define("setVarRange", set_var_range, two(0x26))
define("stringOps", sub(STRING_OPS), 0x27)
define("saveRestoreVerbs", sub(VERB_STATE_OPS), 0xAB)
define("resourceRoutines", sub(RESOURCE_OPS, 0x3F), two(0x0C))
define("cursorCommand", sub(CURSOR_OPS), 0x2C)
define("expression", ("r", expression), 0xAC)
define("pseudoRoom", pseudo_room, 0xCC)
define("wait", sub(WAIT_OPS), 0xAE)
define("matrixOps", sub(MATRIX_OPS), two(0x30))
define("actorOps", ("b", sub_loop(ACTOR_OPS)), by_four(0x13))
define("roomOps", sub(ROOM_OPS), by_four(0x33))
define("print", ("b", sub_loop(TEXT_OPS, stop=(15,))), two(0x14))
define("printEgo", sub_loop(TEXT_OPS, stop=(15,)), 0xD8)
define("beginOverride", override, 0x58)
define("systemOps", system_ops, 0x98)
define("verbOps", ("b", sub_loop(VERB_OPS)), two(0x7A))


def decode(r: Reader) -> Instr:
    offset = r.pos
    code = r.u8()
    if code not in TABLE:
        raise ValueError(f"unknown opcode 0x{code:02x} at 0x{offset:x}")
    name, spec = TABLE[code]
    instr = Instr(offset, code, name)
    steps = spec if isinstance(spec, tuple) else (spec,)
    for step in steps:
        if isinstance(step, str):
            instr.args += operands(r, step, code, instr)
        else:
            instr.args += step(r, instr)
    return instr


# The engine jumps when the condition is false, and compares value OP variable.
COMPARISONS = {"isEqual": "==", "isNotEqual": "!=", "isGreater": "<", "isGreaterEqual": "<=",
               "isLess": ">", "isLessEqual": ">="}
ARITHMETIC = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/", "and": "&", "or": "|"}
KEEP_RESULT = {"move", "increment", "decrement", "setVarRange"}
SYSTEM_TALKER = 0xFC        # print to these "actors" shows text nobody says
KEEP_ROOM = 0xFF            # v6 says it in the room of putActor; v5 has putActorInRoom


def expression_tree(items: list):
    """Turn the postfix items of an expression into one tree."""
    stack: list = []
    for item in items:
        if isinstance(item, dict) and item.get("call") in ("+", "-", "*", "/") and not item["args"]:
            right, left = stack.pop(), stack.pop()
            stack.append({"call": item["call"], "args": [left, right]})
        else:
            stack.append(item)
    return stack[-1]


def text_of(subs: list):
    """The message among the sub-opcodes of a print."""
    return next((s["args"][0] for s in subs if isinstance(s, dict) and s.get("call") == "text"), None)


def lift_print(i: Instr) -> None:
    """v5 prints everything with one opcode, whose first operand says who talks."""
    who, subs = (i.args[0], i.args[1:]) if i.name == "print" else (None, i.args)
    text = text_of(subs)
    # Where and in which colour: what a close-up has instead of an actor's place.
    place = [s for s in subs if isinstance(s, dict) and s.get("call") in ("at", "color")]
    if text is None:
        # Without a message, it sets the defaults of the next ones.
        i.name, i.args = "printDefaults", ["ego" if i.name == "printEgo" else who, *place]
    elif i.name == "printEgo":
        i.name, i.args = "talkEgo", [text]
    elif isinstance(who, int) and who >= SYSTEM_TALKER:
        i.name, i.args = "printSystem", [{"call": "text", "args": [text]}, *place]
    else:
        i.name, i.args = "talkActor", [who, text]


def lift_draw_object(i: Instr) -> None:
    """drawObject has a sub-opcode in v5, and two instructions in v6."""
    sub = i.args[1] if len(i.args) > 1 else None
    if sub is not None and sub["call"] == "at":
        i.name, i.args = "drawObjectAt", [i.args[0], *sub["args"]]
    else:
        i.args = [i.args[0], sub["args"][0] if sub is not None else 1]


def lift(i: Instr) -> Instr:
    """Rewrite an instruction to the common form the v6 reader also produces."""
    if i.name in COMPARISONS:
        i.args = [{"call": COMPARISONS[i.name], "args": i.args}]
        i.name = "ifNot"
    elif i.name in ("equalZero", "notEqualZero"):
        op = "==" if i.name == "equalZero" else "!="
        i.args = [{"call": op, "args": [i.args[0], 0]}]
        i.name = "ifNot"
    elif i.name in ("ifState", "ifNotState"):
        op = "==" if i.name == "ifState" else "!="
        state = {"call": "getState", "args": [i.args[0]]}
        i.args = [{"call": op, "args": [state, i.args[1]]}]
        i.name = "ifNot"
    elif i.name in ("ifClassOfIs", "isActorInBox"):
        i.args = [{"call": i.name, "args": i.args}]
        i.name = "ifNot"
    elif i.name == "expression":
        i.args = [expression_tree(i.args)]
        i.name = "move"
    elif i.name in ARITHMETIC:
        i.args = [{"call": ARITHMETIC[i.name], "args": [i.result, i.args[0]]}]
        i.name = "move"
    elif i.name == "startScript":
        # v5 keeps the flags in the opcode, v6 passes them first. Only 2, recursive,
        # stays: 1 says the script survives freezeScripts, and v5 games set it everywhere.
        i.args = [2 if i.opcode & 0x40 else 0, *i.args]
    elif i.name in ("putActor", "putActorAtObject"):
        i.args = [*i.args, KEEP_ROOM]
    elif i.name == "walkActorToObject":
        i.args = [*i.args, 0]
    elif i.name == "startObject":
        i.args = [0, *i.args]
    elif i.name == "doSentence":
        if len(i.args) == 1:
            i.name, i.args = "stopSentence", []
        else:
            i.args = [i.args[0], i.args[1], 0, i.args[2]]
    elif i.name == "delay":
        i.args = [i.args[0] | i.args[1] << 8 | i.args[2] << 16]
    elif i.name == "delayVariable":
        i.name = "delay"
    elif i.name == "beginOverride":
        i.name, i.args = ("beginOverride" if i.args[0] else "endOverride"), []
    elif i.name == "pseudoRoom":
        i.args = [i.args[0], i.args[1:]]
    elif i.name in ("print", "printEgo"):
        lift_print(i)
    elif i.name == "drawObject":
        lift_draw_object(i)
    elif i.name in ("actorOps", "verbOps"):
        # v5 names the actor or verb in the instruction, v6 with a sub-opcode.
        i.args = [{"call": "setCurrent", "args": [i.args[0]]}, *i.args[1:]]
    elif i.result is not None and i.name not in KEEP_RESULT:
        i.args = [{"call": i.name, "args": i.args}]
        i.name = "move"
    return i


def disassemble(code: bytes) -> list[Instr]:
    r = Reader(code)
    out = []
    while not r.done:
        out.append(lift(decode(r)))
    return out
