"""Translate IR scripts (ScummC statement trees) into ASHES, Escoria's script language.

Only a subset is translated. Anything else becomes a `# TODO scumm:` comment
carrying the original code, so it can be finished by hand.
"""
from __future__ import annotations

import dataclasses

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..ir import Case, Game, Obj, Stmt, Tok

def statements(stmts: list[Stmt]):
    """Some statements and all those inside them."""
    for st in stmts:
        yield st
        yield from statements(st.body)
        yield from statements(st.orelse)
        for case in st.cases:
            yield from statements(case.body)


SOUND_WAIT = 4.0        # seconds
NO_POSITION = 65535     # startRoomWithEgo: the ego stays at the object
KEEP_ROOM = "255"       # putActorAt: the actor stays in the room it's in

# SCUMM verb -> Escoria action. Other verbs use their snake_case name.
VERB_ACTIONS = {
    "LookAt": "look", "PickUp": "pickup", "TalkTo": "talk", "WalkTo": "walk",
    "Use": "use", "Open": "open", "Close": "close", "Give": "give",
    "Push": "push", "Pull": "pull",
}

# Calls with no visible effect once translated.
IGNORED_CALLS = {"waitForMessage", "waitForActor", "breakScript", "breakHere",
                 "dbgPrint", "waitForCamera",
                 # Actor setup: the frontend already read it into the actor's scene.
                 "initActor", "setActorCostume", "setActorTalkPos", "setActorName",
                 "setActorWalkSpeed", "setActorTalkColor", "setActorWidth",
                 "setActorIgnoreBoxes", "setActorAnimSpeed"}


# SCUMM costume animations with a role in Escoria's animation resource.
ANIM_KINDS = {"walk": "walk", "stand": "idle", "talkStart": "speak"}


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def action_name(verb: str) -> str:
    return VERB_ACTIONS.get(verb, snake(verb))


@dataclass
class Context:
    """What a script is attached to; lets objA / class checks resolve statically."""
    room: Optional[str] = None
    obj: Optional[Obj] = None
    obj_id: Optional[str] = None
    classes: set[str] = field(default_factory=set)
    verb: Optional[str] = None
    var_return: Optional[str] = None     # object key returned by startObject2(...)
    locals: set[str] = field(default_factory=set)        # script-local variables
    current_actor: Optional[str] = None                  # setCurrentActor(...)
    poses: dict[str, list[str]] = field(default_factory=dict)  # actor -> [stand anim, talk anim]
    inline_depth: int = 0
    # Second object of a combination: an object key / actor name, "*" for
    # "any other one" (the fallback), None when the verb has no second object.
    objB: Optional[str] = None
    in_entry: bool = False      # translating a room's entry script
    in_override: bool = False   # an override block, run after the cutscene played out
    # The game's default actions (any object): objA / objB are only known when
    # the action runs; res://ui/scumm_defaults.gd publishes them as globals.
    generic: bool = False
    # A script running on its own, like a room's idle animations: it can loop.
    background: bool = False
    # A conversation in a room of its own: see AshesTranslator.conversation_event.
    flat: bool = False
    closeup: bool = False       # ...that goes to a room of its own, without the ego
    routine: bool = False       # A script that keeps going alongside the game


WAITS = ("breakScript", "breakHere", "delay")
STOP_MOVING, SET_DIRECTION, TURN_TO_DIRECTION = 2, 3, 4     # animateActor commands in v5
V5_DIRECTIONS = {0: 3, 1: 1, 2: 2, 3: 0}    # west, east, south, north -> Escoria's order
# The verb bar and what scripts do to fill a menu: a conversation's commands replace them.
MENU_CALLS = {"setVerbXY", "setVerbOn", "setVerbOff", "setVerbKey", "setVerbColor", "setVerbHiColor", "initVerb",
              "initCharset", "saveVerbs", "restoreVerbs", "cursorOn", "cursorOff", "userPutOn", "userPutOff",
              "dummy", "stopScript", "setVarRange", "waitForMessage"}
# All a background script may do: it must not get in the way of the player.
ROUTINE_LINE = re.compile(r"\s*(#(?! TODO.* = )|wait\(|scumm_(draw|rand|get_state|anim|face|walk|walk_object|stop_bg|"
                          r"running)\(|queue_event\(|set_active\(|teleport_pos\(|\w+ = |var \w+ = |"
                          r"if |elif |else:|while |break$|pass$|$)")
BACKGROUND_LINE = re.compile(r"\s*(#(?! TODO)|wait\(|scumm_(draw|rand|get_state|anim)\(|var \w+ = 0$|\w+ = |"
                             r"if |elif |else:|while |break$|pass$|$)")


class Untranslatable(Exception):
    pass


