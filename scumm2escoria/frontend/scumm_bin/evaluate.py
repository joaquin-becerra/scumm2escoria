"""Run setup scripts to learn what a compiled game only says at runtime.

Actors, verbs, the ego and the first room aren't stored as data: scripts create
them. This runs those scripts on integer variables and reports every operation
that sets something up. What can't be computed is None, and a branch on None is
not taken.
"""
from __future__ import annotations

from typing import Callable, Optional

BINARY = {"+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b,
          "/": lambda a, b: a // b if b else None, "==": lambda a, b: int(a == b),
          "!=": lambda a, b: int(a != b), "<": lambda a, b: int(a < b),
          ">": lambda a, b: int(a > b), "<=": lambda a, b: int(a <= b),
          ">=": lambda a, b: int(a >= b), "&&": lambda a, b: int(bool(a and b)),
          "||": lambda a, b: int(bool(a or b)), "&": lambda a, b: a & b, "|": lambda a, b: a | b}
MAX_STEPS = 50000
MAX_DEPTH = 8
# Scripts are cooperative: one that was started returns to its caller when it yields.
YIELDS = {"breakHere", "delay", "delaySeconds", "delayMinutes", "delayFrames", "wait"}


class Evaluator:
    def __init__(self, scripts: Callable[[int, Optional[int]], Optional[list]],
                 on_op: Callable[[str, list, dict], None]):
        self.scripts = scripts          # (number, room) -> instructions
        self.on_op = on_op              # (operation, evaluated args, raw instruction)
        self.globals: dict = {}
        self.steps = 0
        self.stopped = False            # the game left the boot script for a room
        self.visited: set[int] = set()

    def value(self, e, frame: dict):
        if isinstance(e, bool) or isinstance(e, int):
            return e
        if isinstance(e, list):
            return [self.value(x, frame) for x in e]
        if not isinstance(e, dict):
            return None
        if "text" in e:
            return e["text"]
        if "var" in e:
            name = e["var"]
            if "index" in e:
                return self.globals.get((name, self.value(e["index"], frame)))
            scope = frame if name[0] in "LT" else self.globals
            return scope.get(name, 0 if name[0] in "LVB" else None)
        name, args = e["call"], e["args"]
        if name in BINARY and len(args) == 2:
            left, right = self.value(args[0], frame), self.value(args[1], frame)
            return None if left is None or right is None else BINARY[name](left, right)
        if name == "!":
            inner = self.value(args[0], frame)
            return None if inner is None else int(not inner)
        if name == "arrayRead":
            return self.globals.get((f"V{args[0]}", self.value(args[1], frame)))
        return None

    def assign(self, target: dict, value, frame: dict) -> None:
        name = target["var"]
        if "index" in target:
            self.globals[(name, self.value(target["index"], frame))] = value
        else:
            (frame if name[0] in "LT" else self.globals)[name] = value

    def run(self, instrs: list, args: Optional[list] = None, room: Optional[int] = None,
            depth: int = 0) -> None:
        frame = {f"L{n}": v for n, v in enumerate(args or [])}
        index = {}
        for n, i in enumerate(instrs):
            index.setdefault(i["offset"], n)
        pc = 0
        while pc < len(instrs) and not self.stopped:
            self.steps += 1
            if self.steps > MAX_STEPS:
                return
            i = instrs[pc]
            pc += 1
            name, raw = i["name"], i["args"]
            if name == "move":
                self.assign(i["result"], self.value(raw[0], frame), frame)
                self.on_op("assign", [i["result"], self.value(raw[0], frame)], i)
            elif name in ("increment", "decrement"):
                current = self.value(i["result"], frame) or 0
                self.assign(i["result"], current + (1 if name == "increment" else -1), frame)
            elif name == "arrayWrite":
                value, position = self.value(raw[2], frame), self.value(raw[1], frame)
                self.globals[(f"V{raw[0]}", position)] = value
                self.on_op("arrayWrite", [raw[0], position, value], i)
            elif name == "beginOverride":
                pc += 1         # the jump after it is only taken when the player skips
            elif name == "arrayOps" and raw[1]["call"] == "assignList":
                values, base = raw[1]["args"]
                for k, item in enumerate(values):
                    self.globals[(f"V{raw[0]}", (self.value(base, frame) or 0) + k)] = self.value(item, frame)
            elif name == "jumpRelative":
                pc = index.get(i["target"], len(instrs))
            elif name in ("if", "ifNot"):
                cond = self.value(raw[0], frame)
                if cond is not None and bool(cond) == (name == "if"):
                    pc = index.get(i["target"], len(instrs))
            elif name == "stopObjectCode" or (depth and name in YIELDS):
                return
            elif name in ("startScript", "startScriptQuick", "startScriptQuick2", "chainScript"):
                number, call_args = (raw[1], raw[2]) if name == "startScript" else (raw[0], raw[1])
                number = self.value(number, frame)
                body = self.scripts(number, room) if isinstance(number, int) else None
                if body is not None and depth < MAX_DEPTH and number not in self.visited:
                    self.visited.add(number)
                    steps = self.steps
                    self.run(body, self.value(call_args, frame), room, depth + 1)
                    self.steps = steps
            elif name == "loadRoom":
                room = self.value(raw[0], frame)
                self.on_op("loadRoom", [room], i)
                # Room 0 is no room: a boot script blanks the screen with it and goes on.
                self.stopped = room != 0
            else:
                self.report(name, raw, frame, i)

    def report(self, name: str, raw: list, frame: dict, instr: dict) -> None:
        for item in raw:
            if isinstance(item, dict) and "call" in item:
                self.on_op(f"{name}.{item['call']}", self.value(item["args"], frame), instr)
        if name in ("actorOps", "verbOps") and raw and not (isinstance(raw[0], dict) and "call" in raw[0]):
            self.on_op(f"{name}.setCurrent", [self.value(raw[0], frame)], instr)
            for item in raw[1:]:
                self.on_op(f"{name}.{item['call']}", self.value(item["args"], frame), instr)
        else:
            self.on_op(name, self.value(raw, frame), instr)
