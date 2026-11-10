"""SCUMM v6 bytecode. Operands go through a stack, so disassembling simulates it:
pushes build expression trees and only statements become instructions, with the
same shape the v5 reader produces.

An instruction's offset is where its first push starts, which is what jumps
point at. Pop specs are in push order: n = one value, l = a counted list.
"""
from __future__ import annotations

from .script import Instr, Reader, var_name

# name, immediates, pops, leaves a value on the stack
FUNCTIONS = {
    0x5E: ("startScript", "", "nnl", 0), 0x5F: ("startScriptQuick", "", "nl", 0),
    0x60: ("startObject", "", "nnnl", 0), 0x61: ("drawObject", "", "nn", 0),
    0x62: ("drawObjectAt", "", "nnn", 0), 0x63: ("drawBlastObject", "", "nnnnnl", 0),
    0x64: ("setBlastObjectWindow", "", "nnnn", 0),
    0x65: ("stopObjectCode", "", "", 0), 0x66: ("stopObjectCode", "", "", 0),
    0x67: ("endCutscene", "", "", 0), 0x68: ("cutscene", "", "l", 0),
    0x69: ("stopMusic", "", "", 0), 0x6A: ("freezeScripts", "", "n", 0),
    0x6C: ("breakHere", "", "", 0), 0x6D: ("ifClassOfIs", "", "nl", 1),
    0x6E: ("setClass", "", "nl", 0), 0x6F: ("getState", "", "n", 1),
    0x70: ("setState", "", "nn", 0), 0x71: ("setOwnerOf", "", "nn", 0),
    0x72: ("getObjectOwner", "", "n", 1), 0x74: ("startSound", "", "n", 0),
    0x75: ("stopSound", "", "n", 0), 0x76: ("startMusic", "", "n", 0),
    0x77: ("stopObjectScript", "", "n", 0), 0x78: ("panCameraTo", "", "n", 0),
    0x79: ("actorFollowCamera", "", "n", 0), 0x7A: ("setCameraAt", "", "n", 0),
    0x7B: ("loadRoom", "", "n", 0), 0x7C: ("stopScript", "", "n", 0),
    0x7D: ("walkActorToObject", "", "nnn", 0), 0x7E: ("walkActorTo", "", "nnn", 0),
    0x7F: ("putActor", "", "nnnn", 0), 0x80: ("putActorAtObject", "", "nnn", 0),
    0x81: ("faceActor", "", "nn", 0), 0x82: ("animateActor", "", "nn", 0),
    0x83: ("doSentence", "", "nnnn", 0), 0x84: ("pickupObject", "", "nn", 0),
    0x85: ("loadRoomWithEgo", "", "nnnn", 0), 0x87: ("getRandomNr", "", "n", 1),
    0x88: ("getRandomNrRange", "", "nn", 1), 0x8A: ("getActorMoving", "", "n", 1),
    0x8B: ("isScriptRunning", "", "n", 1), 0x8C: ("getActorRoom", "", "n", 1),
    0x8D: ("getObjectX", "", "n", 1), 0x8E: ("getObjectY", "", "n", 1),
    0x8F: ("getObjectDir", "", "n", 1), 0x90: ("getActorWalkBox", "", "n", 1),
    0x91: ("getActorCostume", "", "n", 1), 0x92: ("findInventory", "", "nn", 1),
    0x93: ("getInventoryCount", "", "n", 1), 0x94: ("getVerbFromXY", "", "nn", 1),
    0x95: ("beginOverride", "", "", 0), 0x96: ("endOverride", "", "", 0),
    0x97: ("setObjectName", "s", "n", 0), 0x98: ("isSoundRunning", "", "n", 1),
    0x99: ("setBoxFlags", "", "ln", 0), 0x9A: ("createBoxMatrix", "", "", 0),
    0x9F: ("actorFromPos", "", "nn", 1), 0xA0: ("findObject", "", "nn", 1),
    0xA1: ("pseudoRoom", "", "nl", 0), 0xA2: ("getActorElevation", "", "n", 1),
    0xA3: ("getVerbEntrypoint", "", "nn", 1), 0xA6: ("drawBox", "", "nnnnn", 0),
    0xA8: ("getActorWidth", "", "n", 1), 0xAA: ("getActorScale", "", "n", 1),
    0xAB: ("getAnimCounter", "", "n", 1), 0xAC: ("soundKludge", "", "l", 0),
    0xAD: ("isAnyOf", "", "nl", 1), 0xAF: ("isActorInBox", "", "nn", 1),
    0xB0: ("delay", "", "n", 0), 0xB1: ("delaySeconds", "", "n", 0),
    0xB2: ("delayMinutes", "", "n", 0), 0xB3: ("stopSentence", "", "", 0),
    0xBA: ("talkActor", "s", "n", 0), 0xBB: ("talkEgo", "s", "", 0),
    0xBD: ("dummy", "", "", 0), 0xBE: ("startObjectQuick", "", "nnl", 0),
    0xBF: ("startScriptQuick2", "", "nl", 0), 0xC4: ("abs", "", "n", 1),
    0xC5: ("distObjectObject", "", "nn", 1), 0xC6: ("distObjectPt", "", "nnn", 1),
    0xC7: ("distPtPt", "", "nnnn", 1), 0xC8: ("kernelGetFunctions", "", "l", 1),
    0xC9: ("kernelSetFunctions", "", "l", 0), 0xCA: ("delayFrames", "", "n", 0),
    0xCB: ("pickOneOf", "", "nl", 1), 0xCC: ("pickOneOfDefault", "", "nln", 1),
    0xCD: ("stampObject", "", "nnnn", 0), 0xD0: ("getDateTime", "", "", 0),
    0xD1: ("stopTalking", "", "", 0), 0xD2: ("getAnimateVariable", "", "nn", 1),
    0xD4: ("shuffle", "W", "nn", 0), 0xD5: ("jumpToScript", "", "nnl", 0),
    0xD8: ("isRoomScriptRunning", "", "n", 1), 0xDD: ("findAllObjects", "", "n", 1),
    0xE1: ("getPixel", "", "nn", 1), 0xE3: ("pickVarRandom", "W", "l", 1),
    0xE4: ("setBoxSet", "B", "", 0), 0xEC: ("getActorLayer", "", "n", 1),
    0xED: ("getObjectNewDir", "", "n", 1),
}

