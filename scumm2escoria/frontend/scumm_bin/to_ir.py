"""Build ir.Game from a bundle (see bundle.py).

Names didn't survive compilation, so they're generated (Room7, obj_36, actor_5...)
except where the game itself gives one: room names, verb labels, object names.
The two things only scripts know are recovered by running them (evaluate.py) and
by decompiling them (decompile.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Optional

from PIL import Image

from ...ir import (Actor, Anim, AnimStep, Box, Costume, FlatBlock, Game, Obj, ObjState, Picture, Room,
                   ScriptDef, Sound, Verb)
from ..scummc import Token, tokenize
from ..scummc_stmt import StmtParser
from .decompile import Namer, flat_blocks, fold, script_text, scummc_text, verb_text
from .engine_vars import engine_vars
from .evaluate import Evaluator
from .script import Instr

DIRECTIONS = {0: "W", 1: "E", 2: "S", 3: "N"}
OBJECT_DIRECTIONS = {0: "WEST", 1: "EAST", 2: "SOUTH", 3: "NORTH"}
# Costume frames with a role in the engine; the rest are only numbered.
STANDARD_FRAMES = {1: "init", 2: "walk", 3: "stand", 4: "talkStart", 5: "talkStop"}
# Other labels for the verbs the translator knows.
VERB_SYNONYMS = {"Examine": "LookAt", "Look": "LookAt", "Take": "PickUp", "Get": "PickUp",
                 "Talk": "TalkTo", "Walk": "WalkTo", "Goto": "WalkTo"}
MIN_WALK_AREA = 16
FIRST_CEL_COMMAND = 0x71       # commands from here on are sounds and limb controls


CHOICE_VAR = '"V194"'           # where v5 verb scripts leave the line of a menu


def instrs_of(code: list[dict]) -> list[Instr]:
    return [Instr(c["offset"], c["opcode"], c["name"], c["args"], c.get("target"), c.get("result"))
            for c in code]


def code_size(code: list[dict], data_end: Optional[int] = None) -> int:
    """Where code ends: past its last instruction, for jumps to the end."""
    last = max((c.get("target") or 0 for c in code), default=0)
    return max(last, code[-1]["offset"] + 1 if code else 0, data_end or 0)


def parse(text: str) -> list:
    return StmtParser(tokenize(text)).parse_all()


def identifier(text: str, fallback: str) -> str:
    """"Look at" -> LookAt. Labels with format codes or symbols aren't names."""
    if not text or "%" in text or not re.fullmatch(r"[A-Za-z0-9 ]+", text):
        return fallback
    name = "".join(w[:1].upper() + w[1:] for w in text.split())
    if name[0].isdigit():
        return fallback
    return VERB_SYNONYMS.get(name, name)


STOP_LIMB = 0x79        # the engine doesn't draw a stopped limb; 0x7A brings it back
DEFAULT_WALK_SPEED = (8, 2)     # pixels per game frame
RESTING_FRAMES = (1, 3, 5, 4, 2)    # init, stand, talk stop and start, walk
KEEP_ROOM = 0xFF        # putActor: the actor stays in the room it's in