class AshesTranslator:
    def __init__(self, game: Game, ids: dict[str, str], scenes: dict[str, str], scale: int,
                 anim_name: Optional[Callable[[str, str], Optional[str]]] = None,
                 pose_resource: Optional[Callable[[str, str, str], Optional[str]]] = None):
        """ids: object key / actor name -> Escoria global id; scenes: room -> scene path.

        anim_name(actor, scumm anim) -> Escoria animation name;
        pose_resource(actor, stand anim, talk anim) -> res:// animation resource.
        """
        self.game = game
        self.ids = ids
        self.scenes = scenes
        self.scale = scale
        self.anim_name = anim_name
        self.pose_resource = pose_resource
        self.room: Optional[str] = None     # of the code being translated
        self.last_sound: Optional[str] = None
        self.room_vars: dict[str, str] = {}     # variable -> the actor room it was read from
        self.event_room: Optional[str] = None   # an inlined script keeps its own room in ctx
        self.numbered_objects = {k.split("::obj_")[1]: k for k in game.objects if "::obj_" in k}
        self.ready_room: Optional[str] = None   # the room whose :ready is being translated
        # Who talks -> position and colour the next lines take, set by printDefaults.
        self.print_defaults: dict[str, dict[str, str]] = {}
        self.conversations_ok: dict[str, bool] = {}
        self.routines: dict[str, Optional[str]] = {}     # script -> event, None if it can't be one
        self.choice_verb: Optional[str] = None  # setCurrentVerb of a conversation's menu
        self.leaves_to: Optional[str] = None    # room a conversation goes back to
        self.ego_elsewhere = False              # the ego was already put in that room
        # Room -> event name -> lines; None for a script that can't run in the background.
        self.background_events: dict[str, dict[str, Optional[list[str]]]] = {}
        self.used_globals: set[str] = set()
        # Objects that go to the inventory themselves: object key -> inventory item id.
        self.inventory_ids: dict[str, str] = {}
        self.todos = 0
        self._following: dict[int, list[Stmt]] = {}   # statement -> the next few in its block
        # Rooms holding the game's dialog library (found when a dialog is
        # recognized); Escoria's own dialogs replace those scripts.
        self.dialog_rooms: set[str] = set()
        self.dialog_colors: Optional[tuple[int, int]] = None   # dialogStart(color, hiColor)
        self.ui_scripts = self.find_ui_scripts()
        self.known_classes = {c for o in game.objects.values() for c in o.classes} | \
            {c for a in game.actors.values() for c in a.classes}

    # ------------------------------------------------------------------ API

    def verb_cases(self, cases: list[Case], verb: str) -> Optional[list[Stmt]]:
        """Statements run for `verb`, following case fall-through. None if unhandled."""
        prologue = [st for c in cases for st in c.body
                    if c.labels and c.labels[0] and c.labels[0][0][1] == "__prologue__" and st.kind == "decl"]
        for i, case in enumerate(cases):
            if any(lbl and lbl[0][1] == verb for lbl in case.labels):
                stmts: list[Stmt] = list(prologue)
                for c in cases[i:]:
                    stmts += c.body
                    if c.body and c.body[-1].kind in ("return", "break"):
                        break
                return stmts
        return None

    @staticmethod
    def object_default(cases: list[Case]) -> Optional[list[Stmt]]:
        """The `default:` entry of an object's verbs."""
        for case in cases:
            if not case.labels:
                return list(case.body)
        return None

    def default_cases(self, verb: str) -> list[Stmt]:
        stmts = self.verb_cases(self.game.default_action, verb)
        if stmts is None:
            for case in self.game.default_action:
                if not case.labels:
                    return case.body
            return []
        return stmts

    @staticmethod
    def hoist_repeated_vars(body: list[str]) -> list[str]:
        """Inlined scripts can declare the same local: one declaration at the top, and
        each of them only sets it to 0. ASHES refuses a repeated `var` in a scope."""
        found = [m.group(1) for line in body if (m := re.fullmatch(r"\t+var (\w+) = 0", line))]
        repeated = sorted({name for name in found if found.count(name) > 1})
        if not repeated:
            return body
        out = [re.sub(r"^(\t+)var (\w+) = 0$", r"\1\2 = 0", line)
               if re.fullmatch(r"\t+var (\w+) = 0", line) and line.split()[1] in repeated else line
               for line in body]
        return [f"\tvar {name} = 0" for name in repeated] + out

    def event(self, action: str, stmts: list[Stmt], ctx: Context, flags: str = "") -> list[str]:
        self.last_sound = None
        self.room_vars.clear()
        self.print_defaults = {}
        self.event_room = ctx.room
        body = self.block(stmts, 1, ctx)
        if self.game.version < 6:
            body = self.hoist_repeated_vars(body)
        body = [line for i, line in enumerate(body)
                if not (line.strip() == "stop" and i and body[i - 1] == line)]
        body = self._drop_unreachable(body)
        body = self._collapse_set_animations(body)
        # A trailing `stop` is redundant at the end of an event.
        while body and body[-1].strip() == "stop":
            body.pop()
        if not any(not line.strip().startswith("#") for line in body):
            body.append("\tpass")
        head = f":{action}" + (f" | {flags}" if flags else "")
        return [head] + body + [""]

    # ----------------------------------------------------------- statements

    @staticmethod
    def _collapse_set_animations(lines: list[str]) -> list[str]:
        """Consecutive set_animations for the same object: only the last one matters."""
        out: list[str] = []
        for line in lines:
            if out and line.strip().startswith("set_animations(") and out[-1].strip().startswith("set_animations(") \
                    and line.split(",")[0] == out[-1].split(",")[0]:
                out[-1] = line
            else:
                out.append(line)
        return out

    @staticmethod
    def _drop_unreachable(lines: list[str]) -> list[str]:
        """Remove lines after a `stop` in the same block."""
        out: list[str] = []
        dead_at = None
        for line in lines:
            depth = len(line) - len(line.lstrip("\t"))
            if dead_at is not None:
                if depth >= dead_at and line.strip():
                    continue
                dead_at = None
            out.append(line)
            if line.strip() == "stop":
                dead_at = depth
        return out

    def waits_for(self, st: Stmt, actor_toks: list[Tok]) -> bool:
        """Is a walk followed by waitForActor(same actor), with only no-op calls between?"""
        target = [v for _, v in actor_toks]
        following = self._following.get(id(st), [])
        for nxt in following:
            vals = [v for _, v in nxt.expr]
            if nxt.kind != "expr" or not vals:
                return False
            if vals[0] == "waitForActor":
                return vals[2:-1] == target
            if vals[0] not in ("waitForMessage", "breakScript", "breakHere", "dbgPrint"):
                return False
        return False

    def block(self, stmts: list[Stmt], depth: int, ctx: Context, in_loop: bool = False) -> list[str]:
        out: list[str] = []
        for j, st in enumerate(stmts):
            self._following[id(st)] = stmts[j + 1:j + 4]
        i = 0
        while i < len(stmts):
            st = stmts[i]
            nxt = stmts[i + 1] if i + 1 < len(stmts) else None
            dialog = self.linear_dialog(stmts, i, depth, ctx, in_loop)
            if dialog is not None:
                out += dialog[0]
                i = dialog[1]
                continue
            lines = self.statement(st, depth, ctx, in_loop, nxt)
            i += 1
            if self.game.version < 6 and self.endless(st, lines):
                # What follows waits for a loop that isn't running here, and so did a
                # delay just before it: alone it would only hold the player back.
                while out and out[-1].strip().startswith("wait("):
                    out.pop()
                out += lines
                break
            out += lines
        return out

    def endless(self, st: Stmt, lines: list[str]) -> bool:
        """A `while(1)` left as a TODO, that was waiting for something to happen."""
        return st.kind == "loop" and st.name == "while" and [v for _, v in st.expr] == ["1"] \
            and "TODO" in "".join(lines[:1]) and self.waits_in_loop([st])

    def todo(self, depth: int, text: str) -> list[str]:
        self.todos += 1
        return ["\t" * depth + f"# TODO scumm: {text}"]

    def statement(self, st: Stmt, depth: int, ctx: Context, in_loop: bool, nxt: Optional[Stmt]) -> list[str]:
        self.room = ctx.room
        ind = "\t" * depth
        k = st.kind
        if k == "block":
            return self.block(st.body, depth, ctx, in_loop)
        if k == "loop" and ctx.background:
            return self.background_loop(st, depth, ctx)
        if k == "cutscene":
            # The player can't act during a cutscene, but may skip lines. The
            # game's cutscene start/end scripts decide about the verb bar.
            n = self.constant(self.expression(st.expr, ctx)) if st.expr else 0
            start = [ind + 'accept_input("SKIP")']
            end = [ind + 'accept_input("ALL")']
            for show in dict.fromkeys(self.cutscene_verbs(self.game.cutscene_start, n or 0)):
                start.append(ind + f"set_gui_visible({'true' if show else 'false'})")
            for show in dict.fromkeys(self.cutscene_verbs(self.game.cutscene_end, n or 0)):
                end.append(ind + f"set_gui_visible({'true' if show else 'false'})")
            return start + self.block(st.body, depth, ctx, in_loop) + end
        if k == "label" or (k == "loop" and st.name == "while"):
            dialog = self.dialog(st.body[0] if k == "label" else st, depth, ctx)
            if dialog is not None:
                return dialog
        if k == "expr" and st.expr[:1] == [("ident", "startSound")] and len(st.expr) > 1:
            self.last_sound = st.expr[-2][1]
        if k == "loop" and self.waits_for_sound(st):
            return [ind + f"wait({SOUND_WAIT})  # SCUMM: until the sound ends"]
        if k == "loop" and self.is_wait_for_script(st):
            return []           # the called script was inlined: it already ran
        if k == "label":
            return self.block(st.body, depth, ctx, in_loop)
        if k == "try":
            out = self.block(st.body, depth, ctx, in_loop)
            if st.orelse:
                # SCUMM runs the override block when the cutscene ends, with
                # VAR_OVERRIDE telling whether it was skipped. Escoria skips
                # line by line, so translate the "played out" branch.
                out += [ind + "# override block (not skipped: VAR_OVERRIDE = 0)"]
                out += self.block(st.orelse, depth, dataclasses.replace(ctx, in_override=True), in_loop)
            return out
        if k == "decl":
            if "*" in st.source:
                return []       # string/array buffers (e.g. the dialog line): not needed in ASHES
            out = []
            for _, name in st.expr:
                if name not in ctx.locals:
                    ctx.locals.add(name)
                    out.append(ind + f"var {snake(name)} = 0")
            return out
        if k == "return":
            return [ind + "stop"]
        if k == "break":
            return [ind + ("break" if in_loop else "stop")]
        if k == "if":
            return self.if_chain(st, depth, ctx, in_loop)
        if k == "expr":
            before = set(self.used_globals)
            try:
                return [ind + line for line in self.expr_statement(st, ctx, nxt)]
            except Untranslatable:
                self.used_globals = before
                return self.todo(depth, st.source)
        if k == "switch" and [v for _, v in st.expr] == ["objB"] and ctx.objB:
            # The second object is known when converting: keep only its case.
            chosen = None
            for case in st.cases:
                if not case.labels:
                    if chosen is None and ctx.objB == "*":
                        chosen = case
                    continue
                if ctx.objB != "*" and any(self.ref_key(lbl, ctx) == ctx.objB for lbl in case.labels):
                    chosen = case
                    break
            if chosen is None:
                chosen = next((c for c in st.cases if not c.labels), None)
            body = list(chosen.body) if chosen else []
            while body and body[-1].kind == "break":
                body.pop()
            return self.block(body, depth, ctx, in_loop)
        if k == "switch" and [v for _, v in st.expr[:2]] == ["getRandomNumber", "("] \
                and len(st.expr) == 4 and st.expr[2][0] == "number":
            return self.random_switch(st, depth, ctx, in_loop)
        if k == "loop" and st.name == "for":
            unrolled = self.unroll(st)
            if unrolled is not None:
                return self.block(unrolled, depth, ctx, in_loop)
        # switch, other loops...
        return self.todo(depth, st.source + " { ... }")

    # ---------------------------------------------------- background scripts

    @staticmethod
    def waits_in_loop(stmts: list[Stmt]) -> bool:
        """Does a loop of this script give way to the game? Then it runs alongside it."""
        def walk(body):
            for st in body:
                yield st
                yield from walk(st.body)
                yield from walk(st.orelse)
        return any(inner.kind == "expr" and inner.expr[:1] and inner.expr[0][1] in WAITS
                   for st in stmts if st.kind == "loop" for inner in walk(st.body))

    def background_event(self, target) -> Optional[str]:
        """An event for a script that animates a room while the game goes on, or None
        if the script does anything else or can't be translated whole."""
        events = self.background_events.setdefault(target.room, {})
        name = f"scumm_bg_{snake(target.name)}"
        if name not in events:
            todos, used = self.todos, set(self.used_globals)
            events[name] = None
            body = self.block(target.body, 1, Context(room=target.room, background=True))
            if all(BACKGROUND_LINE.match(line) for line in body):
                # A loop runs its `var`s every turn: they go once, at the top.
                declared = [line for line in body if line.startswith("\t") and line.strip().startswith("var ")]
                head = [f"\t{line.strip()}" for line in dict.fromkeys(declared)]
                events[name] = [f":{name}", *head, *[line for line in body if line not in declared], ""]
            else:
                self.todos, self.used_globals = todos, used
        return name if events[name] else None

    def background_loop(self, st: Stmt, depth: int, ctx: Context) -> list[str]:
        ind = "\t" * depth
        cond = self.condition(st.expr, ctx)
        if cond is None or st.name not in ("while", "do"):
            return self.todo(depth, st.source + " { ... }")
        body = self._nonempty(self.block(st.body, depth + 1, ctx, True), depth + 1)
        if st.name == "do":
            leave = cond if st.negate else self.negate(cond)
            return [ind + "while true:", *body, ind + f"\tif {leave}:", ind + "\t\tbreak"]
        test = self.negate(cond) if st.negate else cond
        return [ind + f"while {'true' if test == '1' else test}:", *body]

    def object_number(self, toks: list[Tok], ctx: Context) -> str:
        """An object known at runtime, as the number of SCUMM."""
        text = self.expression(toks, ctx) if len(toks) != 1 or not toks[0][1].startswith("obj_") else toks[0][1]
        return text.removeprefix("obj_")

    # --------------------------------------------------------- conversations

    def conversation_id(self, script) -> str:
        return f"scumm_conv_{snake(script.name)}"

    @staticmethod
    def is_closeup(script) -> bool:
        return any(st.expr[:3] == [("ident", "startRoom"), ("punct", "("), ("ident", script.room)]
                   for block in script.flat for st in block.body)

    def room_conversations(self, room: str) -> list[str]:
        """Lines for a room's :ready that go on with the conversation that brought us here.
        A scene change ends the event that asked for it, so this room's own event takes over."""
        lines = []
        for script in self.game.scripts.values():
            if script.room == room and script.flat and (event := self.conversation_event(script)):
                self.background_events.setdefault(room, {})[event[0][1:]] = event
                name = event[0][1:]
                if not self.is_closeup(script):
                    continue
                lines += [f'\tif {self.var("scummConversation")} == "{name}":',
                          f'\t\t{self.var("scummConversation")} = ""',
                          f'\t\tqueue_event("{snake(room)}", "{name}")']
        return lines

    def routine_event(self, script) -> Optional[str]:
        """An event for a script that runs alongside the game and moves actors around,
        or None if it does anything that needs the player or can't be read."""
        key = f"{script.room}::{script.name}"
        if key not in self.routines:
            name = f"scumm_bg_{snake(script.name)}"
            self.routines[key] = name       # they start each other
            saved = (self.event_room, self.room_vars, self.print_defaults, self.todos,
                     set(self.used_globals), self.leaves_to, self.ego_elsewhere)
            event = self.conversation_event(script, routine=True) if script.routine else None
            if event and all(ROUTINE_LINE.match(line) for line in event[1:]):
                self.background_events.setdefault(script.room, {})[name] = [f":{name}", *event[1:]]
            else:
                self.routines[key] = None
                self.todos, self.used_globals = saved[3:5]
            self.event_room, self.room_vars, self.print_defaults = saved[:3]
            self.leaves_to, self.ego_elsewhere = saved[5:]
        return self.routines[key]

    def script_event(self, target, ctx: Context) -> Optional[str]:
        """The event a script runs as, where something can ask if it's running."""
        if target is None or target.room != ctx.room:
            return None
        if target.flat:
            return self.conversation_id(target)
        return self.routine_event(target) if self.waits_in_loop(target.body) else None

    def conversation_event(self, script, routine: bool = False) -> Optional[list[str]]:
        """The whole script as a loop over its blocks: `pc` says which one runs next. ASHES
        has no goto, and a conversation jumps between its menu, its answers and its start."""
        closeup = not routine and self.is_closeup(script)
        blocks = script.routine if routine else script.flat
        ctx = Context(room=script.room, background=True, flat=True, closeup=closeup, routine=routine)
        self.event_room, self.room_vars, self.print_defaults = script.room, {}, {}
        self.leaves_to, self.ego_elsewhere = None, False
        declared = [v for st in script.body if st.kind == "decl" for _, v in st.expr]
        ctx.locals.update(declared)
        ctx.locals.update(script.params)
        body = []
        for block in blocks:
            body.append(f"\t\tif pc == {block.offset}:")
            lines = self.block(block.body, 3, ctx)
            if block.cond:
                # Skipping a line isn't skipping the override: it's never taken here.
                pre, cond = self.indexed_reads([("number", "0") if t[1] == "VAR_OVERRIDE" else t
                                                for t in self.literal_indexes(block.cond)], ctx)
                test = self.condition(cond, ctx)
                if test is None:
                    return None
                if block.target <= block.offset and self.waits_for_choice(block):
                    # On screen the ego's line goes over the ego: -1 asks for that.
                    x, y, color = self.print_place("ego", None, []) if closeup else ("-1", "-1", "15")
                    lines.append(f'\t\t\tscumm_choose("{self.var(cond[0][1])}", {x}, {y}, {color})')
                lines += [f"\t\t\t{line}" for line in pre]
                yes, no = (block.next, block.target) if block.negate else (block.target, block.next)
                lines += [f"\t\t\tif {test}:", f"\t\t\t\tpc = {yes}", "\t\t\telse:", f"\t\t\t\tpc = {no}"]
            else:
                lines.append(f"\t\t\tpc = {-1 if block.next is None else block.next}")
            body += lines
        end = [] if routine else ['\taccept_input("ALL")', "\tset_gui_visible(true)"]
        if self.leaves_to in self.scenes:
            end += ["\tscumm_fade_out()", f'\tchange_scene("{self.scenes[self.leaves_to]}")']
        head = [] if routine else ['\taccept_input("SKIP")', "\tset_gui_visible(false)"]
        return [f":{self.conversation_id(script)}", *head,
                *[f"\tvar {snake(p)} = {self.var(f'scummParam{n}')}" for n, p in enumerate(script.params)],
                *[f"\tvar {snake(v)} = 0" for v in declared], f"\tvar pc = {blocks[0].offset}",
                "\twhile true:", *body, "\t\tif pc == -1:", "\t\t\tbreak", *end, ""]

    @staticmethod
    def waits_for_choice(block) -> bool:
        """`do { ... } until(choice != 0)`: the game goes on until the player clicks."""
        return len(block.cond) == 3 and block.cond[0][0] == "ident" and [v for _, v in block.cond[1:]] == ["!=", "0"]

    def indexed_reads(self, toks: list[Tok], ctx: Context) -> tuple[list[str], list[Tok]]:
        """`b374[l1]`, with an index only known at runtime: read first into a variable."""
        vals = [v for _, v in toks]
        if "[" not in vals:
            return [], toks
        start = vals.index("[")
        end = vals.index("]", start)
        index = self.expression(toks[start + 1:end], ctx)
        name = vals[start - 1]
        line = f'scumm_get_var("{self.var("scummIndex")}", "{name}", {index})'
        return [line], [*toks[:start - 1], ("ident", "scummIndex"), *toks[end + 1:]]

    def print_place(self, who: str, keys: Optional[str], values: list[str]) -> tuple[str, str, str]:
        """(x, y, colour) of some text: its own, the defaults or SCUMM's top of the screen."""
        place = dict(self.print_defaults.get(who, {}))
        names = [k for k in (keys or "").split(",") if k]
        given = iter(values)
        for name in names:
            if name == "at":
                place["x"], place["y"] = next(given), next(given)
            else:
                place[name] = next(given)
        return place.get("x", "160"), place.get("y", "8"), place.get("color", "15")

    def plain_text(self, text: str) -> str:
        """Without v5's escapes for lines that go on where the last one ended: here each
        line takes its turn, in its own place."""
        return text.replace("\\k", "").replace("\\n", " ") if self.game.version < 6 else text

    def text_lines(self, text: str, x: str, y: str, color: str) -> list[str]:
        if "%" in text:
            raise Untranslatable("formatted text")
        text = self.plain_text(text)
        quoted = (part.strip().replace(chr(34), chr(92) + chr(34)) for part in text.split("\\w"))
        return [f'scumm_print("{part}", {x}, {y}, {color})' for part in quoted if part]

    def print_statement(self, name: str, args: list[list[Tok]], ctx: Context) -> Optional[list[str]]:
        """sysPrint, and egoSay where the ego isn't on screen."""
        extra = [a[0][1] if a[0][0] == "string" else self.expression(a, ctx) for a in args[1:]]
        if name == "printDefaults":
            who = args[0][0][1]
            x, y, color = self.print_place(who, extra[0], extra[1:])
            self.print_defaults[who] = {"x": x, "y": y, "color": color}
            return []
        if args[0][0][0] != "string":
            raise Untranslatable(name)
        who = "ego" if name == "egoSay" else "255"
        return self.text_lines(args[0][0][1], *self.print_place(who, extra[0] if extra else None, extra[1:]))

    def flat_statement(self, st: Stmt, ctx: Context) -> Optional[list[str]]:
        """What a conversation does differently, or None for the usual translation."""
        toks = st.expr
        vals = [v for _, v in toks]
        args = self._split_args(toks[2:-1]) if vals[1:2] == ["("] and vals[-1] == ")" else []
        ego = [self.game.ego, "VAR_EGO"]
        if ctx.routine:
            routine = self.routine_statement(st, toks, vals, args, ctx)
            if routine is not None:
                return routine
        if vals[0] == "stopScript" and args:
            return self.stop_script(args, ctx)
        if vals[0] in MENU_CALLS:
            return []
        if vals[0] in ("startRoom", "putActorInRoom", "putActorAt", "setCurrentActor", "setActorElevation") \
                and not ctx.closeup and vals[0] != "setCurrentVerb":
            return None
        if vals[0] == "setCurrentVerb":
            self.choice_verb = self.expression(args[0], ctx)
            return []
        if vals[0] == "setVerbName" and self.choice_verb and args[0][0][0] == "string":
            text = args[0][0][1].replace(chr(34), chr(92) + chr(34))
            return [f'scumm_choice({self.choice_verb}, "{text}")']
        if vals[1:2] == ["["] and vals[3:4] != ["]"] or vals[1:4] == ["[", "g100", "]"]:
            return []       # the menu's own bookkeeping
        if vals[0] == "startRoom":
            if args[0][0][1] != ctx.room:
                self.leaves_to = args[0][0][1]
            return []
        if vals[0] == "putActorInRoom" and [v for _, v in args[0]][0] in ego:
            room = args[1][0][1]
            self.ego_elsewhere = room != ctx.room
            return [f'{self.actor_room_var(self.game.ego)} = "{room}"']
        if vals[0] == "putActorAt" and [v for _, v in args[0]][0] in ego:
            if not self.ego_elsewhere:
                return []
            self.used_globals |= {"ego_entry_x", "ego_entry_y"}
            return [f"ego_entry_x = {int(args[1][0][1], 0) * self.scale}",
                    f"ego_entry_y = {int(args[2][0][1], 0) * self.scale}"]
        if vals[0] in ("setCurrentActor", "setActorElevation"):
            return []       # the ego stays out of sight here
        if len(vals) > 3 and vals[1] == "::":
            target = self.script_def(f"{vals[0]}::{vals[2]}", ctx)
            if target is not None and target.room != ctx.room:
                # The game's scripts for the verb bar and the menu: done by the commands.
                calls = {s.expr[0][1] for s in self._all(target.body) if s.kind == "expr" and s.expr}
                return [] if calls & MENU_CALLS else None
            if target is not None:
                if self.waits_in_loop(target.body):
                    event = self.routine_event(target)
                    if event:
                        return [f'queue_event("{snake(target.room)}", "{event}", "{event}")']
                    if ctx.routine:
                        return []       # e.g. watching the ego: the rest goes on without it
                    raise Untranslatable(st.source)
                given = self._split_args(toks[4:-1]) if len(toks) > 5 else []
                return self.background_call(target, given, ctx, st)
        return None

    def stop_script(self, args: list[list[Tok]], ctx: Context) -> list[str]:
        target = self.script_def("".join(v for _, v in args[0]), ctx)
        event = self.script_event(target, ctx)
        return [f'scumm_stop_bg("{event}")'] if event and event.startswith("scumm_bg_") else []

    def routine_statement(self, st: Stmt, toks: list[Tok], vals: list[str], args: list[list[Tok]],
                          ctx: Context) -> Optional[list[str]]:
        """A routine moves actors without touching what the player is doing."""
        s = self.scale
        if vals[0] == "walkActorTo" and len(args) == 3:
            who = self.actor_id(args[0])
            if who:
                return [f'scumm_walk("{who}", {self.expression(args[1], ctx)} * {s}, '
                        f'{self.expression(args[2], ctx)} * {s})']
        if vals[0] == "walkActorToObj" and len(args) >= 2:
            who = self.actor_id(args[0])
            if who:
                return [f'scumm_walk_object("{who}", {self.object_number(args[1], ctx)})']
        if vals[0] == "startObject" and len(args) >= 3 and [v for _, v in args[2]] in (["Open"], ["Close"]):
            state = 1 if args[2][0][1] == "Open" else 0
            return [f"scumm_draw({self.object_number(args[1], ctx)}, {state})"]
        if vals[1:3] == ["=", "isScriptRunning"]:
            target = self.script_def("".join(v for _, v in self._split_args(toks[4:-1])[0]), ctx)
            event = self.script_event(target, ctx)
            name = self.var(vals[0], ctx)
            self.room_vars.pop(name, None)
            return [f'scumm_running("{name}", "{event}")'] if event else [f"{name} = 0"]
        return None

    def background_statement(self, st: Stmt, ctx: Context) -> Optional[list[str]]:
        """What only a background script can do, or None for the usual translation."""
        toks = st.expr
        vals = [v for _, v in toks]
        args = self._split_args(toks[2:-1]) if vals[1:2] == ["("] and vals[-1] == ")" else []
        if ctx.flat:
            flat = self.flat_statement(st, ctx)
            if flat is not None:
                return flat
        if vals[0] in ("breakScript", "breakHere"):
            return [f"wait({round((self.game.timer_next or 1) / 60, 2)})"]
        if vals[0] in ("drawObject", "setObjectState") and len(args) == 2:
            state = [v for _, v in args[1]]
            if state == ["0"] and ctx.flat:
                return [f"scumm_draw({self.object_number(args[0], ctx)}, 0)"]
            if state != ["1"]:
                raise Untranslatable(st.source)
            return [f"scumm_draw({self.object_number(args[0], ctx)})"]
        if vals[0] == "delay" and args and args[0][0][0] != "number":
            return [f"wait({self.expression(args[0], ctx)} / 60.0)"]
        if "getRandomNumber" in vals:
            start = vals.index("getRandomNumber")
            end = self._match(toks, start + 1)
            limit = self.expression(toks[start + 2:end], ctx)
            rest = [*toks[:start], ("ident", "scummRandom"), *toks[end + 1:]]
            return [f'scumm_rand("{self.var("scummRandom")}", {limit})',
                    *self.expr_statement(dataclasses.replace(st, expr=rest), ctx, None)]
        if vals[1:4] == ["=", "getObjectState", "("]:
            if vals[0] in ctx.locals:
                raise Untranslatable(st.source)
            number = self.object_number(self._split_args(toks[4:-1])[0], ctx)
            self.room_vars.pop(self.var(vals[0]), None)
            return [f'scumm_get_state("{self.var(vals[0])}", {number})']
        if vals[0] == "startScript" and len(args) >= 2:
            target = self.script_def("".join(v for _, v in args[1]), ctx)
            given = self._split_args(args[2][1:-1]) if len(args) > 2 and len(args[2]) > 2 else []
            return self.background_call(target, given, ctx, st)
        return None

    def background_call(self, target, given: list[list[Tok]], ctx: Context, st: Stmt) -> list[str]:
        """A short script a background script starts: it runs in place, with its own
        locals, since both can have an `l1`."""
        if target is None or target.room != ctx.room or ctx.inline_depth >= 4 \
                or len(given) != len(target.params) or any(len(a) != 1 for a in given):
            raise Untranslatable(st.source)
        values = dict(zip(target.params, (a[0] for a in given)))
        for decl in (s for s in target.body if s.kind == "decl"):
            values.update({v: ("ident", f"{target.name}_{v}") for _, v in decl.expr})
        body = [self.with_values(s, values) for s in target.body]
        sub = dataclasses.replace(ctx, inline_depth=ctx.inline_depth + 1, locals=set(ctx.locals))
        return self.block(body, 0, sub)

    def conversation_event_ok(self, script) -> bool:
        """Can this script run as a conversation? Asked from any other event."""
        key = f"{script.room}::{script.name}"
        if key not in self.conversations_ok:
            saved = (self.event_room, self.room_vars, self.print_defaults, self.todos, set(self.used_globals))
            self.conversations_ok[key] = self.conversation_event(script) is not None
            self.event_room, self.room_vars, self.print_defaults, self.todos, self.used_globals = saved
        return self.conversations_ok[key]

    def random_switch(self, st: Stmt, depth: int, ctx: Context, in_loop: bool) -> list[str]:
        """switch(getRandomNumber(n)): a random global, and one `if` per case."""
        ind = "\t" * depth
        var = self.var("scummRandom")
        out = [ind + f'rand_global("{var}", {int(st.expr[2][1], 0)})']
        numbers = [int(lbl[0][1], 0) for case in st.cases for lbl in case.labels]
        for case in st.cases:
            body = list(case.body)
            while body and body[-1].kind == "break":
                body.pop()
            if case.labels:
                test = " or ".join(f"{var} == {int(lbl[0][1], 0)}" for lbl in case.labels)
            else:
                test = " and ".join(f"{var} != {n}" for n in numbers) or "true"
            out.append(ind + f"if {test}:")
            out += self._nonempty(self.block(body, depth + 1, ctx, in_loop), depth + 1)
        return out

    @classmethod
    def with_values(cls, st: Stmt, values: dict[str, Tok]) -> Stmt:
        """A copy of a statement with parameters replaced by their values."""
        def tokens(toks):
            return [values.get(t[1], t) if t[0] == "ident" else t for t in toks]

        return Stmt(st.kind, st.line, expr=tokens(st.expr),
                    body=[cls.with_values(b, values) for b in st.body],
                    orelse=[cls.with_values(b, values) for b in st.orelse],
                    cases=[Case(c.labels, [cls.with_values(b, values) for b in c.body]) for c in st.cases],
                    name=st.name, negate=st.negate, source=st.source)

    @staticmethod
    def is_wait_for_script(st: Stmt) -> bool:
        """`while(isScriptRunning(x)) breakScript();`"""
        vals = [v for _, v in st.expr]
        body = st.body[0] if st.body else None
        return st.name == "while" and not st.negate and vals[:1] == ["isScriptRunning"] and \
            body is not None and body.kind == "expr" and [v for _, v in body.expr[:1]] in (["breakScript"], ["breakHere"])

    # ---------------------------------------------------------------- dialogs

    @staticmethod
    def _single(st: Stmt) -> Stmt:
        """The statement inside a block of just one."""
        return st.body[0] if st.kind == "block" and len(st.body) == 1 else st

    @staticmethod
    def _dialog_text(st: Stmt) -> Optional[str]:
        """The text of `sentence[0] = "text";`."""
        vals = [v for _, v in st.expr]
        if st.kind == "expr" and len(vals) == 6 and vals[1:5] == ["[", "0", "]", "="] \
                and st.expr[5][0] == "string":
            return vals[5]
        return None

    def _dialog_choices(self, st: Stmt) -> Optional[list[tuple[str, Optional[Stmt], bool]]]:
        """Dialog choices a statement prepares: (text, condition, is the else branch)."""
        text = self._dialog_text(st)
        if text is not None:
            return [(text, None, False)]
        if st.kind != "if":
            return None
        then = self._dialog_text(self._single(st.body[0])) if len(st.body) == 1 else None
        other = self._dialog_text(self._single(st.orelse[0])) if len(st.orelse) == 1 else None
        found = [(t, st, flip) for t, flip in ((then, False), (other, True)) if t is not None]
        return found or None

    def _dialog_head(self, text: str, cond_st: Optional[Stmt], is_else: bool, ind: str,
                     ctx: Context) -> list[str]:
        """The `- "text" [condition]` line of a choice."""
        out = []
        head = f'{ind}\t- "{text.replace(chr(34), chr(92) + chr(34))}"'
        if cond_st is not None:
            cond = self.condition(cond_st.expr, ctx)
            if cond is not None and cond_st.negate != is_else:
                cond = self.negate(cond)
            if cond is None:
                out.append(ind + f"\t# TODO scumm: option condition {cond_st.source}")
                self.todos += 1
            elif cond != "true":
                head += f" [{cond}]"
        return out + [head]

    def _for_choice(self, stmts: list[Stmt], n: int) -> list[Stmt]:
        """What runs once choice n is known: its case, its side of `if(selectedSentence...)`."""
        out: list[Stmt] = []
        for st in stmts:
            vals = [v for _, v in st.expr]
            if st.kind == "block":
                out += self._for_choice(st.body, n)
            elif st.kind == "switch" and vals == ["selectedSentence"]:
                match = next((c for c in st.cases for lbl in c.labels
                              if lbl and lbl[0][0] == "number" and int(lbl[0][1], 0) == n), None)
                match = match or next((c for c in st.cases if not c.labels), None)
                if match is not None:
                    out += self._for_choice([b for b in match.body if b.kind != "break"], n)
            elif st.kind == "if" and len(vals) == 3 and vals[:2] == ["selectedSentence", "=="]:
                taken = (int(vals[2], 0) == n) != st.negate
                out += self._for_choice(st.body if taken else st.orelse, n)
            elif st.kind == "expr" and vals[1:2] == ["="] and "selectedSentence" in vals:
                pass        # sentence = dialogList[selectedSentence];
            elif st.kind == "expr" and vals[:1] == ["egoSay"] and len(vals) > 2 and vals[2].startswith("%s{"):
                pass        # the ego says the chosen line, already in the answer
            else:
                out.append(st)
        return out

    def linear_dialog(self, stmts: list[Stmt], start: int, depth: int, ctx: Context,
                      in_loop: bool) -> Optional[tuple[list[str], int]]:
        """A dialog asked once, with no loop around it: (lines, index after it).

        Code after it can read the choice, so each answer stores it.
        """
        if self._dialog_choices(stmts[start]) is None:
            return None
        options: list[tuple[int, str, Optional[Stmt], bool]] = []
        pending: list[tuple[str, Optional[Stmt], bool]] = []
        text_vars: set[str] = set()
        assigns: list[Stmt] = []
        added = 0
        room = None
        i = start
        while True:
            if i >= len(stmts):
                return None
            st = stmts[i]
            vals = [v for _, v in st.expr]
            i += 1
            choices = self._dialog_choices(st)
            if choices is not None:
                pending = choices
                inner = st if st.kind == "expr" else self._single((st.body if choices[0][2] is False
                                                                  else st.orelse)[0])
                text_vars.add(inner.expr[0][1])
            elif st.kind == "expr" and vals and vals[-1] == ")" and "dialogAdd" in vals:
                options += [(added, *p) for p in pending]
                pending = []
                added += 1
                room = vals[0] if len(vals) > 2 and vals[1] == "::" else room
            elif st.kind == "expr" and "dialogStart" in vals:
                self._dialog_colors_from(st, vals)
                break
            elif st.kind == "expr" and vals[1:2] == ["="]:
                assigns.append(st)
            else:
                return None
        if not options:
            return None
        rest: list[Stmt] = []
        while i < len(stmts):
            st = stmts[i]
            vals = [v for _, v in st.expr]
            if st.kind == "loop" and "selectedSentence" in vals or "dialogHide" in vals:
                i += 1
            elif st.kind == "cutscene":
                rest += st.body[0].body if st.body[0].kind == "block" else st.body
                i += 1
                break
            else:
                break
        if i < len(stmts) and "dialogClear" in [v for _, v in stmts[i].expr]:
            i += 1
        if room:
            self.dialog_rooms.add(room)
        assigns = [st for st in assigns if st.expr[0][1] not in text_vars]
        ind = "\t" * depth
        out = [ind + "set_gui_visible(false)", ind + "?!"]
        for n, text, cond_st, is_else in options:
            out += self._dialog_head(text, cond_st, is_else, ind, ctx)
            choice = Stmt("expr", expr=[("ident", "selectedSentence"), ("punct", "="), ("number", str(n))])
            say = Stmt("expr", expr=[("ident", "egoSay"), ("punct", "("), ("string", text), ("punct", ")")])
            lines = self.block([choice, *assigns, say, *self._for_choice(rest, n)], depth + 2, ctx, in_loop=True)
            out += self._nonempty(lines + [ind + "\t\tdone"], depth + 2)
        out.append(ind + "set_gui_visible(true)")
        return out, i

    def _dialog_colors_from(self, st: Stmt, vals: list[str]) -> None:
        """dialogStart(color, hiColor)."""
        if self.dialog_colors is None:
            args = self._split_args(st.expr[vals.index("(") + 1:-1])
            nums = [self.game.defines.get(a[0][1]) if a[0][0] == "ident" else int(a[0][1], 0)
                    for a in args if len(a) == 1]
            if len(nums) == 2 and all(isinstance(n, int) for n in nums):
                self.dialog_colors = (nums[0], nums[1])

    def dialog(self, loop: Stmt, depth: int, ctx: Context) -> Optional[list[str]]:
        """The ScummC dialog idiom (examples' dialog.scc) -> an ASHES `?!` block.

            while(1) {
                [if(cond)] s[0] = "text"; X::dialogAdd(s);     // option N
                X::dialogStart(..); wait selectedSentence; X::dialogHide();
                ... switch(selectedSentence) { case N: answer }
                if(selectedSentence == K) break <loop>;       // option K ends it
            }
        """
        if loop.kind != "loop" or loop.name != "while" or [v for _, v in loop.expr] != ["1"]:
            return None
        body = loop.body[0].body if loop.body and loop.body[0].kind == "block" else loop.body
        # (case number, text, statement with the condition, whether it's the else branch)
        options: list[tuple[int, str, Optional[Stmt], bool]] = []
        pending: list[tuple[str, Optional[Stmt], bool]] = []
        added = 0
        text_vars: set[str] = set()
        assigns: list[Stmt] = []
        switch: Optional[Stmt] = None
        exits: set[int] = set()

        single, text_of = self._single, self._dialog_text

        def find_switch(stmts):
            for st in stmts:
                if st.kind == "switch" and [v for _, v in st.expr] == ["selectedSentence"]:
                    return st
                found = find_switch(st.body) or find_switch(st.orelse)
                if found:
                    return found
            return None

        for st in body:
            vals = [v for _, v in st.expr]
            choices = self._dialog_choices(st)
            if choices is not None:
                # A text for each branch means two choices that exclude each other.
                pending = choices
                inner = st if st.kind == "expr" else single((st.orelse if choices[0][2] else st.body)[0])
                text_vars.add(inner.expr[0][1])
            elif st.kind == "expr" and vals and vals[-1] == ")" and "dialogAdd" in vals:
                options += [(added, *p) for p in pending]
                pending = []
                added += 1
                if len(vals) > 2 and vals[1] == "::":
                    self.dialog_rooms.add(vals[0])
            elif st.kind == "expr" and "dialogStart" in vals:
                self._dialog_colors_from(st, vals)
            elif st.kind == "if" and [v for _, v in st.expr][:2] == ["selectedSentence", "=="] \
                    and st.body and st.body[0].kind == "break":
                exits.add(int(st.expr[2][1], 0))
            elif st.kind == "expr" and vals[1:2] == ["="]:
                assigns.append(st)
            elif switch is None:
                switch = find_switch([st])
        if not options or switch is None:
            return None
        # The loop's own assignments: a `?!` doesn't run them, so each answer does.
        assigns = [st for st in assigns if st.expr[0][1] not in text_vars]

        ind = "\t" * depth
        # dialogStart hides the verb bar (showVerbs(0)), dialogEnd brings it back.
        out = [ind + "set_gui_visible(false)", ind + "?!"]
        for n, text, cond_st, is_else in options:
            out += self._dialog_head(text, cond_st, is_else, ind, ctx)
            # SCUMM has the ego say the chosen line, then runs its case.
            answer = assigns + [Stmt("expr", expr=[("ident", "egoSay"), ("punct", "("), ("string", text),
                                                   ("punct", ")")])]
            for case in switch.cases:
                if any(lbl and lbl[0][0] == "number" and int(lbl[0][1], 0) == n for lbl in case.labels):
                    answer += [b for b in case.body]
            while answer and answer[-1].kind in ("break", "return"):
                answer.pop()
            lines = self.block(answer, depth + 2, ctx, in_loop=True)
            if n in exits:
                lines.append(ind + "\t\tdone")
            out += self._nonempty(lines, depth + 2)
        out.append(ind + "set_gui_visible(true)")
        return out

    @staticmethod
    def unroll(st: Stmt, limit: int = 16) -> Optional[list[Stmt]]:
        """for(i = A; i < B; i++) with literal bounds -> copies of the body."""
        init, body, step = st.body
        iv = [v for _, v in init.expr]
        cv = [v for _, v in st.expr]
        sv = [v for _, v in step.expr]
        if len(iv) != 3 or iv[1] != "=" or len(cv) != 3 or cv[0] != iv[0]:
            return None
        up = cv[1] in ("<", "<=") and sv in ([iv[0], "++"], ["++", iv[0]])
        down = cv[1] in (">", ">=") and sv in ([iv[0], "--"], ["--", iv[0]])
        if not (up or down):
            return None
        try:
            start, bound = int(iv[2], 0), int(cv[2], 0)
        except ValueError:
            return None
        if up:
            values = list(range(start, bound + (1 if cv[1] == "<=" else 0)))
        else:
            values = list(range(start, bound - (1 if cv[1] == ">=" else 0), -1))
        if len(values) > limit:
            return None

        def subst(s: Stmt, n: int) -> Stmt:
            return Stmt(s.kind, s.line,
                        expr=[("number", str(n)) if t == ("ident", iv[0]) else t for t in s.expr],
                        body=[subst(b, n) for b in s.body], orelse=[subst(b, n) for b in s.orelse],
                        cases=s.cases, name=s.name, negate=s.negate,
                        source=re.sub(rf"\b{iv[0]}\b", str(n), s.source))
        return [subst(body, n) for n in values]

    def if_chain(self, st: Stmt, depth: int, ctx: Context, in_loop: bool) -> list[str]:
        ind = "\t" * depth
        out: list[str] = []
        keyword = "if"
        while True:
            cond = self.condition(st.expr, ctx)
            if cond is not None and st.negate:
                cond = self.negate(cond)
            if cond == "true":
                # Statically known: keep only the taken branch.
                if keyword == "if":
                    return out + self.block(st.body, depth, ctx, in_loop)
                out += [ind + "else:"] + self._nonempty(self.block(st.body, depth + 1, ctx, in_loop), depth + 1)
                return out
            if cond == "false":
                if st.orelse:
                    nested = st.orelse[0]
                    if nested.kind == "if":
                        st = nested
                        continue
                    if keyword == "if":
                        return out + self.block(st.orelse, depth, ctx, in_loop)
                    out += [ind + "else:"] + self._nonempty(self.block(st.orelse, depth + 1, ctx, in_loop), depth + 1)
                return out
            if cond is None:
                self.todos += 1
                out.append(ind + f"# TODO scumm: condition {st.source}")
                cond = "false"
            out.append(ind + f"{keyword} {cond}:")
            out += self._nonempty(self.block(st.body, depth + 1, ctx, in_loop), depth + 1)
            if not st.orelse:
                return out
            nested = st.orelse[0]
            if nested.kind == "if":
                keyword = "elif"
                st = nested
                continue
            out.append(ind + "else:")
            out += self._nonempty(self.block(st.orelse, depth + 1, ctx, in_loop), depth + 1)
            return out

    @staticmethod
    def _nonempty(lines: list[str], depth: int) -> list[str]:
        if not any(not line.strip().startswith("#") for line in lines):
            lines.append("\t" * depth + "pass")
        return lines

    # ---------------------------------------------------------- expressions

    def cutscene_verbs(self, script_key: Optional[str], n: int) -> list[bool]:
        """showVerbs(...) calls a cutscene start/end script makes for cutscene(n)."""
        script = self.game.scripts.get(script_key or "")
        if script is None:
            return []
        from ..frontend.scummc_eval import SetupEvaluator
        params = {p: n for p in script.params}
        found: list[bool] = []

        def walk(stmts):
            for st in stmts:
                vals = [v for _, v in st.expr]
                if st.kind == "expr" and vals[:1] in (["saveVerbs"], ["restoreVerbs"]):
                    found.append(vals[0] == "restoreVerbs")     # v5: the bar is put away and back
                elif st.kind == "expr" and vals and vals[-1] == ")" and "showVerbs" in vals:
                    arg = SetupEvaluator(dict(self.game.defines), lambda *a: None).value(
                        st.expr[vals.index("(") + 1:-1])
                    if isinstance(arg, int):
                        found.append(bool(arg))
                elif st.kind == "if":
                    ev = SetupEvaluator(dict(self.game.defines), lambda *a: None)
                    ev.vars.update(params)
                    cond = ev.value(st.expr)
                    if isinstance(cond, int):
                        walk(st.body if bool(cond) != st.negate else st.orelse)
                    elif any(s.expr[:1] == [("ident", "restoreVerbs")] for s in statements(st.body)):
                        walk(st.body)       # unknown here: "unless a boot script runs"
                elif st.kind == "block":
                    walk(st.body)
        walk(script.body)
        return found

    @staticmethod
    def constant(text: str) -> Optional[int]:
        """Value of an expression made only of numbers and + - * / ( )."""
        if not re.fullmatch(r"[\d\s+\-*/()]+", text):
            return None
        try:
            return int(eval(text, {"__builtins__": {}}, {}))   # digits and operators only
        except Exception:
            return None

    @staticmethod
    def negate(cond: str) -> str:
        if cond in ("true", "false"):
            return "false" if cond == "true" else "true"
        return f"!({cond})"

    def var(self, name: str, ctx: Optional[Context] = None) -> str:
        g = snake(name)
        if ctx is not None and name in ctx.locals:
            return g
        self.used_globals.add(g)
        return g

    def actor_room_var(self, actor: str) -> str:
        """Global holding the room an actor is in (SCUMM actors live in one room)."""
        g = f"{snake(actor)}_room"
        self.used_globals.add(g)
        return g

    def obj_key(self, toks: list[Tok], ctx: Context) -> Optional[str]:
        key = self._obj_key(toks, ctx)
        return key if key in self.ids else None

    def _obj_key(self, toks: list[Tok], ctx: Context) -> Optional[str]:
        vals = [v for _, v in toks]
        if vals in (["objA"], ["VAR_ME"]) and ctx.obj is not None:
            return ctx.obj.key
        if len(vals) == 1 and toks[0][0] == "number":
            # Passed to a script as a parameter: numbers there have no kind.
            return self.numbered_objects.get(vals[0])
        if vals == ["VAR_RETURN"] and ctx.var_return:
            return ctx.var_return
        if len(vals) == 3 and vals[1] == "::":
            key = f"{vals[0]}::{vals[2]}"
            return key if key in self.game.objects else None
        if len(vals) == 1 and ctx.room and f"{ctx.room}::{vals[0]}" in self.game.objects:
            return f"{ctx.room}::{vals[0]}"
        return None

    def actor_id(self, toks: list[Tok]) -> Optional[str]:
        vals = [v for _, v in toks]
        if vals == ["VAR_EGO"] or (len(vals) == 1 and vals[0] == self.game.ego):
            return "player"
        if len(vals) == 1 and vals[0] in self.game.actors:
            return self.ids.get(vals[0])
        return None

    def ref_key(self, toks: list[Tok], ctx: Context) -> Optional[str]:
        """An object key or actor name for `x`, `Room::x` or an actor."""
        vals = [v for _, v in toks]
        if len(vals) == 1 and vals[0] in self.game.actors:
            return vals[0]
        return self._obj_key(toks, ctx)

    def ref_classes(self, ref: Optional[str]) -> Optional[set[str]]:
        if ref in (None, "*"):
            return None
        if ref in self.game.actors:
            return set(self.game.actors[ref].classes)
        obj = self.game.objects.get(ref)
        return set(obj.classes) if obj else None

    def ref_id(self, ref: str) -> Optional[str]:
        if ref == self.game.ego:
            return "player"
        return self.inventory_ids.get(ref) or self.ids.get(ref)

    def objB_targets(self, stmts: list[Stmt], ctx: Context) -> list[str]:
        """Second objects a verb body singles out: switch(objB) cases, objB == X."""
        found: list[str] = []

        def walk(sts):
            for st in sts:
                if st.kind == "switch" and [v for _, v in st.expr] == ["objB"]:
                    for case in st.cases:
                        for lbl in case.labels:
                            key = self.ref_key(lbl, ctx)
                            if key and key not in found:
                                found.append(key)
                vals = [v for _, v in st.expr]
                for i in range(len(vals) - 2):
                    if vals[i] == "objB" and vals[i + 1] in ("==", "!="):
                        j = i + 5 if i + 3 < len(vals) and vals[i + 3] == "::" else i + 3
                        key = self.ref_key(st.expr[i + 2:j], ctx)
                        if key and key not in found:
                            found.append(key)
                walk(st.body)
                walk(st.orelse)
                for case in st.cases:
                    walk(case.body)
        walk(stmts)
        return found

    def actor_name(self, toks: list[Tok]) -> Optional[str]:
        vals = [v for _, v in toks]
        if vals == ["VAR_EGO"]:
            return self.game.ego
        if len(vals) == 1 and vals[0] in self.game.actors:
            return vals[0]
        return None

    def scumm_anim(self, actor: Optional[str], toks: list[Tok]) -> Optional[str]:
        """`zif_anim_beam` (the costume header's names) -> `beam`."""
        vals = [v for _, v in toks]
        if len(vals) != 1 or actor is None:
            return None
        cost = self.game.costumes.get(self.game.costume_of(actor, self.room) or "")
        if cost is None:
            return None
        for anim in cost.anims:
            if vals[0] == f"{cost.name}_anim_{anim}":
                return anim
        # Another costume's constant (`zif_anim_raiseArm` on Zob): SCUMM only sees
        # its number, the "@ N" slot, and plays whatever this costume has there.
        number = self.anim_number(vals[0])
        if number is not None:
            for anim, data in cost.anims.items():
                if self.slot(anim, data) == number:
                    return anim
        return None

    # The fixed slots of a costume's standard animations (ScummC's *_anim.sch).
    STANDARD_SLOTS = {"init": 1, "walk": 2, "stand": 3, "talkStart": 4, "talkStop": 5}

    def slot(self, name: str, anim) -> Optional[int]:
        return anim.index if anim.index is not None else self.STANDARD_SLOTS.get(name)

    def anim_number(self, constant: str) -> Optional[int]:
        """`<costume>_anim_<name>` -> its slot number."""
        for cost in self.game.costumes.values():
            prefix = f"{cost.name}_anim_"
            if constant.startswith(prefix) and constant[len(prefix):] in cost.anims:
                name = constant[len(prefix):]
                return self.slot(name, cost.anims[name])
        return None

    def played_anim(self, actor: str, anim: str, single_direction: bool = False) -> Optional[str]:
        """Escoria name of a costume animation worth playing: a real animation
        (several frames, or any if single_direction), optionally only when the
        costume draws it the same way in every direction."""
        cost = self.game.costumes.get(self.game.costume_of(actor, self.room) or "")
        if cost is None or anim not in cost.anims or not self.anim_name:
            return None
        dirs = cost.anims[anim].directions
        frames = [tuple(cost.anims[anim].frames_for(d)[0]) for d in dirs]
        if single_direction:
            if len(set(frames)) != 1:
                return None
        elif not any(len(f) > 1 for f in frames) or all(cost.anims[anim].frames_for(d)[1] for d in dirs):
            return None         # a single still frame, or a looping animation
        return self.anim_name(actor, anim)

    def roam_call(self, target, args, ctx: Context) -> Optional[list[str]]:
        """Calls to the game's roaming library -> scumm_roam(actor, op, ...)."""
        lib = self.game.roaming
        if lib is None:
            return None
        key = f"{target.room}::{target.name}"
        ops = {lib.start: "start", lib.pause: "pause", lib.resume: "resume", lib.stop: "stop"}
        op = ops.get(key)
        if op is None or not args:
            return None
        actor = self.actor_name(args[0])
        aid = self.ref_id(actor) if actor else None
        if aid is None:
            return None
        if op != "start":
            return [f'scumm_roam("{aid}", "{op}")']
        nums = [self.constant(self.expression(args[i], ctx)) if i < len(args) else None
                for i in lib.start_params]
        if None in nums:
            return None
        x0, x1, y0, y1 = (n * self.scale for n in nums)
        return [f'scumm_roam("{aid}", "start", {x0}, {x1}, {y0}, {y1})']

    def waits_for_sound(self, st: Stmt) -> bool:
        """A loop waiting for the sound just started: a title screen, say. Songs aren't
        converted, so there's nothing to wait for but some time."""
        values = [v for s in statements(st.body) for _, v in s.expr]
        return "isSoundRunning" in values and self.last_sound in values

    def ego_entry(self, args: list) -> tuple[int, int]:
        """Where the ego appears: the object's walk-to point. The x, y of the
        instruction are where it walks to from there."""
        obj = self.game.objects.get("".join(v for _, v in args[0]))
        if obj is None:
            raise Untranslatable("startRoomWithEgo")
        spot = obj.states[0] if obj.states else obj
        return obj.x + spot.hs_x, obj.y + spot.hs_y

    def script_def(self, name: str, ctx: Context):
        key = name if "::" in name else f"{ctx.room}::{name}"
        return self.game.scripts.get(key)

    @staticmethod
    def inlinable(body: list[Stmt]) -> bool:
        """No `return` except at the very end (it would stop the caller)."""
        def has_return(stmts):
            for st in stmts:
                if st.kind == "return" or has_return(st.body) or has_return(st.orelse) or \
                        any(has_return(c.body) for c in st.cases):
                    return True
            return False
        inner = body[:-1] if body and body[-1].kind == "return" else body
        return not has_return(inner)

    def object_state_var(self, key: str) -> str:
        g = f"{self.ids[key]}_state"
        self.used_globals.add(g)
        return g

    def condition(self, toks: list[Tok], ctx: Context) -> Optional[str]:
        before = set(self.used_globals)
        try:
            text = self.expression(toks, ctx)
        except Untranslatable:
            self.used_globals = before
            return None
        return self.room_comparisons(self.simplify(text))

    def room_comparisons(self, text: str) -> str:
        """Where an actor is goes in a name here, and in a number in a compiled game.
        A variable keeps numbers: a name in it would break its other comparisons."""
        if self.game.version >= 6:
            return text
        holders = "|".join([r"\w+_room", *map(re.escape, self.room_vars)])

        def name(match: re.Match) -> str:
            value = match.group(3)
            room = self.event_room if value == "var_room" else self.game.room_numbers.get(int(value), value)
            return f'{self.room_vars.get(match.group(1), match.group(1))} {match.group(2)} "{room}"'

        return re.sub(rf"\b({holders}) (==|!=) (\d+|var_room)\b", name, text)

    @staticmethod
    def simplify(text: str) -> str:
        text = text.strip()
        for _ in range(5):
            prev = text
            text = re.sub(r"!\s*true\b", "false", text)
            text = re.sub(r"!\s*false\b", "true", text)
            text = re.sub(r"\(\s*(true|false)\s*\)", r"\1", text)
            text = re.sub(r"\btrue and ", "", text)
            text = re.sub(r" and true\b", "", text)
            text = re.sub(r"\bfalse or ", "", text)
            text = re.sub(r" or false\b", "", text)
            if re.search(r"(^|\()false and [^()]*?(?=\)|$)", text):
                text = re.sub(r"(^|\()false and [^()]*?(?=\)|$)", r"\1false", text)
            if text == prev:
                break
        return text

    @staticmethod
    def literal_indexes(toks: list[Tok]) -> list[Tok]:
        """`b374[3]` in v5 is simply variable 377."""
        out = list(toks)
        for i in range(len(out) - 3, 0, -1):
            name, index = out[i - 1], out[i + 1]
            if out[i][1] == "[" and out[i + 2][1] == "]" and index[0] == "number" \
                    and re.fullmatch(r"[bg]\d+", str(name[1])):
                out[i - 1:i + 3] = [("ident", f"{name[1][0]}{int(name[1][1:]) + int(index[1], 0)}")]
        return out

    def expression(self, toks: list[Tok], ctx: Context) -> str:
        if self.game.version < 6:
            toks = self.literal_indexes(toks)
        out: list[str] = []
        i = 0
        while i < len(toks):
            kind, val = toks[i]
            nxt = toks[i + 1][1] if i + 1 < len(toks) else None
            if kind == "ident" and nxt == "(":
                end = self._match(toks, i + 1)
                out.append(self.call_expr(val, self._split_args(toks[i + 2:end]), ctx))
                i = end + 1
                continue
            if kind == "ident" and nxt == "::" and i + 2 < len(toks):
                key = f"{val}::{toks[i + 2][1]}"
                if key in self.game.objects:
                    raise Untranslatable(key)
                out.append(self.var(toks[i + 2][1]))
                i += 3
                continue
            if kind == "ident" and val in ("objA", "objB") and nxt == "is" and ctx.generic:
                negate = i + 2 < len(toks) and toks[i + 2][1] == "!"
                cls = toks[i + 3 if negate else i + 2][1]
                var = self.class_var(val, cls)
                out.append(("!" + var if negate else var) if var else ("true" if negate else "false"))
                i += 4 if negate else 3
                continue
            if kind == "ident" and val == "objA" and nxt == "<=" and i + 2 < len(toks) \
                    and toks[i + 2][0] == "number" and int(toks[i + 2][1], 0) == 15:
                # Numbers up to 15 are actors.
                if ctx.generic:
                    out.append("scumm_a_actor")
                else:
                    is_actor = ctx.obj is not None and ctx.obj.key in self.game.actor_objects.values()
                    out.append("true" if is_actor or "Person" in ctx.classes else "false")
                i += 3
                continue
            if kind == "ident" and nxt == "is" and ctx.background:
                negate = i + 2 < len(toks) and toks[i + 2][1] == "!"
                cls = toks[i + 3 if negate else i + 2][1]
                numbers = [n for n, key in self.numbered_objects.items() if self.game.objects[key].room == ctx.room
                           and cls in self.game.objects[key].classes]
                test = " or ".join(f"{self.var(val, ctx)} == {n}" for n in numbers) or "false"
                out.append(f"not ({test})" if negate else f"({test})")
                i += 4 if negate else 3
                continue
            if kind == "number" and nxt == "is":
                negate = i + 2 < len(toks) and toks[i + 2][1] == "!"
                cls = toks[i + 3 if negate else i + 2][1]
                key = self.obj_key([toks[i]], ctx)
                if key is None:
                    raise Untranslatable("number is")
                out.append("true" if (cls in self.ref_classes(key)) != negate else "false")
                i += 4 if negate else 3
                continue
            if kind == "ident" and val == "objA" and nxt == "is":
                negate = i + 2 < len(toks) and toks[i + 2][1] == "!"
                cls = toks[i + 3 if negate else i + 2][1]
                has = cls in ctx.classes
                out.append("true" if has != negate else "false")
                i += 4 if negate else 3
                continue
            if kind == "ident" and val == "objB" and nxt in ("==", "!="):
                other = self.ref_key(toks[i + 2:i + 5] if i + 3 < len(toks) and toks[i + 3][1] == "::"
                                     else toks[i + 2:i + 3], ctx)
                if ctx.objB in (None, "*") or other is None:
                    if ctx.objB == "*" and other is not None:
                        out.append("false" if nxt == "==" else "true")   # "any other" object
                        i += 5 if i + 3 < len(toks) and toks[i + 3][1] == "::" else 3
                        continue
                    raise Untranslatable("objB")
                same = other == ctx.objB
                out.append("true" if same == (nxt == "==") else "false")
                i += 5 if i + 3 < len(toks) and toks[i + 3][1] == "::" else 3
                continue
            if kind == "ident" and val == "objB" and nxt == "is":
                negate = i + 2 < len(toks) and toks[i + 2][1] == "!"
                cls = toks[i + 3 if negate else i + 2][1]
                classes = self.ref_classes(ctx.objB)
                if classes is None:
                    raise Untranslatable("objB is")
                out.append("true" if (cls in classes) != negate else "false")
                i += 4 if negate else 3
                continue
            if kind == "ident" and val == "objB" and ctx.generic:
                out.append('scumm_object_b != ""')
            elif kind == "ident" and val == "objB":
                out.append("true" if ctx.objB else "false")
            elif kind == "ident" and val == "vrb":
                if nxt in ("==", "!=") and i + 2 < len(toks):
                    same = toks[i + 2][1] == ctx.verb
                    out.append("true" if same == (nxt == "==") else "false")
                    i += 3
                    continue
                raise Untranslatable(val)
            elif kind == "ident" and val == "VAR_OVERRIDE" and ctx.in_override:
                out.append("false")
            elif kind == "ident" and val == "VAR_RETURN" and ctx.var_return == "$inventory_a":
                out.append('scumm_inventory_a != ""')
            elif kind == "ident" and val == "VAR_RETURN" and ctx.var_return:
                out.append("true")
            elif kind == "ident" and val == "VAR_EGO" and "__owner" in "".join(out[-3:]):
                out.append("__ego")     # getObjectOwner(x) == VAR_EGO
            elif kind == "ident" and val in ("VAR_EGO", "objA", "VAR_RETURN", "VAR_OVERRIDE", "is"):
                raise Untranslatable(val)
            elif kind == "ident" and val in self.game.rooms:
                out.append(f'"{val}"')       # rooms compare by name (see <actor>_room)
            elif kind == "ident":
                if val in self.game.actors or f"{ctx.room}::{val}" in self.game.objects:
                    raise Untranslatable(val)
                out.append(self.var(val, ctx))
            elif kind == "number":
                out.append(str(int(val, 0)))
            elif kind == "string":
                out.append('"' + val.replace('"', '\\"') + '"')
            elif val == "&&":
                out.append(" and ")
            elif val == "||":
                out.append(" or ")
            elif val in ("==", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%"):
                out.append(f" {val} ")
            elif val in ("!", "(", ")"):
                out.append(val)
            else:
                raise Untranslatable(val)
            i += 1
        text = "".join(out)
        # getObjectOwner(x) == 0xF: still in the world, i.e. not picked up.
        text = re.sub(r'__owner\("([^"]+)"\) == 15', r'!("\1" in inventory)', text)
        text = re.sub(r'__owner\("([^"]+)"\) != 15', r'("\1" in inventory)', text)
        text = re.sub(r'__owner\("([^"]+)"\)\s*==\s*__ego', r'("\1" in inventory)', text)
        text = re.sub(r'__owner\("([^"]+)"\)\s*!=\s*__ego', r'!("\1" in inventory)', text)
        if "__owner" in text:
            raise Untranslatable(text)
        return text

    def class_var(self, which: str, cls: str) -> Optional[str]:
        """objA / objB is <cls>, in the default actions: a global set per action."""
        if cls not in self.known_classes:
            return None         # no object has it
        return f"scumm_{'a' if which == 'objA' else 'b'}_{snake(cls)}"

    def generic_call(self, name: str, args, ctx: Context) -> Optional[list[str]]:
        """Calls on objA / VAR_RETURN in the default actions (ids known at runtime)."""
        first = [v for _, v in args[0]] if args else []
        if name == "startObject2" and first == ["objA"] and len(args) >= 2 and \
                [v for _, v in args[1]] == ["InventoryObject"]:
            ctx.var_return = "$inventory_a"     # the inventory item the object stands for
            return []
        if name == "pickupObject" and first == ["VAR_RETURN"] and ctx.var_return == "$inventory_a":
            return ["inventory_add(scumm_inventory_a)"]
        if name == "setObjectClass" and first == ["objA"] and len(args) == 2 and \
                "ClassUntouchable" in [v for _, v in args[1]]:
            untouchable = "0x80" in [v for _, v in args[1]]
            return [f"set_interactive(scumm_object_a, {'false' if untouchable else 'true'})"]
        if name == "setObjectState" and first == ["objA"] and len(args) == 2:
            n = self.constant(self.expression(args[1], ctx))
            if n is not None:
                return [f'set_state(scumm_object_a, "state_{n}")', f"scumm_state_var(scumm_object_a, {n})"]
        return None

    def find_ui_scripts(self) -> set[str]:
        """Background scripts of the game's interface: an endless `while(1)` loop
        started by the boot script or the input handler (e.g. mouseWatch, which
        keeps the sentence line up to date). Escoria's UI does their job."""
        starters = [k for k in (self.game.verb_script, ) if k] + \
            [k for k in self.game.scripts if k.endswith("::main")]
        found = set()
        for key in starters:
            script = self.game.scripts.get(key)
            room = key.split("::")[0]
            for st in self._all(script.body if script else []):
                vals = [v for _, v in st.expr]
                if st.kind != "expr" or len(vals) < 3 or vals[-1] != ")":
                    continue
                name = "".join(vals[:vals.index("(")]) if "(" in vals else ""
                target = name if "::" in name else f"{room}::{name}"
                body = self.game.scripts[target].body if target in self.game.scripts else []
                if any(b.kind == "loop" and [v for _, v in b.expr] == ["1"] for b in body):
                    found.add(target)
        return found

    def _all(self, stmts):
        for st in stmts:
            yield st
            yield from self._all(st.body)
            yield from self._all(st.orelse)
            for case in st.cases:
                yield from self._all(case.body)

    def ui_script(self, toks: list[Tok], ctx: Context) -> bool:
        name = "".join(v for _, v in toks)
        return (name if "::" in name else f"{ctx.room}::{name}") in self.ui_scripts

    def call_expr(self, name: str, args: list[list[Tok]], ctx: Context) -> str:
        if name == "isScriptRunning" and self.game.version < 6 and args:
            target = self.script_def("".join(v for _, v in args[0]), ctx)
            if target is not None and target.room == ctx.room and self.waits_in_loop(target.body) \
                    and not self.background_events.get(ctx.room, {}).get(f"scumm_bg_{snake(target.name)}"):
                return "1"      # a room's routine never ends: it's the one that would change things
        if name == "isScriptRunning" and args and self.ui_script(args[0], ctx):
            return "false"      # an interface script: Escoria's UI does its job
        if name == "getObjectState" and args:
            if ctx.generic and [v for _, v in args[0]] == ["objA"]:
                return "scumm_a_state"
            key = self.obj_key(args[0], ctx)
            if key:
                return self.object_state_var(key)
        if name == "getActorRoom" and args:
            actor = self.actor_name(args[0])
            if actor:
                return self.actor_room_var(actor)
        if name == "getObjectOwner" and args:
            key = self.obj_key(args[0], ctx)
            if key:
                return f'__owner("{self.inventory_ids.get(key, self.ids[key])}")'
        if name == "getObjectVerbEntrypoint" and len(args) == 2 and [v for _, v in args[0]] == ["objB"] and ctx.objB:
            if ctx.objB == "*":
                # Fallback for "any other" second object: Escoria only gets here
                # after looking for that object's own event.
                return "false"
            other = self.game.objects.get(ctx.objB)
            verb = args[1][0][1]
            return "true" if other and self.verb_cases(other.verbs, verb) is not None else "false"
        if name == "getObjectVerbEntrypoint" and len(args) == 2 and ctx.generic and \
                [v for _, v in args[0]] == ["objA"] and [v for _, v in args[1]] == ["InventoryObject"]:
            return 'scumm_inventory_a != ""'
        if name == "getObjectVerbEntrypoint" and len(args) == 2 and ctx.obj is not None:
            if [v for _, v in args[0]] == ["objA"]:
                verb = args[1][0][1]
                return "true" if self.verb_cases(ctx.obj.verbs, verb) is not None else "false"
        raise Untranslatable(name)

    @staticmethod
    def _match(toks: list[Tok], open_idx: int) -> int:
        depth = 0
        for j in range(open_idx, len(toks)):
            v = toks[j][1]
            if toks[j][0] == "punct" and v in "([":
                depth += 1
            elif toks[j][0] == "punct" and v in ")]":
                depth -= 1
                if depth == 0:
                    return j
        raise Untranslatable("unbalanced")

    @staticmethod
    def _split_args(toks: list[Tok]) -> list[list[Tok]]:
        args: list[list[Tok]] = [[]]
        depth = 0
        for t in toks:
            if t[0] == "punct" and t[1] in "([{":
                depth += 1
            elif t[0] == "punct" and t[1] in ")]}":
                depth -= 1
            elif t[0] == "punct" and t[1] == "," and depth == 0:
                args.append([])
                continue
            args[-1].append(t)
        return [a for a in args if a]

    # -------------------------------------------------------- simple statements

    def sound(self, name: str, room: Optional[str]):
        """A sound resource by name: `Room::x`, or `x` in this room (or anywhere, if unique)."""
        if "::" in name:
            return self.game.sounds.get(name)
        if room and f"{room}::{name}" in self.game.sounds:
            return self.game.sounds[f"{room}::{name}"]
        found = [s for s in self.game.sounds.values() if s.name == name]
        return found[0] if len(found) == 1 else None

    def say_lines(self, who: str, args: list[Tok], room: Optional[str] = None) -> list[str]:
        if not args or any(k != "string" for k, _ in args):
            raise Untranslatable("say")
        text = "".join(v for _, v in args)          # "a" "b" concatenates
        # %V{voice}: a recorded voice played with the line (res://speech/<key>.wav).
        voices = re.findall(r"%V\{([^}]*)\}", text)
        text = self.plain_text(re.sub(r"%V\{[^}]*\}", "", text))
        if "%" in text:
            raise Untranslatable("formatted text")
        key = None
        if voices:
            sound = self.sound(voices[0], room)
            if sound is None or not sound.voice:
                raise Untranslatable("voice " + voices[0])
            key = snake(sound.name)
        lines = []
        for part in text.split("\\w"):
            part = part.strip()
            if part:
                quoted = part.replace(chr(34), chr(92) + chr(34))
                lines.append(f'say("{who}", "{quoted}", "{key}")' if key and not lines else
                             f'say("{who}", "{quoted}")')
        return lines

    def set_state_lines(self, key: str, value: int) -> list[str]:
        oid = self.ids[key]
        obj = self.game.objects[key]
        if key in self.numbered_objects.values() and self.event_room and obj.room != self.event_room:
            # In another room, like the other side of a door: that room shows it on entering.
            return [f"{self.object_state_var(key)} = {value}"]
        lines = [f'set_state("{oid}", "state_{value}")', f"{self.object_state_var(key)} = {value}"]
        for child in self.game.objects.values():
            if child.room == obj.room and child.parent == obj.name and child.key in self.ids:
                active = "true" if child.parent_state == value else "false"
                lines.append(f'set_active("{self.ids[child.key]}", {active})')
        return lines

    def expr_statement(self, st: Stmt, ctx: Context, nxt: Optional[Stmt]) -> list[str]:
        toks = st.expr
        if not toks:
            return []
        if self.game.version < 6 and toks != (plain := self.literal_indexes(toks)):
            toks = plain
            following = self._following.get(id(st), [])
            st = dataclasses.replace(st, expr=toks)
            self._following[id(st)] = following
        vals = [v for _, v in toks]
        s = self.scale
        if self.game.version < 6 and not ctx.background and vals[1:3] == ["=", "isScriptRunning"]:
            event = self.script_event(self.script_def("".join(v for _, v in self._split_args(toks[4:-1])[0]), ctx), ctx)
            if event:
                self.room_vars.pop(self.var(vals[0], ctx), None)
                return [f'scumm_running("{self.var(vals[0], ctx)}", "{event}")']
        if self.game.version < 6 and not ctx.background and vals[0] == "stopScript" and len(toks) > 3 \
                and self.stop_script(self._split_args(toks[2:-1]), ctx):
            return self.stop_script(self._split_args(toks[2:-1]), ctx)
        if self.game.version < 6 and vals[0] in ("sysPrint", "printDefaults") or ctx.closeup and vals[0] == "egoSay":
            return self.print_statement(vals[0], self._split_args(toks[2:-1]), ctx)
        if ctx.background:
            special = self.background_statement(st, ctx)
            if special is not None:
                return special

        # Assignments: var = expr; var++; var--.
        if len(toks) >= 2 and toks[0][0] == "ident" and vals[1] in ("=", "++", "--") \
                and toks[0][1] not in self.game.actors:
            if vals[0] == "VAR_VERB_SCRIPT":
                target = self.script_def("".join(vals[2:]), ctx)
                key = f"{target.room}::{target.name}" if target else None
                if ctx.in_entry or key == self.game.verb_script or target is None:
                    return []   # the room's click area / back to normal input
                # A script takes over clicks (e.g. the end: any click quits).
                body = [b for b in target.body if b.kind != "return"]
                sub = Context(room=target.room, inline_depth=ctx.inline_depth + 1)
                return ["scumm_wait_click()", f"# {key}() on the next click"] + self.block(body, 0, sub)
            if vals[0].startswith("VAR_") or vals[0] == "sntcPrepo":
                raise Untranslatable(st.source)
            name = self.var(vals[0], ctx)
            if vals[1] == "++":
                return [f"{name} = {name} + 1"]
            if vals[1] == "--":
                return [f"{name} = {name} - 1"]
            value = self.simplify(self.expression(toks[2:], ctx))
            if self.game.version < 6 and re.fullmatch(r"\w+_room", value):
                self.room_vars[name] = value    # its comparisons read the room directly
                return []
            self.room_vars.pop(name, None)
            return [f"{name} = {value}"]
        if len(toks) >= 4 and vals[1] == "::" and vals[3] == "=":
            return [f"{self.var(vals[2])} = {self.simplify(self.expression(toks[4:], ctx))}"]

        # Calls: name(args) or Room::name(args).
        if vals[-1] != ")":
            raise Untranslatable(st.source)
        if len(vals) > 2 and vals[1] == "::":
            name = f"{vals[0]}::{vals[2]}"
            args = self._split_args(toks[4:-1])
        else:
            name = vals[0]
            args = self._split_args(toks[2:-1])
        if vals[1] != "(" and "::" not in name:
            raise Untranslatable(st.source)

        if name in IGNORED_CALLS or name == "printDefaults":
            return []
        if name == "egoSay":
            return self.say_lines("player", [t for a in args for t in a], ctx.room)
        if name == "actorSay" and len(args) >= 2:
            if [v for _, v in args[0]] == ["0xFF"]:
                return []
            who = self.actor_id(args[0])
            if who:
                return self.say_lines(who, [t for a in args[1:] for t in a], ctx.room)
        if name == "delay" and args and args[0][0][0] == "number":
            return [f"wait({round(int(args[0][0][1], 0) / 60, 2)})"]
        if name == "walkActorTo" and len(args) == 3:
            who = self.actor_id(args[0])
            x, y = (self.expression(a, ctx) for a in args[1:])
            if who and x.lstrip("-").isdigit() and y.lstrip("-").isdigit():
                cmd = "walk_to_pos_block" if self.waits_for(st, args[0]) else "walk_to_pos"
                return [f'{cmd}("{who}", {int(x) * s}, {int(y) * s})']
        if name == "walkActorToObj" and len(args) >= 2:
            # Walk to an object's walk-to point (or next to an actor) and face
            # it like the object says: Escoria's walk to an item does both.
            who = self.actor_id(args[0])
            target = self.ref_key(args[1], ctx) or self.actor_name(args[1])
            tid = self.ref_id(target) if target else None
            if who and tid:
                cmd = "walk_block" if self.waits_for(st, args[0]) else "walk"
                return [f'{cmd}("{who}", "{tid}")']
        if name == "setActorDirection" and args and ctx.current_actor:
            deg = self.expression(args[0], ctx)
            who = "player" if ctx.current_actor == self.game.ego else self.ids.get(ctx.current_actor)
            if who and deg.lstrip("-").isdigit():
                return [f'set_angle("{who}", {int(deg) % 360})']
        if name == "setActorElevation" and args and ctx.current_actor:
            px = self.constant(self.expression(args[0], ctx))
            who = "player" if ctx.current_actor == self.game.ego else self.ids.get(ctx.current_actor)
            if who and px is not None:
                return [f'scumm_elevation("{who}", {px})']
        if name == "putActorAt" and len(args) == 4:
            who = self.actor_id(args[0])
            actor = self.actor_name(args[0])
            room = [v for _, v in args[3]]
            if who and room == [KEEP_ROOM]:
                x, y = (self.constant(self.expression(a, ctx)) for a in args[1:3])
                if x is None or y is None:
                    raise Untranslatable(st.source)
                return [f'teleport_pos("{who}", {x * s}, {y * s})']
            if who and actor and len(room) == 1:
                lines = [f'{self.actor_room_var(actor)} = "{room[0]}"']
                if ctx.room and room == [ctx.room]:
                    x, y = (self.expression(a, ctx) for a in args[1:3])
                    lines += [f'set_active("{who}", true)',
                              f'teleport_pos("{who}", {int(x) * s}, {int(y) * s})']
                    # SCUMM plays the costume's init animation when an actor is put
                    # in a room (Carol walks in from behind the wall).
                    init = self.played_anim(actor, "init")
                    if init:
                        lines.append(f'anim("{who}", "{init}")')
                    return lines
                return lines + [f'set_active("{who}", false)']
        if name == "putActorInRoom" and len(args) == 2:
            who, actor = self.actor_id(args[0]), self.actor_name(args[0])
            room = [v for _, v in args[1]]
            if who and actor and len(room) == 1:
                return [f'{self.actor_room_var(actor)} = "{room[0]}"',
                        f'set_active("{who}", {"true" if room == [ctx.room] else "false"})']
        if name == "cameraFollowActor" and [v for _, v in args[0]] == ["VAR_EGO"]:
            return ['camera_set_target(0, "player")']
        if name == "actorFace" and len(args) == 2:
            a, b = self.actor_id(args[0]), self.actor_id(args[1])
            if a and b:
                return [f'turn_to("{a}", "{b}")']
        if name == "stopScript" and args and self.ui_script(args[0], ctx):
            return []
        if name == "drawObject" and len(args) == 2:
            # Draw one of the object's images (its state) without changing the state.
            key = self.obj_key(args[0], ctx)
            n = self.constant(self.expression(args[1], ctx))
            if key and n is not None:
                return [f'set_state("{self.ids[key]}", "state_{n}")']
        if name == "setObjectState" and len(args) == 2:
            key = self.obj_key(args[0], ctx)
            if key:
                arg = [v for _, v in args[1]]
                if len(arg) == 1 and args[1][0][0] == "number":
                    return self.set_state_lines(key, int(arg[0], 0))
                if arg[:2] == ["!", "getObjectState"] and self.obj_key(args[1][3:-1], ctx) == key:
                    var = self.object_state_var(key)
                    on = self.set_state_lines(key, 1)
                    off = self.set_state_lines(key, 0)
                    return [f"if {var}:"] + ["\t" + l for l in off] + ["else:"] + ["\t" + l for l in on]
        if name == "startSound" and len(args) == 1:
            sound = self.sound("".join(v for _, v in args[0]), ctx.room)
            if sound is not None and not sound.voice:
                return [f'scumm_sound("res://sounds/{snake(sound.name)}.wav")']
        if name == "setRoomRGBIntensity" and len(args) == 5:
            vals = [self.constant(self.expression(a, ctx)) for a in args]
            # Only the whole palette (0..255): a multiply over everything
            # SCUMM draws with it, the room, its actors and the verb bar.
            if None not in vals and vals[3] == 0 and vals[4] >= 255:
                return [f"scumm_room_intensity({vals[0]}, {vals[1]}, {vals[2]})"]
        if name == "setRoomPalette" and args and [v for _, v in args[0]] == ["0"]:
            return ["scumm_room_intensity(255, 255, 255)"]    # back to the room's palette
        if name == "setObjectName" and len(args) == 2 and args[1] and all(k == "string" for k, _ in args[1]):
            # setObjectName(obj, "x") / setObjectName(actorObject[actor], "x")
            vals = [v for _, v in args[0]]
            if vals[:2] == ["actorObject", "["] and vals[-1] == "]":
                ref = self.actor_name(args[0][2:-1])
                oid = self.ref_id(ref) if ref else None
            else:
                key = self.obj_key(args[0], ctx)
                oid = self.ids[key] if key else None
            if oid:
                text = "".join(v for _, v in args[1]).replace('"', '\\"')
                return [f'scumm_set_name("{oid}", "{text}")']
        if ctx.generic:
            generic = self.generic_call(name, args, ctx)
            if generic is not None:
                return generic
        if name == "pickupObject" and args:
            key = self.obj_key(args[0], ctx)
            if key in self.inventory_ids:
                # SCUMM moves it to state 1: often the place without the object.
                return [f'inventory_add("{self.inventory_ids[key]}")', *self.set_state_lines(key, 1),
                        f'set_interactive("{self.ids[key]}", false)']
            if key:
                return [f'inventory_add("{self.ids[key]}")']
        if name == "setObjectOwner" and len(args) == 2:
            key = self.obj_key(args[0], ctx)
            owner = [v for _, v in args[1]]
            if key and (owner == ["0"] or (self.actor_name(args[1]) not in (None, self.game.ego))):
                # Nobody / another actor. SCUMM doesn't mind if the ego didn't have
                # it; Escoria's inventory_remove does, so check first.
                return [f'if "{self.ids[key]}" in inventory:', f'\tinventory_remove("{self.ids[key]}")']
            if key and self.actor_name(args[1]) == self.game.ego:
                return [f'inventory_add("{self.ids[key]}")']
        if name == "setObjectClass" and len(args) == 2:
            key = self.obj_key(args[0], ctx)
            if key and "ClassUntouchable" in [v for _, v in args[1]]:
                untouchable = "0x80" in [v for _, v in args[1]]
                return [f'set_interactive("{self.ids[key]}", {"false" if untouchable else "true"})']
        if name == "startObject2" and len(args) >= 2 and ctx.obj is not None:
            verb = args[1][0][1]
            stmts = self.verb_cases(ctx.obj.verbs, verb)
            if verb == "InventoryObject" and stmts and stmts[-1].kind == "return":
                ctx.var_return = self.obj_key(stmts[-1].expr, ctx)
                if ctx.var_return:
                    return []
        if name == "doSentence" and len(args) == 4 and ctx.obj is not None and ctx.inline_depth < 4 \
                and [v for _, v in args[1]] == ["objA"]:
            # Another verb on this object: its own handler, or the game's default.
            verb = args[0][0][1]
            stmts = self.verb_cases(ctx.obj.verbs, verb)
            body = list(self.default_cases(verb) if stmts is None else stmts)
            while body and body[-1].kind in ("break", "return"):
                body.pop()
            return self.block(body, 0, dataclasses.replace(ctx, verb=verb, inline_depth=ctx.inline_depth + 1))
        if name == "shutdown":
            return ["scumm_quit()"]
        if name in ("userPutOff", "userPutOn"):
            # A click with input on interrupts the running event: a title screen never ends.
            return ['accept_input("SKIP")' if name == "userPutOff" else 'accept_input("ALL")']
        if name == "setCurrentActor" and args:
            ctx.current_actor = self.actor_name(args[0])
            return []
        if name == "setActorStanding":
            # Back to the stand animation. Its frames depend on where the actor
            # faces, unknown here: only done when the costume has one direction.
            actor = ctx.current_actor
            stand = self.played_anim(actor, "stand", single_direction=True) if actor else None
            if stand:
                who = "player" if actor == self.game.ego else self.ids.get(actor)
                return [f'anim("{who}", "{stand}")'] if who else []
            return []
        if name == "setActorInitFrame":
            return []       # the frame shown at initActor(), not a pose
        if name in ("setActorStandFrame", "setActorTalkFrame") and args \
                and ctx.current_actor and self.pose_resource:
            actor = ctx.current_actor
            pose = ctx.poses.setdefault(actor, ["stand", "talkStart"])
            anim = self.scumm_anim(actor, args[0])
            if anim is None:
                raise Untranslatable(st.source)
            if name == "setActorTalkFrame":
                pose[1] = anim
            else:
                pose[0] = anim
            res = self.pose_resource(actor, pose[0], pose[1])
            if res:
                return [f'set_animations("{self.ids[actor] if actor != self.game.ego else "player"}", "{res}")']
        if name == "animateActor" and len(args) == 2 and args[1][0][0] == "number" and self.game.version < 6:
            # In v5 the highest numbers aren't animations: 0x3F - n / 4 + 2 is a command.
            value = int(args[1][0][1], 0)
            command = 0x3F - value // 4 + 2
            who = self.actor_id(args[0])
            if command == STOP_MOVING:
                return []
            if command in (SET_DIRECTION, TURN_TO_DIRECTION) and who:
                return [f'scumm_face("{who}", {V5_DIRECTIONS[value % 4]})']
        if name == "animateActor" and len(args) == 2 and self.anim_name:
            who, actor = self.actor_id(args[0]), self.actor_name(args[0])
            anim = self.scumm_anim(actor, args[1]) if actor else None
            if who and anim and self.anim_name(actor, anim):
                # SCUMM picks the frames by where the actor faces *now*: the
                # generated scumm_anim command does that at runtime.
                return [f'scumm_anim("{who}", "{ANIM_KINDS.get(anim, snake(anim))}")']
        if name == "delay" and args and [v for _, v in args[0][:2]] == ["getRandomNumberRange", "("]:
            nums = [int(v, 0) for k, v in args[0] if k == "number"]
            if len(nums) == 2:
                return [f"wait({round(sum(nums) / 2 / 60, 2)})  # SCUMM: random {nums[0]}..{nums[1]} frames"]
        # Calls to other game scripts: setup ones were read already; simple ones are inlined.
        target = self.script_def(name, ctx)
        roam = self.roam_call(target, args, ctx) if target is not None else None
        if roam is not None:
            return roam
        if target is not None and target.flat and not ctx.flat and self.conversation_event_ok(target):
            event = self.conversation_id(target)
            if target.room != self.event_room and self.is_closeup(target) and target.room in self.scenes:
                return [f'{self.var("scummConversation")} = "{event}"', "scumm_fade_out()",
                        f'change_scene("{self.scenes[target.room]}")']
            if target.room == self.event_room:
                # It runs after this event, on its own: arguments go through globals.
                params = [f"{self.var(f'scummParam{n}')} = {self.expression(args[n], ctx) if n < len(args) else 0}"
                          for n in range(len(target.params))]
                return [*params, f'queue_event("{snake(target.room)}", "{event}")']
        if target is not None and self.game.version < 6 and target.room == self.event_room \
                and self.waits_in_loop(target.body):
            event = self.background_event(target) if target.room == self.ready_room else None
            event = event or self.routine_event(target)
            if event:
                return [f'queue_event("{snake(target.room)}", "{event}", "{event}")']
        if target is not None:
            # The dialog library is replaced by Escoria's dialogs. Only its scripts:
            # its room can hold the rest of the game's scripts too.
            if target.name in self.game.setup_scripts or \
                    (target.room in self.dialog_rooms and "dialog" in target.name.lower()):
                return []
            if target.params and ctx.inline_depth < 4 and self.inlinable(target.body) \
                    and target.name not in ("showVerbs", "defaultAction") \
                    and len(args) == len(target.params) and all(len(a) == 1 for a in args):
                # Plain arguments go straight into the body.
                values = {p: a[0] for p, a in zip(target.params, args)}
                body = [self.with_values(st, values) for st in target.body]
                while body and body[-1].kind == "return":
                    body.pop()
                sub = Context(room=target.room, current_actor=ctx.current_actor, poses=ctx.poses,
                              inline_depth=ctx.inline_depth + 1, locals=set(ctx.locals))
                return [f"# {target.room}::{target.name}()"] + self.block(body, 0, sub)
            if not target.params and ctx.inline_depth < 4 and self.inlinable(target.body):
                body = list(target.body)
                while body and body[-1].kind == "return":
                    body.pop()
                sub = Context(room=target.room, current_actor=ctx.current_actor, poses=ctx.poses,
                              inline_depth=ctx.inline_depth + 1)
                return [f"# {target.room}::{target.name}()"] + self.block(body, 0, sub)
        if name.split("::")[-1] == "showVerbs" and args and args[0][0][0] == "number":
            # The verb bar: SCUMM scripts turn it on and off.
            return [f"set_gui_visible({'true' if int(args[0][0][1], 0) else 'false'})"]
        if name == "startRoomWithEgo" and len(args) == 4 and args[1][0][1] in self.scenes:
            room = args[1][0][1]
            x, y = self.ego_entry(args)
            self.used_globals |= {"ego_entry_x", "ego_entry_y", "ego_walk_x", "ego_walk_y"}
            walk = [self.constant(self.expression(a, Context(room=room))) for a in args[2:4]]
            walk = [0, 0] if None in walk or walk[0] == NO_POSITION else [v * self.scale for v in walk]
            return [f'{self.actor_room_var(self.game.ego)} = "{room}"',
                    f"ego_entry_x = {x * self.scale}", f"ego_entry_y = {y * self.scale}",
                    f"ego_walk_x = {walk[0]}", f"ego_walk_y = {walk[1]}",
                    "scumm_fade_out()", f'change_scene("{self.scenes[room]}")']
        if name == "startRoom" and args:
            room = args[0][0][1]
            if self.game.version < 6 and room == ctx.room and self.event_room not in (None, room):
                # A script of another room takes the game there and goes on, like a
                # conversation: a new scene would end this event, so it stops here.
                self.todos += 1
                return [f"# TODO scumm: {st.source} and the rest of the script", "stop"]
            if room in self.scenes and room == ctx.room:
                # Escoria won't change to the room it's in. Entering again only means
                # running its entry: objects keep their state, as in SCUMM.
                rid = snake(room)
                return ["scumm_fade_out()", f'queue_event("{rid}", "setup")', f'queue_event("{rid}", "ready")']
            if room in self.scenes:
                # The old picture stays (and fades out) while the room changes.
                return ["scumm_fade_out()", f'change_scene("{self.scenes[room]}")']
        if name == "screenEffect" and args:
            value = int(args[0][0][1], 0) if len(args[0]) == 1 and args[0][0][0] == "number" else None
            if value:
                return [f"scumm_screen_effect({value & 0xFFFF})"]     # compiled: a signed word
        if name.split("::")[-1] == "defaultAction" and ctx.verb:
            stmts = list(self.default_cases(ctx.verb))
            while stmts and stmts[-1].kind in ("break", "return"):
                stmts.pop()
            return self.block(stmts, 0, ctx)
        raise Untranslatable(st.source)
