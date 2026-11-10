"""Frontend for ScummC projects (.scc/.sch scripts, .scost costumes, .box files).

Stage 1 only extracts the declarative parts: rooms (background, z-planes,
walk boxes) and costumes. Script bodies are skipped for now.
"""
from __future__ import annotations

import glob
import os
import re
import struct
from dataclasses import dataclass
from typing import Iterator, Optional

from ..ir import Sound, Actor, Anim, AnimStep, Box, Costume, Game, Obj, ObjState, Picture, Room, ScriptDef, Verb


# --------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------

@dataclass
class Token:
    kind: str    # ident, number, string, punct
    value: str
    line: int


_TOKEN_RE = re.compile(r"""
    (?P<ws>[ \t\r]+)
  | (?P<nl>\n)
  | (?P<lcomment>//[^\n]*)
  | (?P<bcomment>/\*.*?\*/)
  | (?P<pre>\#[^\n]*)
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<char>'(?:\\.|[^'\\])')
  | (?P<number>0[xX][0-9a-fA-F]+|-?\d+)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct>::|==|!=|<=|>=|&&|\|\||\+\+|--|[{}()\[\];,=<>+\-*/%!&|@:?.])
""", re.VERBOSE | re.DOTALL)


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    pos, line = 0, 1
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise SyntaxError(f"line {line}: unexpected character {text[pos]!r}")
        kind = m.lastgroup
        value = m.group()
        if kind == "string":
            tokens.append(Token("string", re.sub(r'\\(["\\])', r"\1", value[1:-1]), line))
        elif kind == "char":
            # Character literals are just numbers in SCUMM.
            tokens.append(Token("number", str(ord(value[-2])), line))
        elif kind in ("number", "ident", "punct"):
            tokens.append(Token(kind, value, line))
        line += value.count("\n")
        pos = m.end()
    return tokens