class Loader:
    def __init__(self, path: str, verbose: bool = False):
        self.path = os.path.abspath(path)
        self.verbose = verbose
        with open(os.path.join(self.path, "game.json"), encoding="utf-8") as f:
            self.data = json.load(f)
        self.version = self.data["version"]
        self.engine = {v: k for k, v in engine_vars(self.version).items()}
        self.rooms = {int(n): r for n, r in self.data["rooms"].items()}
        self.scripts = {int(n): s for n, s in self.data["scripts"].items()}
        self.owners = {kind: {int(n): r for n, r in table.items()}
                       for kind, table in self.data["owners"].items()}
        self.game = Game(name=os.path.basename(self.path.rstrip("/")), version=self.version)
        self.namer = Namer(self.version)
        self.verbs: dict[int, Verb] = {}
        self.actors: dict[int, Actor] = {}
        self.sources: dict[str, list[tuple[str, str]]] = {}   # room -> [(heading, code)]

    def log(self, text: str) -> None:
        if self.verbose:
            print(f"[bundle] {text}", flush=True)

    def describe(self) -> None:
        d = self.data
        self.log(f"{self.path}: SCUMM v{self.version}, {len(self.rooms)} rooms, "
                 f"{len(self.scripts)} scripts, {len(d['costumes'])} costumes, "
                 f"{len(d['charsets'])} charsets, {len(d['sounds'])} sounds")
        for problem in d.get("problems", []):
            self.log(f"problem when bundling: {problem}")

    def keep_source(self, room: str, heading: str, text: str) -> None:
        self.sources.setdefault(room, []).append((heading, text))

    def asset(self, relative: str) -> str:
        return os.path.join(self.path, relative)

    # ------------------------------------------------------------- tables

    def room_name(self, number: int) -> str:
        name = self.rooms[number]["name"]
        clean = re.sub(r"\W", "_", name)
        return clean[0].upper() + clean[1:] if clean and not clean[0].isdigit() else f"Room{number}"

    def inventory_room(self) -> Optional[int]:
        """The storage room whose objects scripts pick up. The backend expects its name."""
        code = [c for c in self.scripts.values()]
        for room in self.rooms.values():
            code += [room["entry"], room["exit"], *room["local_scripts"].values()]
            code += [o["code"] for o in room["objects"]]
        picked = {i["args"][0] for piece in code for i in piece
                  if i["name"] == "pickupObject" and isinstance(i["args"][0], int)}
        counts = {number: sum(o["number"] in picked for o in room["objects"])
                  for number, room in self.rooms.items()
                  if room["width"] <= 8 or room["height"] <= 8}
        best = max(counts, key=counts.get, default=None)
        return best if best is not None and counts[best] else None

    def name_things(self) -> None:
        n = self.namer
        n.rooms = {number: self.room_name(number) for number in self.rooms}
        inventory = self.inventory_room()
        if inventory is not None:
            n.rooms[inventory] = "InventoryItems"
        for number, room in self.rooms.items():
            for o in room["objects"]:
                n.objects[o["number"]] = f"{n.rooms[number]}::obj_{o['number']}"
        for number in self.scripts:
            room = n.rooms.get(self.owners["scripts"].get(number))
            n.scripts[number] = f"{room}::{'main' if number == 1 else f'script_{number}'}"
        for number in self.data["sounds"]:
            room = n.rooms.get(self.owners["sounds"].get(int(number)))
            n.sounds[int(number)] = f"{room}::sound_{number}"
        n.costumes = {int(c): f"costume_{c}" for c in self.data["costumes"]}
        n.charsets = {int(c): f"charset_{c}" for c in self.data["charsets"]}

    def find_actor_params(self) -> None:
        """Which parameters of each script are actors.

        Some scripts use theirs directly; others only pass it on, or use it as an
        index of an array that other scripts index by actor. So this repeats until
        nothing new turns up.
        """
        takes_actor = {"walkActorTo", "walkActorToObject", "putActor", "faceActor", "animateActor",
                       "getActorRoom", "getActorMoving", "putActorAtObject", "getActorWalkBox"}
        starts = ("startScript", "startScriptQuick", "startScriptQuick2")

        def local(value) -> Optional[int]:
            if isinstance(value, dict) and value.get("var", "").startswith("L"):
                return int(value["var"][1:])
            return None

        def indexes(value):
            """(array, local used as its first index) anywhere in an expression."""
            if isinstance(value, dict):
                if value.get("call") in ("arrayRead", "arrayRead2", "arrayWrite2") \
                        and local(value["args"][1]) is not None:
                    yield value["args"][0], local(value["args"][1])
                yield from indexes(value.get("args", []))
            elif isinstance(value, list):
                for item in value:
                    yield from indexes(item)

        found: dict[int, set[int]] = {n: set() for n in self.scripts}
        uses: dict[int, set] = {n: set() for n in self.scripts}
        links: list[tuple[int, int, int, int]] = []     # caller, local, callee, parameter
        for number, code in self.scripts.items():
            for i in code:
                uses[number] |= set(indexes(i["args"]))
                if i["name"] == "arrayWrite2" and local(i["args"][1]) is not None:
                    uses[number].add((i["args"][0], local(i["args"][1])))
                first = None
                if i["name"] in takes_actor and i["args"]:
                    first = i["args"][0]
                elif i["name"] == "actorOps" and i["args"] and i["args"][0].get("call") == "setCurrent":
                    first = i["args"][0]["args"][0]
                if local(first) is not None:
                    found[number].add(local(first))
                if i["name"] in starts:
                    callee, values = (i["args"][1], i["args"][2]) if i["name"] == "startScript" \
                        else (i["args"][0], i["args"][1])
                    if isinstance(callee, int) and callee in found:
                        links += [(number, local(v), callee, k) for k, v in enumerate(values)
                                  if local(v) is not None]
        changed = True
        while changed:
            changed = False
            actor_arrays = {array for number, pairs in uses.items() for array, l in pairs
                            if l in found[number]}
            for number, pairs in uses.items():
                new = {l for array, l in pairs if array in actor_arrays} - found[number]
                if new:
                    found[number] |= new
                    changed = True
            for caller, l, callee, k in links:
                if l in found[caller] and k not in found[callee]:
                    found[callee].add(k)
                    changed = True
                elif k in found[callee] and l not in found[caller]:
                    found[caller].add(l)
                    changed = True
        self.namer.actor_params = {n: f for n, f in found.items() if f}

    def rename_script(self, number: int, name: str) -> None:
        room = self.namer.scripts[number].split("::")[0]
        self.namer.scripts[number] = f"{room}::{name}"

    @staticmethod
    def calls(code: list) -> list[int]:
        return [i["args"][0] for i in code
                if i["name"] in ("startScriptQuick", "startScriptQuick2") and isinstance(i["args"][0], int)]

    def name_ui_scripts(self) -> None:
        """Interface scripts the translator knows by name, recognised by what they do."""
        show_verbs = None
        for number, code in self.scripts.items():
            names = {a["call"] for i in code if i["name"] == "verbOps" for a in i["args"]
                     if isinstance(a, dict) and "call" in a}
            branches = any(i["name"] in ("if", "ifNot") and i["args"][0] == {"var": "L0"} for i in code)
            if {"on", "off"} <= names and branches and "new" not in names:
                show_verbs = number
                self.rename_script(number, "showVerbs")
                break
        self.name_dialog_library(show_verbs)

    @staticmethod
    def arrays_read(code: list) -> set[int]:
        """Arrays an expression reads, anywhere in some code."""
        found: set[int] = set()

        def visit(value):
            if isinstance(value, dict):
                if value.get("call") in ("arrayRead", "arrayRead2"):
                    found.add(value["args"][0])
                visit(value.get("args", []))
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        for i in code:
            visit(i["args"])
        return found

    @staticmethod
    def verb_bases(code: list) -> set[int]:
        """First numbers of verb lists it goes through: setCurrentVerb(base + i)."""
        bases = set()
        for i in code:
            if i["name"] == "verbOps" and i["args"][0].get("call") == "setCurrent":
                target = i["args"][0]["args"][0]
                if isinstance(target, dict) and target.get("call") == "+":
                    bases |= {a for a in target["args"] if isinstance(a, int)}
        return bases

    def name_dialog_library(self, show_verbs: Optional[int]) -> None:
        """The ScummC dialog library, by how its scripts relate to each other.

        The menu shows verbs with a runtime text taken from an array. dialogAdd
        fills that array, dialogClear frees its strings, dialogStart clears the
        selection, dialogHide turns the menu's verbs off and dialogEnd also brings
        the normal verbs back.
        """
        menus = [n for n, c in self.scripts.items()
                 if self.has_sub(c, "verbOps", "nameString") and self.verb_bases(c)]
        for menu in menus:
            arrays = self.arrays_read(self.scripts[menu])
            others = {n: c for n, c in self.scripts.items() if n != menu}
            # dialogAdd stores its parameter at the position of a global counter.
            add = count = None
            for number, code in others.items():
                for i in code:
                    if i["name"] == "arrayWrite" and i["args"][0] in arrays \
                            and i["args"][1].__class__ is dict and i["args"][1].get("var", "")[:1] == "V" \
                            and i["args"][2] == {"var": "L0"}:
                        add, count = number, i["args"][1]
                if add is not None:
                    break
            start = next((n for n, c in others.items() if c and c[0]["name"] == "move"
                          and c[0]["args"][0] == -1), None)
            if add is None or start is None:
                continue
            self.rename_script(menu, "showDialog")
            self.rename_script(add, "dialogAdd")
            self.rename_script(start, "dialogStart")
            self.namer.aliases[f"g{self.scripts[start][0]['result']['var'][1:]}"] = "selectedSentence"
            bases = self.verb_bases(self.scripts[menu])
            hide = next((n for n, c in others.items() if self.verb_bases(c) & bases
                         and self.has_sub(c, "verbOps", "off") and not self.has_sub(c, "verbOps", "on")), None)
            if hide is not None:
                self.rename_script(hide, "dialogHide")
                end = next((n for n, c in others.items() if hide in self.calls(c)
                            and show_verbs in self.calls(c)), None)
                if end is not None:
                    self.rename_script(end, "dialogEnd")
            # dialogClear frees the strings and puts the counter back to 0.
            clear = next((n for n, c in others.items()
                          if any(i["name"] == "undim" and i["args"][0] in arrays for i in c)
                          and any(i["name"] == "move" and i["result"] == count and i["args"][0] == 0
                                  for i in c)), None)
            if clear is not None:
                self.rename_script(clear, "dialogClear")
            return

    def script_code(self, number: int, room: Optional[int]) -> Optional[list]:
        if number >= 200 and room in self.rooms:
            return self.rooms[room]["local_scripts"].get(str(number))
        return self.scripts.get(number)

    # ---------------------------------------------------------------- run

    def on_op(self, op: str, args: list, instr: dict) -> None:
        g = self.game
        if op == "assign":
            target, value = args
            var = self.engine_name(target)
            if var == "VAR_EGO" and isinstance(value, int) and g.ego is None:
                self.ego_number = value
            elif var == "VAR_TIMER_NEXT" and isinstance(value, int):
                g.timer_next = value
            elif var == "VAR_CHARINC" and isinstance(value, int):
                g.char_inc = value
            elif var in ("VAR_CUTSCENE_START_SCRIPT", "VAR_CUTSCENE_END_SCRIPT", "VAR_VERB_SCRIPT") \
                    and isinstance(value, int):
                self.hooks.setdefault(var, value)
        elif op == "loadRoom" and isinstance(args[0], int):
            self.start_room = self.start_room or args[0] or None
        elif op == "arrayWrite":
            array, position, value = args
            self.array_links.setdefault(array, []).append((position, value))
        elif op == "actorOps.setCurrent":
            self.current_actor = args[0]
        elif op.startswith("actorOps."):
            self.actor_op(op[9:], args)
        elif op == "verbOps.setCurrent":
            self.current_verb = args[0]
        elif op.startswith("verbOps."):
            self.verb_op(op[8:], args)
        elif op == "cursorCommand.charsetSet" and isinstance(args[0], int):
            self.current_charset = args[0]
            if self.in_boot:
                self.text_charset = args[0]
        elif op == "cursorCommand.cursorImage" and len(args) == 2:
            self.cursor_image = args
        elif op == "cursorCommand.cursorTransparency":
            self.cursor_transparent = args[0]
        elif op == "roomOps.fade" and self.in_boot and isinstance(args[0], int):
            self.game.boot_effect = args[0] & 0xFFFF    # compiled: a signed word
        elif op == "setClass" and isinstance(args[0], int):
            self.classes_set.setdefault(args[0], []).extend(c for c in args[1] if isinstance(c, int))

    def engine_name(self, var: dict) -> Optional[str]:
        name = var.get("var", "")
        if name[:1] != "V" or "index" in var:
            return None
        return engine_vars(self.version).get(int(name[1:]))

    def actor_op(self, op: str, args: list) -> None:
        number = self.current_actor
        if not isinstance(number, int):
            return
        # What the boot script gives an actor is how it starts; later scripts are
        # the game changing it during play.
        if number in self.boot_actors and not self.in_boot:
            return
        if op == "costume" and isinstance(args[0], int):
            actor = self.actors.setdefault(number, Actor(name=f"actor_{number}"))
            actor.costume = f"costume_{args[0]}"
            if self.in_boot:
                self.boot_actors.add(number)
        actor = self.actors.get(number)
        if actor is None:
            return
        if op == "name":
            actor.display_name = args[0].strip("@") if isinstance(args[0], str) else args[0]
        elif op == "talkColor":
            actor.talk_color = args[0]
        elif op == "animationSpeed":
            actor.anim_speed = args[0]
        elif op == "stepDist":
            actor.walk_speed = (args[0], args[1])
        elif op == "elevation":
            actor.elevation = args[0] or 0
        elif op == "width":
            actor.width = args[0]
        elif op == "textOffset":
            actor.talk_pos = (args[0], args[1])
        elif op == "ignoreBoxes":
            actor.ignore_boxes = True

    def verb_op(self, op: str, args: list) -> None:
        number = self.current_verb
        if not isinstance(number, int):
            return
        verb = self.verbs.setdefault(number, Verb(f"verb_{number}"))
        if op == "name" and isinstance(args[0], str):
            verb.label = scummc_text(args[0], self.namer, {})
            verb.charset = f"charset_{self.current_charset}" if self.current_charset else None
        elif op == "key" and isinstance(args[0], int):
            verb.key = chr(args[0])
        elif op == "at" and all(isinstance(a, int) for a in args):
            verb.pos = (args[0], args[1])
        elif op == "center":
            verb.centered = True
        elif op == "on":
            verb.on = True
        elif op in ("color", "hiColor", "dimColor", "backColor") and isinstance(args[0], int):
            setattr(verb, {"color": "color", "hiColor": "hi_color", "dimColor": "dim_color",
                           "backColor": "back_color"}[op], args[0])
        elif op == "imageRoom" and isinstance(args[0], int):
            verb.image = self.namer.objects.get(args[0])

    @staticmethod
    def has_sub(code: list, family: str, *subs: str) -> bool:
        found = {a["call"] for i in code if i["name"] == family for a in i["args"]
                 if isinstance(a, dict) and "call" in a}
        return all(s in found for s in subs)

    def creates_things(self, code: list) -> bool:
        """Whether a script sets up verbs or actors, rather than using them."""
        verbs = self.has_sub(code, "verbOps", "new", "at") and not self.has_sub(code, "verbOps", "nameString")
        actors = self.has_sub(code, "actorOps", "default", "costume")
        return verbs or actors

    def worth_running(self, code: list) -> bool:
        """Verb lists with a runtime text are menus built on the fly, not setup."""
        if self.has_sub(code, "verbOps", "nameString"):
            return False
        return any(i["name"] == "verbOps" for i in code) or self.has_sub(code, "actorOps", "costume")

    def run_setup(self) -> list[int]:
        self.hooks: dict[str, int] = {}
        self.array_links: dict[int, list] = {}
        self.current_actor = self.current_verb = self.current_charset = None
        self.text_charset = self.start_room = self.ego_number = None
        self.cursor_image = self.cursor_transparent = None
        self.classes_set: dict[int, list[int]] = {}
        self.boot_actors: set[int] = set()
        ev = Evaluator(self.script_code, self.on_op)
        self.in_boot = True
        if 1 in self.scripts:
            ev.visited.add(1)
            ev.run(self.scripts[1], [0], self.owners["scripts"].get(1))
        self.in_boot = False
        self.boot_globals = dict(ev.globals)
        setup = [n for n, code in sorted(self.scripts.items()) if self.creates_things(code)]
        pieces = [(code, self.owners["scripts"].get(n), n) for n, code in sorted(self.scripts.items())]
        for room_number, room in self.rooms.items():
            pieces.append((room["entry"], room_number, None))
            pieces += [(code, room_number, None) for code in room["local_scripts"].values()]
        for code, room, number in pieces:
            if self.worth_running(code):
                ev.stopped, ev.steps = False, 0
                # A script that shows or hides things by a parameter: "show" is
                # the state during play.
                ev.run(code, [1] * 4, room)
        return setup

    # -------------------------------------------------------------- assets

    @staticmethod
    def resting_limbs(c: dict, commands: bytes, cels: dict) -> dict[int, dict]:
        """For each direction, the first picture of each limb in the usual animations."""
        resting: dict[int, dict] = {d: {} for d in range(4)}
        for frame in RESTING_FRAMES:
            for direction in range(4):
                for step in c["anims"].get(str(frame * 4 + direction), []):
                    if step["start"] is None:
                        continue
                    chunk = commands[step["start"]:step["start"] + step["length"] + 1]
                    cel = next((f"{step['limb']}_{cmd}" for cmd in chunk if cmd < FIRST_CEL_COMMAND
                                and f"{step['limb']}_{cmd}" in cels), None)
                    if cel:
                        resting[direction].setdefault(step["limb"], cel)
        return resting

    def costume(self, number: int, c: dict) -> Costume:
        name = f"costume_{number}"
        commands = bytes.fromhex(c["commands"])
        cels = c["cels"]
        sizes = {key: Image.open(self.asset(cel["image"])).size for key, cel in cels.items()}
        left = min((cel["rel_x"] for cel in cels.values()), default=0)
        top = min((cel["rel_y"] for cel in cels.values()), default=0)
        right = max((cel["rel_x"] + sizes[k][0] for k, cel in cels.items()), default=1)
        bottom = max((cel["rel_y"] + sizes[k][1] for k, cel in cels.items()), default=1)
        out_dir = self.asset(f"composed/{number:03d}")
        os.makedirs(out_dir, exist_ok=True)
        cost = Costume(name, flip=c["mirror"], palette=self.asset("palette.png"))
        resting = self.resting_limbs(c, commands, cels)
        for key, steps in sorted(c["anims"].items(), key=lambda kv: int(kv[0])):
            key = int(key)
            frame, direction = key // 4, DIRECTIONS[key % 4]
            tracks = []
            moved = set()
            for step in steps:
                if step["start"] is None:
                    moved.add(step["limb"])     # hidden
                    continue
                chunk = commands[step["start"]:step["start"] + step["length"] + 1]
                seq = [f"{step['limb']}_{cmd}" for cmd in chunk
                       if cmd < FIRST_CEL_COMMAND and f"{step['limb']}_{cmd}" in cels]
                if seq:
                    tracks.append((step["limb"], seq, step["loop"]))
                if seq or STOP_LIMB in chunk:
                    moved.add(step["limb"])
            if not tracks:
                continue
            # An animation only moves some limbs: the rest stay as they were. Only for
            # v5: ScummC costumes, the v6 ones so far, say what each animation shows.
            if self.version < 6:
                tracks += [(limb, [cel], True) for limb, cel in resting[key % 4].items() if limb not in moved]
            frames = []
            for t in range(max(len(seq) for _, seq, _ in tracks)):
                canvas = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
                for _, seq, loop in sorted(tracks):
                    cel_key = seq[t % len(seq)] if loop else seq[min(t, len(seq) - 1)]
                    cel = cels[cel_key]
                    image = Image.open(self.asset(cel["image"])).convert("RGBA")
                    canvas.alpha_composite(image, (cel["rel_x"] - left, cel["rel_y"] - top))
                digest = hashlib.md5(canvas.tobytes()).hexdigest()[:10]
                picture = f"f_{digest}"
                if picture not in cost.pictures:
                    path = os.path.join(out_dir, picture + ".png")
                    canvas.save(path)
                    cost.pictures[picture] = Picture(picture, path, (left, top))
                frames.append(picture)
            anim_name = STANDARD_FRAMES.get(frame, f"anim{frame}")
            anim = cost.anims.setdefault(anim_name, Anim(anim_name, frame))
            anim.directions[direction] = [AnimStep("all", frames, None, all(t[2] for t in tracks))]
        cost.limbs = ["all"]
        return cost

    def write_font(self, number: int, c: dict) -> str:
        """Glyph list for backend.fonts."""
        path = self.asset(f"charsets/{number}.json")
        glyphs = {code: {"image": self.asset(g["image"]) if g["image"] else None,
                         "width": g["width"], "height": g["height"], "x": g["x"], "y": g["y"]}
                  for code, g in c["glyphs"].items()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(glyphs, f)
        return path

    def obj(self, room_name: str, o: dict, table: dict) -> Obj:
        n = self.namer
        states = []
        # v5 gives the walk-to point in room coordinates, v6 from the object.
        walk = (o["walk_x"], o["walk_y"]) if self.version >= 6 else (o["walk_x"] - o["x"], o["walk_y"] - o["y"])
        for k, s in enumerate(o["states"]):
            hs = o["hotspots"][k] if k < len(o["hotspots"]) else walk
            states.append(ObjState(hs[0], hs[1], self.asset(s["image"]),
                                   [self.asset(z) for z in s["zplanes"]]))
        classes = [n.class_name(bit + 1) for bit in range(32) if table.get("classes", 0) >> bit & 1]
        # Direction 0 is also what objects with no direction have.
        direction = OBJECT_DIRECTIONS.get(o["direction"]) if o["direction"] else None
        hs_x, hs_y = (0, 0) if states else walk
        # A name can be padded with @, to leave room for a longer one later.
        obj = Obj(f"obj_{o['number']}", room_name, o["name"].strip("@") or None, o["x"], o["y"], o["width"],
                  o["height"], hs_x, hs_y, direction, classes, states, table.get("state", 0))
        if o["code"] and o["verbs"]:
            code = instrs_of(o["code"])
            entries = {int(v): off for v, off in o["verbs"].items()}
            text = verb_text(code, code_size(o["code"]), entries, n, room_name)
            label = f' // "{o["name"]}"' if o["name"] else ""
            self.keep_source(room_name, f"object obj_{o['number']}{label}", text)
            parser = StmtParser(tokenize(text))
            parser.ts.tokens.append(Token("punct", "}", 0))
            obj.verbs = parser.case_list()
        return obj

    # ------------------------------------------------------------ assembly

    def verb_roles(self) -> dict[int, str]:
        """Internal verbs have no label: what they are shows in how they're called.

        UsedWith goes to the second object: startObject2(objB, V, [V, objB, objA]).
        Preposition asks an object: startObject2(objA, V, [vrb, objA]).
        InventoryObject is only handled by inventory items.
        """
        roles: dict[int, str] = {}
        code = list(self.scripts.values())
        for room in self.rooms.values():
            code += [o["code"] for o in room["objects"]] + list(room["local_scripts"].values())
        for piece in code:
            for i in piece:
                if i["name"] != "startObjectQuick" or not isinstance(i["args"][1], int):
                    continue
                verb, values = i["args"][1], i["args"][2]
                if len(values) == 3 and values[0] == verb:
                    roles[verb] = "UsedWith"
                elif len(values) == 2 and values[0] == verb:
                    roles[verb] = "InventoryObject"
                elif len(values) == 2 and isinstance(values[0], int):
                    roles[verb] = "Icon"        # [script that receives the icon, slot]
                elif len(values) == 2 and isinstance(values[0], dict):
                    roles[verb] = self.asking_role(verb)
        owners: dict[int, set[str]] = {}
        for number, room in self.rooms.items():
            for o in room["objects"]:
                for verb in o["verbs"]:
                    owners.setdefault(int(verb), set()).add(self.namer.rooms[number])
        for verb, rooms in owners.items():
            if rooms == {"InventoryItems"} and verb != 255 and verb not in roles \
                    and not (verb in self.verbs and self.verbs[verb].label):
                # Unknown verbs on inventory items are the engine's way of asking
                # for their icon, unless InventoryObject is still missing.
                roles[verb] = "Icon" if "InventoryObject" in roles.values() else "InventoryObject"
        return roles

    def handlers(self, verb: int):
        """(object, code from the handler on) of every object handling a verb."""
        for room in self.rooms.values():
            for o in room["objects"]:
                entry = o["verbs"].get(str(verb))
                if entry is not None:
                    yield o, [i for i in o["code"] if i["offset"] >= entry]

    def asking_role(self, verb: int) -> str:
        """Scripts ask objects two things: a preposition (handlers write its text),
        or to set up the walk boxes."""
        for _, code in self.handlers(verb):
            for i in code:
                if i["name"] == "stopObjectCode":
                    break
                if i["name"] == "arrayOps":
                    return "Preposition"
        return "SetBoxes"

    def name_preposition_var(self, verb: int) -> None:
        """The handler of Preposition writes the word in a global the sentence shows."""
        for _, code in self.handlers(verb):
            for i in code:
                if i["name"] == "arrayOps" and i["args"][1]["call"] == "assignString":
                    self.namer.aliases[f"g{i['args'][0]}"] = "sntcPrepo"
                    return

    def name_verbs(self) -> None:
        for room in self.rooms.values():
            for o in room["objects"]:
                for verb in o["verbs"]:
                    if int(verb) != 255:
                        self.verbs.setdefault(int(verb), Verb(f"verb_{verb}"))
        roles = self.verb_roles()
        used: set[str] = set()
        for number, role in roles.items():
            self.verbs.setdefault(number, Verb(role))
            if role == "Preposition":
                self.name_preposition_var(number)
        for number, verb in sorted(self.verbs.items()):
            name = roles.get(number) or identifier(verb.label or "", f"verb_{number}")
            if name in used:
                name = f"{name}_{number}"
            used.add(name)
            verb.name = name
            self.namer.verbs[number] = name

    def click_script(self, number: int, data: dict) -> list:
        """The script a room's entry installs as VAR_VERB_SCRIPT: click anywhere."""
        for i in data["entry"]:
            if i["name"] == "move" and self.engine_name(i["result"]) == "VAR_VERB_SCRIPT" \
                    and isinstance(i["args"][0], int):
                code = self.script_code(i["args"][0], number)
                if code:
                    room = self.namer.rooms[number]
                    return parse(script_text(instrs_of(code), code_size(code), self.namer, room)[0])
        return []

    def link_actor_objects(self) -> None:
        """Objects standing for actors: an array written with (actor, object) pairs."""
        g, n = self.game, self.namer

        def pairs(links):
            return [(a, v) for a, v in links if a in n.actors and v in n.objects]

        array = max(self.array_links, default=None, key=lambda a: len(pairs(self.array_links[a])))
        if array is None or not pairs(self.array_links[array]):
            return
        n.aliases[f"g{array}"] = "actorObject"
        for actor, value in pairs(self.array_links[array]):
            g.actor_objects[f"actor_{actor}"] = n.objects[value]

    def find_dialog_layout(self) -> None:
        """Where the dialog menu draws its choices, from showDialog's verbs.

        Lines go at (x, y + step * i), at most `max` of them; arrows are verbs with a
        fixed label and position.
        """
        number = next((n for n, name in self.namer.scripts.items() if name.endswith("::showDialog")), None)
        if number is None:
            return
        ui: dict = {"arrows": []}
        label = None
        counter = None
        for i in self.scripts[number]:
            if i["name"] == "verbOps":
                sub = i["args"][0]
                if sub["call"] == "name" and isinstance(sub["args"][0], dict):
                    label = sub["args"][0].get("text")
                elif sub["call"] == "at":
                    x, y = sub["args"]
                    if isinstance(x, int) and isinstance(y, int) and label is not None:
                        ui["arrows"].append({"x": x, "y": y, "label": label})
                        label = None
                    elif isinstance(x, int) and isinstance(y, dict) and y.get("call") == "+":
                        base, product = y["args"]
                        if isinstance(base, int) and isinstance(product, dict) and product.get("call") == "*":
                            step, counter = product["args"]
                            if isinstance(step, int):
                                ui.update(x=x, y=base, step=step)
            elif i["name"] == "ifNot" and counter is not None:
                cond = i["args"][0]
                if isinstance(cond, dict) and cond.get("call") == "<" and cond["args"][0] == counter \
                        and isinstance(cond["args"][1], int):
                    ui["max"] = cond["args"][1]
        if "x" in ui:
            self.game.dialog_ui = ui

    def find_verb_roles(self) -> None:
        """Verbs that take a second object, and the one used on inventory items."""
        g, n = self.game, self.namer
        labelled = {num for num, v in self.verbs.items() if v.label}
        for code in self.scripts.values():
            text = json.dumps(code)
            for match in re.finditer(r'"call": "isAnyOf", "args": \[[^\[]*\[([\d, ]+)\]', text):
                values = [int(v) for v in match[1].split(",")]
                # The full list of verbs also appears: the shortest one is the choice.
                if values and set(values) <= labelled and \
                        (not g.target_verbs or len(values) < len(g.target_verbs)):
                    g.target_verbs = [n.verbs[v] for v in values]
            if not g.inventory_verb and '"findInventory"' in text:
                for i in code:
                    if i["name"] == "move" and isinstance(i["args"][0], int) and i["args"][0] in labelled:
                        g.inventory_verb = n.verbs[i["args"][0]]
                        break

    def name_classes(self, default_action: Optional[int]) -> None:
        """The classes the translator knows, by how the game uses them.

        Person is what actors have in common. Pickable is what the default action
        for "pick up" checks before picking the object up.
        """
        counts: dict[int, int] = {}
        for number, values in self.classes_set.items():
            if number in self.namer.actors:
                for value in values:
                    if value & 0x80:
                        counts[value & 0x7F] = counts.get(value & 0x7F, 0) + 1
        for value, count in counts.items():
            if count >= 2 and value not in self.namer.classes:
                self.namer.classes[value] = "Person"
        def class_tests(e):
            if isinstance(e, dict) and e.get("call") == "ifClassOfIs" and len(e["args"][1]) == 1:
                yield fold(e["args"][1][0])
            elif isinstance(e, dict):
                for arg in e.get("args", []):
                    yield from class_tests(arg)

        code = self.scripts.get(default_action, [])
        for n, i in enumerate(code):
            if i["name"] == "ifNot" and any(j["name"] == "pickupObject" for j in code[n + 1:n + 8]):
                for value in class_tests(i["args"][0]):
                    if isinstance(value, int):
                        self.namer.classes.setdefault(value & 0x7F, "Pickable")

    def find_default_action(self) -> Optional[int]:
        """The script most objects fall back on: called with their (vrb, objA, objB)."""
        counts: dict[int, int] = {}
        triple = [{"var": "L0"}, {"var": "L1"}, {"var": "L2"}]
        for room in self.rooms.values():
            for o in room["objects"]:
                for i in o["code"]:
                    if i["name"] in ("startScriptQuick", "startScriptQuick2") and i["args"][1] == triple:
                        counts[i["args"][0]] = counts.get(i["args"][0], 0) + 1
        if counts:
            return max(counts, key=counts.get)
        # No object calls it directly: it's the script switching on its first
        # parameter over the verbs of the interface.
        labelled = {n for n, v in self.verbs.items() if v.label}
        cases = {}
        for number, code in self.scripts.items():
            tests = [i for i in code if i["name"] in ("if", "ifNot") and isinstance(i["args"][0], dict)
                     and i["args"][0].get("call") in ("==", "!=") and i["args"][0]["args"][0] == {"var": "L0"}
                     and isinstance(i["args"][0]["args"][1], int)
                     and i["args"][0]["args"][1] in labelled]
            if len(tests) >= 3:
                cases[number] = len(tests)
        return max(cases, key=cases.get) if cases else None

    def script_defs(self, default_action: Optional[int]) -> None:
        g, n = self.game, self.namer
        locals_ = {0: "vrb", 1: "objA", 2: "objB"}
        for number, code in self.scripts.items():
            room, name = n.scripts[number].split("::")
            names = locals_ if number == default_action else None
            text, params = script_text(instrs_of(code), code_size(code), n, room, names)
            body = parse(text)
            self.keep_source(room, f"script {name}({', '.join(params)})", text)
            g.scripts[f"{room}::{name}"] = ScriptDef(room, name, params, body)
            if g.version < 6 and self.is_conversation(code):
                g.scripts[f"{room}::{name}"].flat = self.flat_script(code, room)
            if number == default_action:
                g.default_action = next((s.cases for s in body if s.kind == "switch"), [])
        for number, data in self.rooms.items():
            room = n.rooms[number]
            for script, code in data["local_scripts"].items():
                text, params = script_text(instrs_of(code), code_size(code), n, room)
                self.keep_source(room, f"script local_{script}({', '.join(params)})", text)
                g.scripts[f"{room}::local_{script}"] = ScriptDef(room, f"local_{script}", params, parse(text))
                if g.version < 6:
                    blocks = self.flat_script(code, room)
                    g.scripts[f"{room}::local_{script}"].routine = blocks
                    if self.is_conversation(code):
                        g.scripts[f"{room}::local_{script}"].flat = blocks

    @staticmethod
    def is_conversation(code: list[dict]) -> bool:
        """A script that fills the menu of lines and reads what the player chose."""
        menu = any(i["name"] == "verbOps" and any(isinstance(a, dict) and a.get("call") == "name" for a in i["args"])
                   for i in code)
        return menu and CHOICE_VAR in json.dumps(code)

    def flat_script(self, code: list[dict], room: str) -> list[FlatBlock]:
        blocks = []
        for b in flat_blocks(instrs_of(code), code_size(code), self.namer, room):
            cond = parse(f"{b['cond']};")[0].expr if "cond" in b else []
            blocks.append(FlatBlock(b["offset"], parse("\n".join(b["lines"])), cond, b.get("target"),
                                    b.get("negate", False), b["next"]))
        return blocks

    def room_costumes(self) -> None:
        """Costumes a room's own code gives its actors, when they aren't the usual one."""
        g, n = self.game, self.namer
        for number, data in self.rooms.items():
            code = [data["entry"], *(c for _, c in sorted(data["local_scripts"].items())),
                    *(o["code"] for o in data["objects"])]
            found: dict[str, str] = {}
            actor = None        # v6 picks the actor in an instruction of its own
            for i in (i for piece in code for i in piece if i["name"] == "actorOps"):
                for sub in i["args"]:
                    if sub["call"] == "setCurrent":
                        actor = sub["args"][0]
                    elif sub["call"] == "costume" and isinstance(actor, int) and isinstance(sub["args"][0], int):
                        # A v5 script gives the later part of the game's costume first.
                        if g.version < 6:
                            found[f"actor_{actor}"] = f"costume_{sub['args'][0]}"
                        else:
                            found.setdefault(f"actor_{actor}", f"costume_{sub['args'][0]}")
            g.rooms[n.rooms[number]].actor_costumes = {
                a: c for a, c in found.items()
                if a in g.actors and a != g.ego and c in g.costumes and c != g.actors[a].costume}

    def room_start_states(self) -> None:
        """States a room's entry gives its objects, often in loops over numbers the
        translator can't follow: e.g. background characters drawn as objects that the entry turns on."""
        g, n = self.game, self.namer
        for number, data in self.rooms.items():
            states: dict[int, int] = {}

            def seen(op: str, args: list, instr: dict) -> None:
                if op in ("setState", "drawObject") and all(isinstance(a, int) for a in args[:2]):
                    states[args[0]] = args[1]

            ev = Evaluator(self.script_code, seen)
            ev.globals.update(self.boot_globals)
            ev.run(data["entry"], [0] * 4, number)
            for obj_number, state in states.items():
                obj = g.objects.get(n.objects.get(obj_number, ""))
                if obj and obj.room == n.rooms[number]:
                    obj.state = state

    def placements(self, setup: list[int]) -> None:
        g, n = self.game, self.namer

        def who(arg) -> Optional[str]:
            if isinstance(arg, dict) and self.engine_name(arg) == "VAR_EGO":
                return g.ego
            return f"actor_{arg}" if isinstance(arg, int) and f"actor_{arg}" in g.actors else None

        def puts(code):
            # v5 says where an actor goes with its own instruction, before or after.
            # Room 0 is taking it out, later in the same script.
            rooms = {}
            for i in code:
                if i["name"] == "putActorInRoom" and i["args"][1] != 0:
                    rooms.setdefault(str(i["args"][0]), i["args"][1])
            for i in code:
                if i["name"] == "putActor" and isinstance(i["args"][1], int) \
                        and isinstance(i["args"][2], int) and isinstance(i["args"][3], int):
                    room = rooms.get(str(i["args"][0])) if i["args"][3] == KEEP_ROOM else i["args"][3]
                    yield who(i["args"][0]), i["args"][1], i["args"][2], \
                        n.rooms.get(room) if isinstance(room, int) else None

        for number in setup:
            for actor, x, y, room in puts(self.scripts[number]):
                if actor and room in g.rooms:
                    g.actors[actor].start_room = room
                    g.rooms[room].actor_positions[actor] = (x, y)
        for number, data in self.rooms.items():
            for actor, x, y, room in puts(data["entry"]):
                if actor and room in g.rooms:
                    g.rooms[room].actor_positions[actor] = (x, y)
        local = [c for data in self.rooms.values() for c in data["local_scripts"].values()]
        for code in [*self.scripts.values(), *local]:
            for actor, x, y, room in puts(code):
                r = g.rooms.get(room)
                if r and actor and actor != g.ego and actor not in r.actor_positions:
                    r.scripted_actors.setdefault(actor, (x, y))
        for number in setup:
            for i in self.scripts[number]:
                # What comes after going to a room is the game, not its setup.
                if i["name"] == "loadRoom" and i["args"][0] != 0:
                    break
                if i["name"] == "pickupObject" and isinstance(i["args"][0], int):
                    key = n.objects.get(i["args"][0])
                    if key and key not in g.start_inventory:
                        g.start_inventory.append(key)

    def load(self) -> Game:
        g, n = self.game, self.namer
        self.describe()
        self.log("naming things")
        self.name_things()
        self.log("finding actor parameters")
        self.find_actor_params()
        self.log("naming interface scripts")
        self.name_ui_scripts()
        self.log("running setup scripts")
        setup = self.run_setup()
        g.actors = {a.name: a for a in self.actors.values() if a.costume}
        n.actors = {num for num, a in self.actors.items() if a.costume}
        default_action = self.find_default_action()
        self.name_classes(default_action)
        self.name_verbs()
        g.verbs = [v for _, v in sorted(self.verbs.items())]

        for number, c in self.data["costumes"].items():
            g.costumes[f"costume_{number}"] = self.costume(int(number), c)
        g.actors = {k: a for k, a in g.actors.items() if a.costume in g.costumes}
        for cost in g.costumes.values():
            for anim in cost.anims.values():
                n.anims.setdefault(anim.index, f"{cost.name}_anim_{anim.name}")
        for number, values in self.classes_set.items():
            actor = g.actors.get(f"actor_{number}")
            for value in values if actor else []:
                if value & 0x7F == 32 and value & 0x80:
                    actor.untouchable = True
                actor.classes.append(n.class_name(value & 0x7F))
        for number, c in self.data["charsets"].items():
            g.charsets[f"charset_{number}"] = self.write_font(int(number), c)
        if "1" in self.data["charsets"]:
            g.gui_charset = "charset_1"
        if self.text_charset:
            g.text_charset = f"charset_{self.text_charset}"
        g.dialog_charset = g.text_charset
        g.palette = self.asset("palette.png")

        if default_action in n.scripts:
            self.rename_script(default_action, "defaultAction")
        self.link_actor_objects()

        table = self.data["object_table"]
        for number, data in self.rooms.items():
            name = n.rooms[number]
            storage = data["width"] <= 8 or data["height"] <= 8
            room = Room(name, None if storage else self.asset(data["image"]),
                        [] if storage else [self.asset(z) for z in data["zplanes"]])
            # Box 0 is a dummy; a room with no walking area still has one tiny box.
            boxes = [Box(k, [tuple(c) for c in b["corners"]], b["mask"], b["flags"], b["scale"])
                     for k, b in enumerate(data["boxes"]) if k]
            if not storage and sum((b.max_x - b.min_x) * (b.max_y - b.min_y) for b in boxes) > MIN_WALK_AREA:
                room.boxes = boxes
            g.rooms[name] = room
            for o in data["objects"]:
                entry = table[o["number"]] if o["number"] < len(table) else {}
                obj = self.obj(name, o, entry)
                # A parent is a 1-based position in the room's list of objects.
                if 0 < o["parent"] <= len(data["objects"]):
                    obj.parent = f"obj_{data['objects'][o['parent'] - 1]['number']}"
                    obj.parent_state = o["parent_state"]
                g.objects[obj.key] = obj
            text, _ = script_text(instrs_of(data["entry"]), code_size(data["entry"]), n, name)
            room.entry = parse(text)
            self.keep_source(name, "script entry()", text)
            text, _ = script_text(instrs_of(data["exit"]), code_size(data["exit"]), n, name)
            self.keep_source(name, "script exit()", text)
            room.click_script = self.click_script(number, data)

        self.script_defs(default_action)
        g.setup_scripts = [n.scripts[s].split("::")[1] for s in setup]
        self.find_verb_roles()
        self.find_dialog_layout()
        for code in self.scripts.values():
            if self.has_sub(code, "verbOps", "nameString"):
                charset = next((a["args"][0] for i in code if i["name"] == "cursorCommand"
                                for a in i["args"] if a["call"] == "charsetSet"), None)
                if isinstance(charset, int):
                    g.dialog_charset = f"charset_{charset}"
        g.globals = sorted(n.globals_used)

        if self.ego_number is not None and f"actor_{self.ego_number}" in g.actors:
            g.ego = f"actor_{self.ego_number}"
        elif g.actors:
            g.ego = next(iter(g.actors))
        self.placements(setup)
        self.room_costumes()
        if self.version < 6:
            self.room_start_states()
        g.room_numbers = dict(n.rooms)
        if self.version < 6:
            for actor in g.actors.values():     # the engine's defaults; v6 games set them
                actor.anim_speed = actor.anim_speed or 1
                actor.walk_speed = actor.walk_speed or DEFAULT_WALK_SPEED

        def hook(var):
            number = self.hooks.get(var)
            return n.scripts.get(number) if number is not None else None

        g.cutscene_start = hook("VAR_CUTSCENE_START_SCRIPT")
        g.cutscene_end = hook("VAR_CUTSCENE_END_SCRIPT")
        g.verb_script = hook("VAR_VERB_SCRIPT")
        g.start_room = n.rooms.get(self.start_room)
        # v6 games put the ego in with putActorAt; v5 ones leave it to loadRoomWithEgo.
        if self.version < 6 and g.ego and not g.actors[g.ego].start_room:
            g.actors[g.ego].start_room = g.start_room
        if self.cursor_image:
            obj = g.objects.get(n.objects.get(self.cursor_image[0], ""))
            if obj and obj.states:
                g.cursor = {"image": obj.states[0].image,
                            "hotspot": (obj.states[0].hs_x, obj.states[0].hs_y)}
                if self.cursor_transparent is not None:
                    g.cursor["transparent"] = self.cursor_transparent

        for number, s in self.data["sounds"].items():
            room = n.sounds[int(number)].split("::")[0]
            if s["kind"] == "pcm":
                g.sounds[n.sounds[int(number)]] = Sound(room, f"sound_{number}", self.asset(s["file"]))

        from ..scummc_roam import find_roaming
        g.roaming = find_roaming(g)
        return g


def load_bundle(path: str, verbose: bool = False) -> Game:
    return Loader(path, verbose).load()
