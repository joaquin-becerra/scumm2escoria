"""Cut a game down to some of its rooms and what they use.

What a room uses comes from following names through its code: objects, local
scripts and entry first, then every script, actor, costume and sound they
mention. A value only known at runtime can't be followed, so actors placed in
the chosen rooms are always kept. Doors to other rooms stay in the scripts and
simply lead nowhere.
"""
from __future__ import annotations

import difflib
from typing import Iterable, Iterator, Optional

from .ir import Case, Game, Room, Stmt


def clean(name: str) -> str:
    return name.lower().replace("-", "_")


def find_rooms(game: Game, wanted: Iterable[str]) -> list[str]:
    """Room names for what the user typed, in that order. Case and -/_ don't matter."""
    names = {clean(n): n for n, r in game.rooms.items() if r.background}
    found = []
    for text in wanted:
        key = clean(text)
        if key not in names:
            close = difflib.get_close_matches(key, names, n=3)
            hint = f"did you mean {', '.join(close)}?" if close else f"rooms: {', '.join(sorted(names))}"
            raise ValueError(f"no room called {text!r}: {hint}")
        if names[key] not in found:
            found.append(names[key])
    return found


def statements(stmts: list[Stmt]) -> Iterator[Stmt]:
    for st in stmts:
        yield st
        yield from statements(st.body)
        yield from statements(st.orelse)
        for case in st.cases:
            yield from statements(case.body)


def case_tokens(cases: list[Case]) -> Iterator[list]:
    for case in cases:
        yield from case.labels
        for st in statements(case.body):
            yield st.expr
            for inner in st.cases:
                yield from inner.labels


def tokens_of(stmts: list[Stmt]) -> Iterator[list]:
    for st in statements(stmts):
        yield st.expr
        for case in st.cases:
            yield from case.labels


def names_in(tokens: list, room: str) -> Iterator[str]:
    """Every name in some tokens: bare, and as seen from its room."""
    for k, (kind, value) in enumerate(tokens):
        if kind != "ident":
            continue
        yield value
        qualified = k + 2 < len(tokens) and tokens[k + 1][1] == "::"
        yield f"{value}::{tokens[k + 2][1]}" if qualified else f"{room}::{value}"


class Subset:
    def __init__(self, game: Game, rooms: list[str]):
        self.game, self.chosen = game, rooms
        storage = [n for n, r in game.rooms.items() if not r.background]
        self.rooms = set(rooms) | set(storage)
        self.bare_scripts = self.by_bare_name(game.scripts)
        self.bare_sounds = self.by_bare_name(game.sounds)
        self.scripts: set[str] = set()
        self.sounds: set[str] = set()
        self.actors: set[str] = set()
        self.costumes: set[str] = set()
        self.pending: list[tuple[list, str]] = []    # tokens and the room they're seen from

    @staticmethod
    def by_bare_name(table: dict) -> dict[str, str]:
        """Local scripts repeat their names between rooms: those need their room."""
        out: dict[str, str] = {}
        for key in table:
            bare = key.split("::")[1]
            if not bare.startswith("local_"):
                out[bare] = key
        return out

    def add_code(self, stmts: list[Stmt], room: str) -> None:
        self.pending += [(tokens, room) for tokens in tokens_of(stmts)]

    def add_cases(self, cases: list[Case], room: str) -> None:
        self.pending += [(tokens, room) for tokens in case_tokens(cases)]

    def add_script(self, key: str) -> None:
        if key not in self.scripts:
            self.scripts.add(key)
            script = self.game.scripts[key]
            self.add_code(script.body, script.room)

    def follow(self) -> None:
        g = self.game
        while self.pending:
            tokens, room = self.pending.pop()
            for name in names_in(tokens, room):
                key = name if name in g.scripts else self.bare_scripts.get(name)
                if key:
                    self.add_script(key)
                sound = name if name in g.sounds else self.bare_sounds.get(name)
                if sound:
                    self.sounds.add(sound)
                if name in g.actors:
                    self.actors.add(name)
                if name in g.costumes:
                    self.costumes.add(name)

    def collect(self) -> None:
        g = self.game
        for name in self.rooms:
            room = g.rooms[name]
            self.add_code(room.entry, name)
            self.add_code(room.click_script, name)
            self.actors |= set(room.actor_positions) | set(room.scripted_actors)
        for obj in g.objects.values():
            if obj.room in self.rooms:
                self.add_cases(obj.verbs, obj.room)
        for key, script in g.scripts.items():
            if script.room in self.rooms and script.name.startswith("local_"):
                self.add_script(key)
        for key in (g.cutscene_start, g.cutscene_end, g.verb_script):
            if key in g.scripts:
                self.add_script(key)
        self.add_cases(g.default_action, "")
        self.follow()
        self.actors |= {n for n, a in g.actors.items() if a.start_room in self.rooms}
        if g.ego:
            self.actors.add(g.ego)
        self.actors &= set(g.actors)
        self.costumes |= {g.actors[a].costume for a in self.actors if g.actors[a].costume}
        for name in self.rooms:
            self.costumes |= set(g.rooms[name].actor_costumes.values())

    def apply(self) -> None:
        g = self.game
        # The translator recognises setup scripts by name: it has read them already.
        setup = {k for k, s in g.scripts.items() if s.name in g.setup_scripts}
        g.rooms = {n: r for n, r in g.rooms.items() if n in self.rooms}
        g.objects = {k: o for k, o in g.objects.items() if o.room in self.rooms}
        g.scripts = {k: s for k, s in g.scripts.items() if k in self.scripts | setup}
        g.sounds = {k: s for k, s in g.sounds.items() if k in self.sounds}
        g.actors = {k: a for k, a in g.actors.items() if k in self.actors}
        g.costumes = {k: c for k, c in g.costumes.items() if k in self.costumes}
        g.actor_objects = {a: o for a, o in g.actor_objects.items() if a in g.actors and o in g.objects}
        g.start_inventory = [k for k in g.start_inventory if k in g.objects]
        for actor in g.actors.values():
            if actor.start_room not in g.rooms:
                actor.start_room = None
        for hook in ("cutscene_start", "cutscene_end", "verb_script"):
            if getattr(g, hook) not in g.scripts:
                setattr(g, hook, None)
        if g.roaming and g.roaming.script not in g.scripts:
            g.roaming = None


def first_script(game: Game, room: str) -> Optional[str]:
    """The lowest local script of a room that can run without parameters."""
    scripts = [s for s in game.scripts.values() if s.room == room and s.name.startswith("local_")
               and not s.params]
    return min(scripts, key=lambda s: int(s.name.split("_")[1])).name if scripts else None


def start_in(game: Game, name: str) -> None:
    """Start the game in a room: its entry script, or else its first script."""
    game.start_room = name
    if game.ego:
        game.actors[game.ego].start_room = name
    room: Room = game.rooms[name]
    script = None if room.entry else first_script(game, name)
    if script:
        tokens = [("ident", name), ("punct", "::"), ("ident", script), ("punct", "("), ("punct", ")")]
        room.entry = [Stmt("expr", expr=tokens, source=f"{name}::{script}();")]


def keep_rooms(game: Game, wanted: Iterable[str]) -> Subset:
    """Leave in the game only some rooms, starting in the first one."""
    rooms = find_rooms(game, wanted)
    subset = Subset(game, rooms)
    subset.collect()
    subset.apply()
    start_in(game, rooms[0])
    return subset