BINARY = {0x0E: "==", 0x0F: "!=", 0x10: ">", 0x11: "<", 0x12: "<=", 0x13: ">=", 0x14: "+",
          0x15: "-", 0x16: "*", 0x17: "/", 0x18: "&&", 0x19: "||", 0xD6: "&", 0xD7: "|"}

# Sub-opcodes: code -> (name, pops, has a message)
TEXT_OPS = {0x41: ("at", "nn"), 0x42: ("color", "n"), 0x43: ("clipped", "n"),
            0x45: ("center", ""), 0x47: ("left", ""), 0x48: ("overhead", ""),
            0x4A: ("mumble", ""), 0x4B: ("text", "s"), 0xFE: ("begin", ""), 0xFF: ("end", "")}
CURSOR_OPS = {0x90: ("cursorOn", ""), 0x91: ("cursorOff", ""), 0x92: ("userputOn", ""),
              0x93: ("userputOff", ""), 0x94: ("cursorSoftOn", ""), 0x95: ("cursorSoftOff", ""),
              0x96: ("userputSoftOn", ""), 0x97: ("userputSoftOff", ""),
              0x99: ("cursorImage", "nn"), 0x9A: ("cursorHotspot", "nn"),
              0x9C: ("charsetSet", "n"), 0x9D: ("charsetColors", "l"),
              0xD6: ("cursorTransparency", "n")}
RESOURCE_OPS = {0x74: ("clearHeap", ""), 0x77: ("loadObject", "nn")}
for _code, _name in enumerate((
        "loadScript", "loadSound", "loadCostume", "loadRoom", "nukeScript", "nukeSound",
        "nukeCostume", "nukeRoom", "lockScript", "lockSound", "lockCostume", "lockRoom",
        "unlockScript", "unlockSound", "unlockCostume", "unlockRoom"), 0x64):
    RESOURCE_OPS[_code] = (_name, "n")