class TokenStream:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> Optional[Token]:
        i = self.pos + offset
        return self.tokens[i] if i < len(self.tokens) else None

    def next(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def accept(self, value: str) -> bool:
        tok = self.peek()
        if tok is not None and tok.value == value and tok.kind != "string":
            self.pos += 1
            return True
        return False

    def expect(self, value: str) -> Token:
        tok = self.next()
        if tok.value != value:
            raise SyntaxError(f"line {tok.line}: expected {value!r}, got {tok.value!r}")
        return tok

    def skip_block(self) -> None:
        """Skip a balanced {...} block; the opening brace is the next token."""
        self.expect("{")
        depth = 1
        while depth:
            tok = self.next()
            if tok.kind == "punct":
                if tok.value == "{":
                    depth += 1
                elif tok.value == "}":
                    depth -= 1

    def skip_statement(self) -> None:
        """Skip up to and including the next ';' at the current nesting level."""
        depth = 0
        while True:
            tok = self.next()
            if tok.kind != "punct":
                continue
            if tok.value in "({[":
                depth += 1
            elif tok.value in ")}]":
                depth -= 1
            elif tok.value == ";" and depth == 0:
                return


def _parse_value(ts: TokenStream):
    """A string, number, identifier or {list} on the right side of '='."""
    if ts.accept("{"):
        items = []
        while not ts.accept("}"):
            items.append(_parse_value(ts))
            ts.accept(",")
        return items
    tok = ts.next()
    if tok.kind == "number":
        return int(tok.value, 0)
    return tok.value


# --------------------------------------------------------------------------
# Rooms
# --------------------------------------------------------------------------

DECL_TYPES = ("int", "bit", "nibble", "byte", "char", "word")


def parse_rooms(path: str) -> tuple[list[Room], list[Obj], list[str]]:
    """Extract rooms (resource attributes), their objects and room variables."""
    base = os.path.dirname(path)
    ts = TokenStream(tokenize(open(path, encoding="latin-1").read()))
    rooms: list[Room] = []
    objects: list[Obj] = []
    variables: list[str] = []
    while ts.peek() is not None:
        tok = ts.next()
        if not (tok.kind == "ident" and tok.value == "room"):
            continue
        name = ts.next().value
        if ts.accept(";"):      # a forward declaration
            continue
        ts.expect("{")
        attrs = {}
        while not ts.accept("}"):
            head = ts.peek()
            if head.value == "object" and ts.peek(2) is not None and ts.peek(2).value == "{":
                ts.next()
                objects.append(parse_object(ts, name, base))
                continue
            if head.value in DECL_TYPES and ts.peek(1).value != "*":
                ts.next()
                while not ts.accept(";"):
                    t = ts.next()
                    if t.kind == "ident":
                        variables.append(t.value)
                continue
            if head.kind == "ident" and ts.peek(1).value == "=":
                key = ts.next().value
                ts.next()
                attrs[key] = _parse_value(ts)
                ts.accept(";")
                continue
            # Declarations, scripts, objects...: skip to end of statement/block.
            while True:
                t = ts.peek()
                if t.kind == "punct" and t.value == "{":
                    ts.skip_block()
                    ts.accept(";")
                    break
                if t.kind == "punct" and t.value == ";":
                    ts.next()
                    break
                ts.next()

        room = Room(name=name, source=path)
        if isinstance(attrs.get("image"), str):
            room.background = os.path.normpath(os.path.join(base, attrs["image"]))
        if isinstance(attrs.get("zplane"), list):
            room.zplanes = [os.path.normpath(os.path.join(base, p)) for p in attrs["zplane"]]
        if isinstance(attrs.get("boxd"), str):
            room.boxes = parse_box_file(os.path.join(base, attrs["boxd"]))
        rooms.append(room)
    return rooms, objects, variables


def parse_object(ts: TokenStream, room: str, base: str) -> Obj:
    """`NAME { attr = value; ... verb(...) { ... } }`; 'object' already consumed."""
    from .scummc_stmt import StmtParser

    obj = Obj(name=ts.next().value, room=room)
    ts.expect("{")
    attrs = {}
    while not ts.accept("}"):
        key = ts.next().value
        if key == "verb":
            while not ts.accept(")"):
                ts.next()
            ts.expect("{")
            start = ts.pos
            ts.pos -= 1
            ts.skip_block()
            parser = StmtParser(ts.tokens[start:ts.pos - 1])
            # The body of verb() is an implicit switch on the verb.
            parser.ts.tokens.append(Token("punct", "}", 0))
            obj.verbs = parser.case_list()
            continue
        ts.expect("=")
        attrs[key] = _parse_value(ts)
        ts.accept(";")

    obj.display_name = attrs.get("name")
    for k in ("x", "y", "w", "h", "hs_x", "hs_y", "state"):
        if isinstance(attrs.get(k), int):
            setattr(obj, k, attrs[k])
    obj.dir = attrs.get("dir")
    cls = attrs.get("class", [])
    obj.classes = cls if isinstance(cls, list) else [cls]
    if isinstance(attrs.get("trans"), int):
        obj.trans = attrs["trans"]
    obj.parent = attrs.get("parent")
    obj.parent_state = attrs.get("parent_state")
    for st in attrs.get("states", []):
        image = os.path.normpath(os.path.join(base, st[2])) if len(st) > 2 and st[2] else None
        planes = [os.path.normpath(os.path.join(base, p)) if p else None
                  for p in (st[3] if len(st) > 3 else [])]
        obj.states.append(ObjState(st[0], st[1], image, planes))
    return obj


# --------------------------------------------------------------------------
# Box files
# --------------------------------------------------------------------------

_BOX_RECORD = 21   # 1 pad byte, 8 x int16 LE corners, mask, flags, uint16 LE scale


def _blocks(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    pos = 0
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4]
        size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        yield tag, data[pos + 8:pos + size]
        pos += size


def parse_box_file(path: str) -> list[Box]:
    """Parse a ScummC box file (boxd + BOXM + SCAL blocks)."""
    boxes = []
    for tag, body in _blocks(open(path, "rb").read()):
        if tag.lower() != b"boxd":
            continue
        for i in range(len(body) // _BOX_RECORD):
            rec = body[i * _BOX_RECORD:(i + 1) * _BOX_RECORD]
            c = struct.unpack("<8h", rec[1:17])
            boxes.append(Box(
                index=i,
                corners=[(c[0], c[1]), (c[2], c[3]), (c[4], c[5]), (c[6], c[7])],
                mask=rec[17],
                flags=rec[18],
                scale=struct.unpack("<H", rec[19:21])[0],
            ))
    return boxes


# --------------------------------------------------------------------------
# Costumes
# --------------------------------------------------------------------------

def parse_costume(path: str) -> Costume:
    base = os.path.dirname(path)
    name = os.path.splitext(os.path.basename(path))[0]
    cost = Costume(name=name, source=path)
    ts = TokenStream(tokenize(open(path, encoding="latin-1").read()))

    while ts.peek() is not None:
        tok = ts.next()
        if tok.value == "palette" and ts.accept("("):
            cost.palette = os.path.normpath(os.path.join(base, ts.next().value))
            ts.expect(")")
        elif tok.value == "flags" and ts.accept("="):
            while not ts.accept(";"):
                if ts.next().value == "FLIP":
                    cost.flip = True
        elif tok.value == "picture":
            pic_name = ts.next().value
            ts.expect("=")
            attrs = {}
            ts.expect("{")
            while not ts.accept("}"):
                key = ts.next().value
                ts.expect("=")
                attrs[key] = _parse_value(ts)
                ts.accept(",")
            ts.accept(";")
            pos = tuple(attrs.get("position", (0, 0)))
            if "path" in attrs:
                cost.pictures[pic_name] = Picture(
                    pic_name, os.path.normpath(os.path.join(base, attrs["path"])), pos)
            elif "glob" in attrs:
                # name + 2-digit index, e.g. walking_e00 -> walking_e0000..
                for i, f in enumerate(sorted(glob.glob(os.path.join(base, attrs["glob"])))):
                    full = f"{pic_name}{i:02d}"
                    cost.pictures[full] = Picture(full, os.path.normpath(f), pos)
        elif tok.value == "limb":
            cost.limbs.append(ts.next().value)
            ts.accept(";")
        elif tok.value == "anim":
            anim = Anim(name=ts.next().value)
            if ts.accept("@"):
                anim.index = int(ts.next().value, 0)
            ts.expect("=")
            ts.expect("{")
            while not ts.accept("}"):
                direction = ts.next().value
                ts.expect("=")
                ts.expect("{")
                steps = []
                while not ts.accept("}"):
                    if ts.accept(","):
                        continue
                    limb = ts.next().value
                    step = AnimStep(limb=limb)
                    ts.expect("(")
                    while not ts.accept(")"):
                        item = ts.next().value
                        if item in ("HIDE", "SHOW"):
                            step.command = item
                        elif item != ",":
                            step.frames.append(item)
                    if ts.accept("!"):
                        if ts.next().value == "LOOP":
                            step.loop = False
                    elif ts.accept("LOOP"):
                        step.loop = True
                    steps.append(step)
                ts.accept(";")
                anim.directions[direction] = steps
            ts.accept(";")
            cost.anims[anim.name] = anim
    return cost


# --------------------------------------------------------------------------
# Scripts and calls
# --------------------------------------------------------------------------

@dataclass
class Script:
    room: str
    name: str
    tokens: list[Token]      # body, without the outer braces
    params: list[str] = None


def parse_scripts(path: str) -> list[Script]:
    """All `script NAME(...) { ... }` bodies, with the room they belong to."""
    ts = TokenStream(tokenize(open(path, encoding="latin-1").read()))
    scripts = []
    room = None
    depth = 0
    while ts.peek() is not None:
        tok = ts.next()
        if tok.kind == "punct" and tok.value == "{":
            depth += 1
        elif tok.kind == "punct" and tok.value == "}":
            depth -= 1
        elif depth == 0 and tok.value == "room" and tok.kind == "ident":
            room = ts.peek().value
        elif depth == 1 and tok.value == "script" and tok.kind == "ident":
            name = ts.next().value
            head = []
            while not (ts.peek().kind == "punct" and ts.peek().value == "{"):
                t = ts.next()
                if t.value == ";":      # just a declaration
                    break
                head.append(t)
            else:
                # Parameters: the last identifier of each comma-separated group.
                params, group = [], []
                for t in head:
                    if t.value in (",", ")"):
                        if group:
                            params.append(group[-1])
                        group = []
                    elif t.kind == "ident":
                        group.append(t.value)
                start = ts.pos + 1
                ts.skip_block()
                scripts.append(Script(room, name, ts.tokens[start:ts.pos - 1], params))
    return scripts


@dataclass
class Call:
    name: str
    args: list[list[Token]]
    line: int


def iter_calls(tokens: list[Token]) -> Iterator[Call]:
    """Every `name(args...)` in a token list, in source order (nested ones too)."""
    for i, tok in enumerate(tokens[:-1]):
        if tok.kind != "ident" or tokens[i + 1].value != "(":
            continue
        args: list[list[Token]] = [[]]
        depth = 0
        for t in tokens[i + 2:]:
            if t.kind == "punct" and t.value in "([{":
                depth += 1
            elif t.kind == "punct" and t.value in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif t.kind == "punct" and t.value == "," and depth == 0:
                args.append([])
                continue
            args[-1].append(t)
        yield Call(tok.value, [a for a in args if a], tok.line)


def _literal(arg: list[Token], defines: dict[str, int]):
    """Value of a simple argument: number, #define, string or identifier."""
    if len(arg) != 1:
        return None
    tok = arg[0]
    if tok.kind == "number":
        return int(tok.value, 0)
    if tok.kind == "ident" and tok.value in defines:
        return defines[tok.value]
    return tok.value


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


_DEFINE = re.compile(r"^\s*#define\s+(\w+)\s+(-?(?:0[xX][0-9a-fA-F]+|\d+))\b", re.MULTILINE)


# --------------------------------------------------------------------------
# Game-level discovery
# --------------------------------------------------------------------------

def load_game(src_dir: str) -> Game:
    src_dir = os.path.abspath(src_dir)
    game = Game(name=os.path.basename(src_dir.rstrip("/")))
    # Credits and license: the source's own readme/license files, plus the
    # license of the repository it lives in (the parent directory).
    for d, patterns in ((src_dir, ("README*", "LICENSE*", "COPYING*")),
                        (os.path.dirname(src_dir), ("LICENSE*", "COPYING*"))):
        for pattern in patterns:
            game.docs += sorted(p for p in glob.glob(os.path.join(d, pattern)) if os.path.isfile(p))

    sources = sorted(glob.glob(os.path.join(src_dir, "*.scc")))
    for path in sources:
        rooms, objects, variables = parse_rooms(path)
        for room in rooms:
            game.rooms[room.name] = room
        for obj in objects:
            game.objects[obj.key] = obj
        game.globals += [v for v in variables if v not in game.globals]
    # Global variables declared in headers, outside any room.
    for path in sorted(glob.glob(os.path.join(src_dir, "*.sch"))):
        text = _strip_comments(open(path, encoding="latin-1").read())
        for typ, names in re.findall(r"^\s*(" + "|".join(DECL_TYPES) + r")\s+([^*;]+);", text, re.MULTILINE):
            for n in re.findall(r"\w+", names):
                if n not in game.globals:
                    game.globals.append(n)

    for path in sorted(glob.glob(os.path.join(src_dir, "costumes", "*.scost"))):
        cost = parse_costume(path)
        game.costumes[cost.name] = cost

    defines: dict[str, int] = {}
    for path in sorted(glob.glob(os.path.join(src_dir, "*.sc[ch]"))):
        for name, val in _DEFINE.findall(open(path, encoding="latin-1").read()):
            defines[name] = int(val, 0)

    scripts = [s for path in sources for s in parse_scripts(path)]
    cost_files: dict[str, str] = {}
    for path in sources:
        for var, fname in re.findall(r'\bcost\s+(\w+)\s*=\s*"([^"]+)"',
                                     open(path, encoding="latin-1").read()):
            cost_files[var] = os.path.splitext(fname)[0]

    # Actor setup: setCurrentActor(a) followed by setActor*(...) calls.
    ego_candidates: list[str] = []
    for script in scripts:
        current = None
        toks = script.tokens
        for i, tok in enumerate(toks[:-3]):
            if tok.value == "VAR_EGO" and toks[i + 1].value == "=" and toks[i + 3].value == ";":
                ego_candidates.append(toks[i + 2].value)
        for call in iter_calls(toks):
            args = [_literal(a, defines) for a in call.args]
            if call.name == "setCurrentActor" and args:
                current = args[0]
                continue
            if current is None or current == "VAR_EGO":
                continue
            actor = game.actors.get(current)
            if call.name == "setActorCostume" and args:
                actor = game.actors.setdefault(current, Actor(name=current))
                actor.costume = cost_files.get(args[0], args[0])
            if actor is None:
                continue
            if call.name == "setActorName" and args:
                actor.display_name = args[0]
            elif call.name == "setActorTalkColor" and isinstance(args[0], int):
                actor.talk_color = args[0]
            elif call.name == "setActorAnimSpeed" and isinstance(args[0], int):
                actor.anim_speed = args[0]
            elif call.name == "setActorWalkSpeed" and len(args) == 2:
                actor.walk_speed = (args[0], args[1])
            elif call.name == "setActorElevation" and isinstance(args[0], int):
                actor.elevation = args[0]
            elif call.name == "setActorWidth" and args and isinstance(args[0], int):
                actor.width = args[0]
            elif call.name == "setActorTalkPos" and len(args) == 2 and all(isinstance(a, int) for a in args):
                actor.talk_pos = (args[0], args[1])
            elif call.name == "setActorIgnoreBoxes":
                actor.ignore_boxes = True
            elif call.name == "setObjectClass" and args and args[0] == current:
                names = [t.value for t in call.args[1] if t.kind == "ident"]
                if "ClassUntouchable" in names:
                    actor.untouchable = True
                actor.classes += [n for n in names if n not in actor.classes]

    # VAR_EGO is also assigned from script variables; keep the first real actor.
    game.ego = next((e for e in ego_candidates if e in game.actors), None)

    # Initial placements: setupActors first, then each room's entry script.
    # Later calls win, so the room ends up as it looks once the intro is over.
    placing = [s for s in scripts if s.name == "setupActors"] + \
              [s for s in scripts if s.name == "entry"]
    for script in placing:
        for call in iter_calls(script.tokens):
            if call.name != "putActorAt" or len(call.args) != 4:
                continue
            who, x, y, room = (_literal(a, defines) for a in call.args)
            if who == "VAR_EGO":
                who = game.ego
            if script.name == "setupActors" and who in game.actors and isinstance(room, str):
                game.actors[who].start_room = room
            if room in game.rooms and who in game.actors and \
                    isinstance(x, int) and isinstance(y, int):
                game.rooms[room].actor_positions[who] = (x, y)

    # Objects linked to actors: actorObject[actor] = Room::object;
    for script in scripts:
        toks = script.tokens
        for i in range(len(toks) - 8):
            if toks[i].value == "actorObject" and toks[i + 1].value == "[" and toks[i + 3].value == "]" \
                    and toks[i + 4].value == "=":
                rhs = toks[i + 5:i + 8]
                if len(rhs) == 3 and rhs[1].value == "::":
                    key = f"{rhs[0].value}::{rhs[2].value}"
                else:
                    key = f"{script.room}::{rhs[0].value}"
                if key in game.objects:
                    game.actor_objects[toks[i + 2].value] = key

    # Starting inventory: pickupObject(Room::obj, ...) in setupActors.
    for script in scripts:
        if script.name != "setupActors":
            continue
        for call in iter_calls(script.tokens):
            if call.name == "pickupObject" and call.args:
                a = call.args[0]
                key = f"{a[0].value}::{a[2].value}" if len(a) == 3 else f"{script.room}::{a[0].value}"
                if key in game.objects:
                    game.start_inventory.append(key)

    from .scummc_eval import SetupEvaluator
    from .scummc_stmt import StmtParser

    # Verbs. Declarations give them numbers (`verb a, b @ 100;`); setup scripts
    # are evaluated to get labels, positions, colors and images.
    declared: list[tuple[str, Optional[int]]] = []
    for path in sorted(glob.glob(os.path.join(src_dir, "*.sch"))):
        for decl in re.findall(r"^\s*verb\s+([^;]+);", _strip_comments(open(path, encoding="latin-1").read()),
                               re.MULTILINE):
            for name, num in re.findall(r"(\w+)(?:\s*@\s*(\d+))?", decl):
                if not name.isdigit():
                    declared.append((name, int(num) if num else None))
    for script in scripts:
        for decl in re.findall(r"\bverb\s+(\w+)\s*;", " ".join(t.value for t in script.tokens)):
            declared.append((decl, None))
    verbs = {n: Verb(n) for n, _ in declared}
    ids = {n: num for n, num in declared if num is not None}
    nxt = 1
    for n, _ in declared:
        if n not in ids:
            while nxt in ids.values():
                nxt += 1
            ids[n] = nxt
    by_id = {v: k for k, v in ids.items()}
    # Room-level verbs declared inside .scc rooms (e.g. `verb backgroundVerb;`).
    for path in sources:
        for name in re.findall(r"^\s*verb\s+(\w+)\s*;", _strip_comments(open(path, encoding="latin-1").read()),
                               re.MULTILINE):
            if name not in verbs:
                verbs[name] = Verb(name)
                ids[name] = max(ids.values(), default=0) + 1
                by_id[ids[name]] = name

    # Charsets: `chset name [@ n] = "file.char";` -> fonts/<file>.bmp
    gui_slot = None
    for path in sources:
        for name, slot, fname in re.findall(r'\bchset\s+(\w+)\s*(?:@\s*(\d+))?\s*=\s*"([^"]+)"',
                                            _strip_comments(open(path, encoding="latin-1").read())):
            bmp = os.path.join(src_dir, "fonts", os.path.splitext(fname)[0] + ".bmp")
            if os.path.exists(bmp):
                game.charsets[name] = bmp
                if slot == "1":
                    gui_slot = name
    game.gui_charset = gui_slot

    def strip_room(name):
        return name.split("::")[-1] if isinstance(name, str) else name

    state = {"verb": None, "charset": None}

    def on_call(name, args):
        verb = state["verb"]
        if name == "initCharset" and args and isinstance(args[0], str):
            state["charset"] = strip_room(args[0])
        elif name == "setCurrentVerb" and args:
            vid = args[0] if isinstance(args[0], int) else ids.get(args[0])
            state["verb"] = verbs.get(by_id.get(vid)) if vid is not None else None
        elif verb is None:
            return
        elif name == "setVerbName" and args and isinstance(args[0], str):
            verb.label = args[0]
            verb.charset = state["charset"]
        elif name == "setVerbKey" and args and isinstance(args[0], int):
            verb.key = chr(args[0])
        elif name == "setVerbXY" and len(args) == 2 and all(isinstance(a, int) for a in args):
            verb.pos = (args[0], args[1])
        elif name == "verbCenter":
            verb.centered = True
        elif name == "setVerbOn":
            verb.on = True
        elif name in ("setVerbColor", "setVerbHiColor", "setVerbDimColor", "setVerbBackColor") \
                and args and isinstance(args[0], int):
            attr = {"setVerbColor": "color", "setVerbHiColor": "hi_color",
                    "setVerbDimColor": "dim_color", "setVerbBackColor": "back_color"}[name]
            setattr(verb, attr, args[0])
        elif name == "setVerbObject" and len(args) == 2 and isinstance(args[0], str):
            key = f"{args[1]}::{strip_room(args[0])}"
            if key in game.objects:
                verb.image = key

    constants = dict(defines)
    constants.update(ids)
    for script in scripts:
        names = {t.value for t in script.tokens if t.kind == "ident"}
        if "setCurrentVerb" in names:
            state["verb"] = None
            SetupEvaluator(constants, on_call).run(StmtParser(script.tokens).parse_all())
    game.verbs = list(verbs.values())

    # Fonts for text: what the boot script (main) leaves active, and what dialogs use.
    for script in scripts:
        calls = [c for c in iter_calls(script.tokens) if c.name == "initCharset" and c.args]
        if not calls:
            continue
        last = strip_room("".join(t.value for t in calls[-1].args[0]))
        if script.name == "main":
            game.text_charset = last
        elif "dialog" in (script.room + script.name).lower() and game.dialog_charset is None:
            game.dialog_charset = strip_room("".join(t.value for t in calls[0].args[0]))

    # Cursor: setCursorImage(object, room) + setCursorTransparency(color).
    for script in scripts:
        for call in iter_calls(script.tokens):
            args = [_literal(a, defines) for a in call.args]
            if call.name == "setCursorImage" and len(args) == 2:
                obj = game.objects.get(f"{args[1]}::{args[0]}")
                if obj and obj.states and obj.states[0].image:
                    game.cursor = game.cursor or {}
                    game.cursor.update(image=obj.states[0].image,
                                       hotspot=(obj.states[0].hs_x, obj.states[0].hs_y))
            elif call.name == "setCursorTransparency" and args and isinstance(args[0], int):
                game.cursor = game.cursor or {}
                game.cursor["transparent"] = args[0]

    # Room entry scripts, and the "click anywhere" script some rooms install
    # (`VAR_VERB_SCRIPT = proceed;` in a title screen's entry).
    for script in scripts:
        room = game.rooms.get(script.room)
        if room is None or script.name != "entry":
            continue
        room.entry = StmtParser(script.tokens).parse_all()
        toks = script.tokens
        for i in range(len(toks) - 3):
            if toks[i].value == "VAR_VERB_SCRIPT" and toks[i + 1].value == "=":
                target = toks[i + 2].value
                for other in scripts:
                    if other.room == script.room and other.name == target:
                        room.click_script = StmtParser(other.tokens).parse_all()

    # The fallback for verbs an object doesn't handle: defaultAction's switch(vrb).
    from .scummc_stmt import StmtParser
    for script in scripts:
        if script.name == "defaultAction":
            for stmt in StmtParser(script.tokens).parse_all():
                if stmt.kind == "switch":
                    game.default_action = stmt.cases
                    break

    # Actors that scripts put into a room where they don't start.
    for script in scripts:
        for call in iter_calls(script.tokens):
            if call.name != "putActorAt" or len(call.args) != 4:
                continue
            who, x, y, room = (_literal(a, defines) for a in call.args)
            if who == "VAR_EGO" or who == game.ego or room not in game.rooms:
                continue
            r = game.rooms[room]
            if who in game.actors and who not in r.actor_positions and isinstance(x, int) \
                    and isinstance(y, int):
                r.scripted_actors.setdefault(who, (x, y))

    # Every script, for the translator (calls to scripts are inlined).
    for script in scripts:
        game.scripts[f"{script.room}::{script.name}"] = ScriptDef(
            script.room, script.name, script.params or [], StmtParser(script.tokens).parse_all())
    # Scripts the frontend already read (verb and actor setup): calls to them are no-ops.
    for script in scripts:
        names = {t.value for t in script.tokens if t.kind == "ident"}
        # (Runtime verb lists, like dialog choices with setVerbNameString, are not setup.)
        creates = ({"initVerb", "setVerbXY"} <= names and "setVerbNameString" not in names) \
            or ({"initActor", "setActorCostume"} <= names)
        if creates and script.name != "entry" and not script.params:
            game.setup_scripts.append(script.name)

    # The sentence script asks objects for a preposition (a 2nd object) only for
    # some verbs: `isAnyOf(vrb, [ Use, Give ])` before startObject2(.., Preposition, ..).
    for script in scripts:
        vals = [t.value for t in script.tokens]
        if "Preposition" in vals and "isAnyOf" in vals:
            i = vals.index("isAnyOf")
            j = vals.index("]", i)
            game.target_verbs = [v for v in vals[i:j] if v in verbs]
            break
    # Inventory items get a default verb when the pointer is on them
    # (mouseWatch: `vrb = Use;` next to findInventory).
    for script in scripts:
        vals = [t.value for t in script.tokens]
        if "findInventory" in vals:
            k = vals.index("findInventory")
            for i in range(k, len(vals) - 3):
                if vals[i] == "vrb" and vals[i + 1] == "=" and vals[i + 2] in verbs and vals[i + 3] == ";":
                    game.inventory_verb = vals[i + 2]
                    break
            if game.inventory_verb:
                break

    game.defines = dict(defines)

    # Dialog choices layout (dialog.scc's showDialog): lines are verbs with a
    # runtime text (setVerbNameString) at setVerbXY(x, y + step * d), at most
    # MAX lines (the `d < MAX` loop); arrows are verbs with a fixed glyph.
    for script in scripts:
        names = {t.value for t in script.tokens}
        if "setVerbNameString" not in names:
            continue
        ui: dict = {"arrows": []}
        label = None
        for call in iter_calls(script.tokens):
            if call.name == "setVerbName" and call.args and call.args[0][0].kind == "string":
                label = call.args[0][0].value
            elif call.name == "setVerbXY" and len(call.args) == 2:
                xs = [t.value for t in call.args[0]]
                ys = [t for t in call.args[1]]
                if len(xs) == 1 and len(ys) == 5 and ys[1].value == "+" and ys[3].value == "*" \
                        and ys[4].kind == "ident" and ys[4].value not in defines:
                    ui.update(x=int(xs[0], 0), y=int(ys[0].value, 0), step=int(ys[2].value, 0))
                elif label is not None:
                    ev = SetupEvaluator(dict(defines), lambda *a: None)
                    x, y = (ev.value([(t.kind, t.value) for t in a]) for a in call.args)
                    if isinstance(x, int) and isinstance(y, int):
                        ui["arrows"].append({"x": x, "y": y, "label": label})
                    label = None
        toks = [t.value for t in script.tokens]
        for i in range(len(toks) - 2):
            if toks[i] == "d" and toks[i + 1] == "<" and toks[i + 2] in defines:
                ui["max"] = defines[toks[i + 2]]
        if "x" in ui:
            game.dialog_ui = ui
            break

    # Start room: the boot script's normal path, i.e. the `default:` of
    # switch(bootParam) in main (other cases are debug shortcuts).
    for script in scripts:
        if script.name != "main":
            continue
        for st in StmtParser(script.tokens).parse_all():
            if st.kind == "switch" and [v for _, v in st.expr] == ["bootParam"]:
                for case in st.cases:
                    if not case.labels:
                        for call in iter_calls([Token(k, v, 0) for s2 in case.body for k, v in s2.expr]):
                            if call.name == "startRoom" and call.args:
                                game.start_room = call.args[0][0].value
                            elif call.name == "screenEffect" and call.args:
                                value = _literal(call.args[0], defines)
                                if isinstance(value, int):
                                    game.boot_effect = value
    if game.start_room not in game.rooms:
        game.start_room = None
    for script in scripts:
        if game.start_room:
            break
        for call in iter_calls(script.tokens):
            target = _literal(call.args[0], defines) if call.args else None
            if call.name == "startRoom" and target in game.rooms and game.rooms[target].boxes:
                game.start_room = target
                break

    # Game speed: VAR_TIMER_NEXT = n in the boot script (one game frame every n
    # ticks of SCUMM's 60 Hz timer). Actors move and animate once per frame.
    for script in scripts:
        if script.name != "main":
            continue
        toks = script.tokens
        for i in range(len(toks) - 3):
            if toks[i].value in ("VAR_TIMER_NEXT", "VAR_CHARINC") and toks[i + 1].value == "=" \
                    and toks[i + 2].kind == "number" and toks[i + 3].value == ";":
                value = int(toks[i + 2].value, 0)
                if toks[i].value == "VAR_TIMER_NEXT":
                    game.timer_next = value
                else:
                    game.char_inc = value

    # Engine hooks the boot script installs (VAR_xxx = script;).
    for script in scripts:
        if script.name != "main":
            continue
        toks = script.tokens
        for i in range(len(toks) - 3):
            if toks[i + 1].value == "=" and toks[i + 3].value == ";" and toks[i + 2].kind == "ident":
                target = f"{script.room}::{toks[i + 2].value}"
                if toks[i].value == "VAR_CUTSCENE_START_SCRIPT":
                    game.cutscene_start = target
                elif toks[i].value == "VAR_CUTSCENE_END_SCRIPT":
                    game.cutscene_end = target
                elif toks[i].value == "VAR_VERB_SCRIPT":
                    game.verb_script = target

    # The game palette: the start room's background, else any room's.
    for room in [game.rooms.get(game.start_room)] + list(game.rooms.values()):
        if room is not None and room.background:
            game.palette = room.background
            break
    # Sound resources: `sound x = "name.soun";` is built from sounds/name.voc
    # (ScummC's example.mak), `voice x = { "path.voc" };` names its file.
    for path in sorted(glob.glob(os.path.join(src_dir, "*.scc"))):
        text = _strip_comments(open(path, encoding="latin-1").read())
        room = re.search(r"\broom\s+(\w+)", text)
        if not room:
            continue
        for name, file in re.findall(r'\bsound\s+(\w+)\s*=\s*"([^"]+)"\s*;', text):
            voc = os.path.join(src_dir, "sounds", os.path.splitext(file)[0] + ".voc")
            if os.path.exists(voc):
                game.sounds[f"{room.group(1)}::{name}"] = Sound(room.group(1), name, voc)
        for name, file in re.findall(r'\bvoice\s+(\w+)\s*=\s*\{\s*"([^"]+)"', text):
            voc = os.path.join(src_dir, file)
            if os.path.exists(voc):
                game.sounds[f"{room.group(1)}::{name}"] = Sound(room.group(1), name, voc, voice=True)

    from .scummc_roam import find_roaming
    game.roaming = find_roaming(game)
    return game