RESOURCE_OPS.update({0x75: ("loadCharset", "n"), 0x76: ("nukeCharset", "n")})
ROOM_OPS = {0xAC: ("scroll", "nn"), 0xAE: ("screen", "nn"), 0xAF: ("palette", "nnnn"),
            0xB0: ("shakeOn", ""), 0xB1: ("shakeOff", ""), 0xB3: ("intensity", "nnn"),
            0xB4: ("saveGame", "nn"), 0xB5: ("fade", "n"), 0xB6: ("rgbIntensity", "nnnnn"),
            0xB7: ("shadow", "nnnnn"), 0xBA: ("transform", "nnnn"),
            0xBB: ("cycleSpeed", "nn"), 0xD5: ("newPalette", "n")}
ACTOR_OPS = {0x4C: ("costume", "n"), 0x4D: ("stepDist", "nn"), 0x4E: ("sound", "l"),
             0x4F: ("walkAnimation", "n"), 0x50: ("talkAnimation", "nn"),
             0x51: ("standAnimation", "n"), 0x52: ("animation", "nnn"), 0x53: ("default", ""),
             0x54: ("elevation", "n"), 0x55: ("animationDefault", ""), 0x56: ("palette", "nn"),
             0x57: ("talkColor", "n"), 0x58: ("name", "s"), 0x59: ("initAnimation", "n"),
             0x5B: ("width", "n"), 0x5C: ("scale", "n"), 0x5D: ("neverZclip", ""),
             0x5E: ("alwaysZclip", "n"), 0x5F: ("ignoreBoxes", ""), 0x60: ("followBoxes", ""),
             0x61: ("animationSpeed", "n"), 0x62: ("shadow", "n"), 0x63: ("textOffset", "nn"),
             0xC5: ("setCurrent", "n"), 0xC6: ("animVariable", "nn"), 0xD7: ("ignoreTurnsOn", ""),
             0xD8: ("ignoreTurnsOff", ""), 0xD9: ("new", ""), 0xE1: ("zClip", "n"),
             0xE3: ("layer", "n"), 0xE4: ("walkScript", "n"), 0xE5: ("stand", ""),
             0xE6: ("direction", "n"), 0xE7: ("turnToDirection", "n"), 0xE9: ("freeze", ""),
             0xEA: ("unfreeze", ""), 0xEB: ("talkScript", "n")}
VERB_OPS = {0x7C: ("image", "n"), 0x7D: ("name", "s"), 0x7E: ("color", "n"),
            0x7F: ("hiColor", "n"), 0x80: ("at", "nn"), 0x81: ("on", ""), 0x82: ("off", ""),
            0x83: ("delete", ""), 0x84: ("new", ""), 0x85: ("dimColor", "n"), 0x86: ("dim", ""),
            0x87: ("key", "n"), 0x88: ("center", ""), 0x89: ("nameString", "n"),
            0x8B: ("imageRoom", "nn"), 0x8C: ("backColor", "n"), 0xC4: ("setCurrent", "n"),
            0xFF: ("redraw", "")}
VERB_STATE_OPS = {0x8D: ("save", "nnn"), 0x8E: ("restore", "nnn"), 0x8F: ("delete", "nnn")}
SYSTEM_OPS = {0x9E: ("restart", ""), 0x9F: ("pause", ""), 0xA0: ("quit", "")}
WAIT_OPS = {0xA8: ("actor", "no"), 0xA9: ("message", ""), 0xAA: ("camera", ""),
            0xAB: ("sentence", ""), 0xE2: ("animation", "no"), 0xE8: ("turn", "no")}
ARRAY_OPS = {0xCD: ("assignString", "ns"), 0xD0: ("assignList", "ln"),
             0xD4: ("assign2D", "ln")}
SUB_TABLES = {0x6B: ("cursorCommand", CURSOR_OPS), 0x9B: ("resourceRoutines", RESOURCE_OPS),
              0x9C: ("roomOps", ROOM_OPS), 0x9D: ("actorOps", ACTOR_OPS),
              0x9E: ("verbOps", VERB_OPS), 0xA5: ("saveRestoreVerbs", VERB_STATE_OPS),
              0xA9: ("wait", WAIT_OPS), 0xAE: ("systemOps", SYSTEM_OPS),
              0xB4: ("printLine", TEXT_OPS), 0xB5: ("printText", TEXT_OPS),
              0xB6: ("printDebug", TEXT_OPS), 0xB7: ("printSystem", TEXT_OPS),
              0xB8: ("printActor", TEXT_OPS), 0xB9: ("printEgo", TEXT_OPS)}
DIM_TYPES = {0xC7: "int", 0xC8: "bit", 0xC9: "nibble", 0xCA: "byte", 0xCB: "string"}


class StackError(ValueError):
    pass


class Machine:
    def __init__(self, code: bytes):
        self.r = Reader(code)
        self.stack: list = []
        self.out: list[Instr] = []
        self.mark = 0
        self.at_target: dict[int, list] = {}   # destination -> [(jump, stack at the jump)]
        self.temps = 0

    def pop(self):
        if not self.stack:
            raise StackError(f"stack underflow at 0x{self.r.pos:x}")
        return self.stack.pop()

    def pop_list(self) -> list:
        count = self.pop()
        if not isinstance(count, int):
            raise StackError(f"list count is not a constant at 0x{self.r.pos:x}")
        return list(reversed([self.pop() for _ in range(count)]))

    def pops(self, spec: str) -> list:
        args = [self.pop_list() if t == "l" else self.pop()
                for t in reversed(spec.replace("s", "").replace("o", ""))]
        return list(reversed(args))

    def emit(self, opcode: int, name: str, args: list, target=None, result=None) -> None:
        instr = Instr(self.mark, opcode, name, args, target, result)
        self.out.append(instr)
        self.mark = self.r.pos
        if target is not None:
            self.at_target.setdefault(target, []).append((instr, list(self.stack)))

    def sub_op(self, opcode: int, name: str, table: dict) -> None:
        code = self.r.u8()
        if code not in table:
            raise ValueError(f"{name}: unknown sub-op 0x{code:02x} at 0x{self.r.pos - 1:x}")
        sub_name, spec = table[code]
        args = self.pops(spec)
        target = None
        if "s" in spec:
            args.append(self.r.text())
        if "o" in spec:
            target = self.r.jump()
        self.emit(opcode, name, [{"call": sub_name, "args": args}], target)

    def falls_through(self) -> bool:
        """Whether the code just before this point runs into it."""
        if self.mark != self.r.pos:        # pushes since the last instruction
            return True
        return not self.out or self.out[-1].name not in ("jumpRelative", "stopObjectCode")

    def join(self, position: int) -> None:
        """Reconcile the stacks of every path that reaches a jump destination.

        A switch keeps its value on the stack and each case pops it, so after an
        unconditional jump the stack is the one the jump left. A ternary leaves a
        different value on each path: those go through a temporary variable,
        assigned on every path.
        """
        arrivals = self.at_target[position]
        if not self.falls_through():
            self.stack = list(arrivals[0][1])
        for slot in range(len(self.stack)):
            values = [stack[slot] for _, stack in arrivals if slot < len(stack)]
            if all(value == self.stack[slot] for value in values):
                continue
            temp = {"var": f"T{self.temps}"}
            self.temps += 1
            if self.falls_through():
                self.emit(0, "move", [self.stack[slot]], result=temp)
            for jump, stack in arrivals:
                self.out.insert(self.out.index(jump),
                                Instr(jump.offset, 0, "move", [stack[slot]], result=temp))
            self.stack[slot] = temp
        self.mark = position

    def word_var(self) -> dict:
        return {"var": var_name(self.r.u16())}

    def byte_var(self) -> dict:
        return {"var": var_name(self.r.u8())}

    def step(self) -> None:
        r = self.r
        if r.pos in self.at_target:
            self.join(r.pos)
        opcode = r.u8()
        wide = opcode & 1          # paired byte/word forms differ in the low bit
        if opcode in (0x00, 0x01):
            self.stack.append(r.i16() if wide else r.u8())
        elif opcode in (0x02, 0x03):
            self.stack.append(self.word_var() if wide else self.byte_var())
        elif opcode in (0x06, 0x07):
            array = r.u16() if wide else r.u8()
            self.stack.append({"call": "arrayRead", "args": [array, self.pop()]})
        elif opcode in (0x0A, 0x0B):
            array = r.u16() if wide else r.u8()
            base = self.pop()
            self.stack.append({"call": "arrayRead2", "args": [array, self.pop(), base]})
        elif opcode == 0x0C:
            value = self.pop()
            self.stack += [value, value]
        elif opcode == 0x0D:
            self.stack.append({"call": "!", "args": [self.pop()]})
        elif opcode in BINARY:
            right, left = self.pop(), self.pop()
            self.stack.append({"call": BINARY[opcode], "args": [left, right]})
        elif opcode in (0x1A, 0xA7):
            # Kept as an instruction: the end of a switch is a jump target.
            self.emit(opcode, "pop", [self.pop()])
        elif opcode in (0x42, 0x43):
            target = self.word_var() if wide else self.byte_var()
            self.emit(opcode, "move", [self.pop()], result=target)
        elif opcode in (0x46, 0x47):
            array = r.u16() if wide else r.u8()
            value, index = self.pop(), self.pop()
            self.emit(opcode, "arrayWrite", [array, index, value])
        elif opcode in (0x4A, 0x4B):
            array = r.u16() if wide else r.u8()
            value, base, index = self.pop(), self.pop(), self.pop()
            self.emit(opcode, "arrayWrite2", [array, index, base, value])
        elif opcode in (0x4E, 0x4F):
            self.emit(opcode, "increment", [], result=self.word_var() if wide else self.byte_var())
        elif opcode in (0x56, 0x57):
            self.emit(opcode, "decrement", [], result=self.word_var() if wide else self.byte_var())
        elif opcode in (0x52, 0x53, 0x5A, 0x5B):
            array = r.u16() if wide else r.u8()
            name = "arrayInc" if opcode < 0x58 else "arrayDec"
            self.emit(opcode, name, [array, self.pop()])
        elif opcode in (0x5C, 0x5D):
            condition = self.pop()
            self.emit(opcode, "if" if opcode == 0x5C else "ifNot", [condition], r.jump())
        elif opcode == 0x73:
            self.emit(opcode, "jumpRelative", [], r.jump())
        elif opcode in (0xBC, 0xC0):
            code = r.u8()
            array = r.u16()
            if code == 0xCC:
                self.emit(opcode, "undim", [array])
            else:
                sizes = [self.pop()] if opcode == 0xBC else list(reversed([self.pop(), self.pop()]))
                self.emit(opcode, "dimArray", [array, DIM_TYPES[code], *sizes])
        elif opcode == 0xA4:
            code = r.u8()
            array = r.u16()
            sub_name, spec = ARRAY_OPS[code]
            args = self.pops(spec)
            if "s" in spec:
                args.append(r.text())
            self.emit(opcode, "arrayOps", [array, {"call": sub_name, "args": args}])
        elif opcode in SUB_TABLES:
            name, table = SUB_TABLES[opcode]
            self.sub_op(opcode, name, table)
        elif opcode in FUNCTIONS:
            name, immediate, spec, pushes = FUNCTIONS[opcode]
            args = []
            for token in immediate:
                if token == "W":
                    args.append(r.u16())
                elif token == "B":
                    args.append(r.u8())
            args += self.pops(spec)
            if "s" in immediate:
                args.append(r.text())
            if pushes:
                self.stack.append({"call": name, "args": args})
            else:
                self.emit(opcode, name, args)
        else:
            raise ValueError(f"unknown opcode 0x{opcode:02x} at 0x{r.pos - 1:x}")


def disassemble(code: bytes) -> list[Instr]:
    machine = Machine(code)
    while not machine.r.done:
        machine.step()
    return machine.out
