"""Intermediate representation: engine-neutral description of a SCUMM game.

Frontends (ScummC sources today, compiled SCUMM resources maybe later) build
these objects; backends (Escoria) consume them. All coordinates are in
original SCUMM pixels; scaling happens in the backend.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

# SCUMM directions, in the order costumes usually declare them.
DIRECTIONS = ("E", "W", "S", "N")


@dataclass
class Box:
    """A SCUMM walk box: a quad given as ul, ur, lr, ll corners."""
    index: int
    corners: list[tuple[int, int]]
    mask: int          # z-plane that clips actors standing in this box
    flags: int
    scale: int         # 255 == 100%

    @property
    def min_x(self) -> int:
        return min(x for x, _ in self.corners)

    @property
    def max_x(self) -> int:
        return max(x for x, _ in self.corners)

    @property
    def min_y(self) -> int:
        return min(y for _, y in self.corners)

    @property
    def max_y(self) -> int:
        return max(y for _, y in self.corners)


@dataclass
class Room:
    name: str
    background: Optional[str] = None            # absolute path to image
    zplanes: list[str] = field(default_factory=list)   # absolute paths, plane 1..n
    boxes: list[Box] = field(default_factory=list)
    # Where actors are when the room is entered: {actor name: (x, y)}.
    actor_positions: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Actors that scripts bring in later: {actor name: first (x, y) used}.
    scripted_actors: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Actor numbers are slots: a room can dress one in another costume than usual.
    actor_costumes: dict[str, str] = field(default_factory=dict)
    entry: list["Stmt"] = field(default_factory=list)        # entry script
    click_script: list["Stmt"] = field(default_factory=list) # VAR_VERB_SCRIPT set by entry
    source: Optional[str] = None


@dataclass
class Picture:
    name: str
    path: str                  # absolute path to image
    position: tuple[int, int]  # offset of the image's top-left from the actor's feet


@dataclass
class AnimStep:
    """What one limb does in one direction of an animation."""
    limb: str
    frames: list[str] = field(default_factory=list)  # picture names
    command: Optional[str] = None                     # HIDE / SHOW / None
    loop: bool = True


@dataclass
class Anim:
    name: str
    index: Optional[int] = None                       # the "@ N" slot, if any
    directions: dict[str, list[AnimStep]] = field(default_factory=dict)

    def frames_for(self, direction: str) -> tuple[list[str], bool]:
        """Frames of the first limb that actually shows pictures."""
        for step in self.directions.get(direction, []):
            if step.frames:
                return step.frames, step.loop
        return [], True


@dataclass
class Costume:
    name: str
    flip: bool = False                 # W is drawn mirrored
    palette: Optional[str] = None      # image whose palette the costume uses
    pictures: dict[str, Picture] = field(default_factory=dict)
    limbs: list[str] = field(default_factory=list)
    anims: dict[str, Anim] = field(default_factory=dict)
    source: Optional[str] = None


@dataclass
class Actor:
    name: str                          # script identifier, e.g. commanderZif
    costume: Optional[str] = None      # costume name (file stem)
    display_name: Optional[str] = None
    talk_color: Optional[int] = None   # palette index
    anim_speed: Optional[int] = None   # ticks per animation frame
    walk_speed: Optional[tuple[int, int]] = None   # (x, y) pixels per step
    elevation: int = 0                 # drawn this many pixels above its position
    ignore_boxes: bool = False
    untouchable: bool = False
    classes: list[str] = field(default_factory=list)
    start_room: Optional[str] = None   # where game setup puts the actor (None: nowhere yet)
    width: Optional[int] = None        # setActorWidth (SCUMM v6 default: 40)
    talk_pos: Optional[tuple[int, int]] = None     # setActorTalkPos: text anchor from the feet


@dataclass
class Sound:
    """A sound or voice resource declared in a room."""
    room: str
    name: str
    source: str           # the .voc file
    voice: bool = False   # `voice` (played with a line of text, %V{name})


@dataclass
class RoamLib:
    """A game's actor-roaming library (see frontend/scummc_roam.py)."""
    script: str                         # the background loop, "Room::name"
    array: str                          # per-actor array: state and rectangle
    state_field: str
    fields: list[str] = field(default_factory=list)    # x min, x max, y min, y max
    stopped: Optional[int] = None
    paused: Optional[int] = None
    delay: Optional[tuple[int, int]] = None             # jiffies (1/60 s) between turns
    chance: float = 1.0                 # of walking somewhere each turn
    choices: dict[str, int] = field(default_factory=dict)
    walk_cases: dict[str, int] = field(default_factory=dict)
    start: Optional[str] = None         # control scripts, "Room::name"
    start_params: list[int] = field(default_factory=list)  # param index of each field
    pause: Optional[str] = None
    resume: Optional[str] = None
    stop: Optional[str] = None


@dataclass
class Game:
    name: str
    version: int = 6                   # of the SCUMM engine
    room_numbers: dict[int, str] = field(default_factory=dict)    # compiled games
    rooms: dict[str, Room] = field(default_factory=dict)
    costumes: dict[str, Costume] = field(default_factory=dict)
    actors: dict[str, Actor] = field(default_factory=dict)
    ego: Optional[str] = None          # actor name
    start_room: Optional[str] = None
    verbs: list["Verb"] = field(default_factory=list)
    objects: dict[str, "Obj"] = field(default_factory=dict)       # key: Room::name
    globals: list[str] = field(default_factory=list)              # bit/int variables
    actor_objects: dict[str, str] = field(default_factory=dict)   # actor -> object key
    start_inventory: list[str] = field(default_factory=list)      # object keys
    default_action: list["Case"] = field(default_factory=list)    # switch(vrb) cases
    charsets: dict[str, str] = field(default_factory=dict)        # name -> font image
    text_charset: Optional[str] = None      # speech / overhead text
    dialog_charset: Optional[str] = None    # dialog choices
    gui_charset: Optional[str] = None       # charset @ 1, used by the engine GUI
    cursor: Optional[dict] = None           # {"image", "hotspot", "transparent"}
    palette: Optional[str] = None           # image holding the game palette
    timer_next: Optional[int] = None        # VAR_TIMER_NEXT: 60 Hz ticks per game frame
    char_inc: Optional[int] = None          # VAR_CHARINC: game frames each character of speech stays
    scripts: dict[str, "ScriptDef"] = field(default_factory=dict)   # "Room::name"
    setup_scripts: list[str] = field(default_factory=list)  # names the frontend already evaluated
    target_verbs: list[str] = field(default_factory=list)   # verbs that may take a 2nd object (Use, Give)
    inventory_verb: Optional[str] = None    # verb used on inventory items when none is selected
    defines: dict[str, int] = field(default_factory=dict)  # #define NAME number
    dialog_ui: Optional[dict] = None        # dialog lines layout: x, y, step, max, arrows
    # Engine hooks set by the boot script: VAR_CUTSCENE_START/END_SCRIPT and
    # VAR_VERB_SCRIPT (the normal input handler), as "Room::script".
    roaming: Optional[RoamLib] = None
    sounds: dict[str, Sound] = field(default_factory=dict)   # "Room::name"
    docs: list[str] = field(default_factory=list)   # readme/license files shipped with the source
    boot_effect: int = 0                    # screenEffect() before the start room
    cutscene_start: Optional[str] = None
    cutscene_end: Optional[str] = None
    verb_script: Optional[str] = None

    def costume_of(self, actor: str, room: Optional[str] = None) -> Optional[str]:
        """The costume an actor wears in a room."""
        here = self.rooms[room].actor_costumes if room in self.rooms else {}
        return here.get(actor) or self.actors[actor].costume

    @property
    def frames_per_second(self) -> Optional[float]:
        return 60 / self.timer_next if self.timer_next else None

    @property
    def ego_costume(self) -> Optional[str]:
        actor = self.actors.get(self.ego) if self.ego else None
        return actor.costume if actor else None

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Scripts: a small engine-neutral AST. Expressions are kept as token lists
# ([kind, value] pairs) and interpreted by each backend.
# --------------------------------------------------------------------------

Tok = tuple[str, str]


@dataclass
class Stmt:
    kind: str                       # see the constructors below
    line: int = 0
    expr: list[Tok] = field(default_factory=list)       # condition / expression
    body: list["Stmt"] = field(default_factory=list)    # then / loop / cutscene body
    orelse: list["Stmt"] = field(default_factory=list)  # else / override body
    cases: list["Case"] = field(default_factory=list)   # switch
    name: str = ""                  # label name, loop kind, declared type
    negate: bool = False            # `unless` / `until`
    source: str = ""                # original text, for TODO comments


@dataclass
class Case:
    labels: list[list[Tok]]         # empty list == default
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Verb:
    name: str                       # identifier, e.g. LookAt
    label: Optional[str] = None     # text on the button
    key: Optional[str] = None
    pos: Optional[tuple[int, int]] = None   # on the verb bar; None == not a button
    centered: bool = False          # pos is the center of the text, not its left edge
    color: Optional[int] = None     # palette indices
    hi_color: Optional[int] = None
    dim_color: Optional[int] = None
    back_color: Optional[int] = None
    image: Optional[str] = None     # object key, for image verbs (e.g. the bar background)
    charset: Optional[str] = None
    on: bool = False

    @property
    def is_button(self) -> bool:
        # Some verbs have no key; labels of arrows and of the sentence line aren't words.
        return bool(self.pos and self.label and (self.key or self.label[0].isalpha()))


@dataclass
class ObjState:
    hs_x: int                       # walk-to point, relative to the object
    hs_y: int
    image: Optional[str] = None     # absolute path
    zplanes: list[Optional[str]] = field(default_factory=list)  # per plane 1..n


@dataclass
class Obj:
    name: str
    room: str
    display_name: Optional[str] = None
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    hs_x: int = 0
    hs_y: int = 0
    dir: Optional[str] = None       # EAST / WEST / NORTH / SOUTH
    classes: list[str] = field(default_factory=list)
    states: list[ObjState] = field(default_factory=list)   # state N == states[N-1]
    state: int = 0
    parent: Optional[str] = None
    parent_state: Optional[int] = None
    trans: Optional[int] = None     # palette index drawn transparent (`trans = n;`)
    verbs: list[Case] = field(default_factory=list)        # verb(...) body
    locals: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.room}::{self.name}"


@dataclass
class FlatBlock:
    """Straight code and where it goes next, for code that structured loops can't hold."""
    offset: int
    body: list[Stmt] = field(default_factory=list)
    cond: list[Tok] = field(default_factory=list)   # empty: always goes to `next`
    target: Optional[int] = None    # where to go if `cond`, negated if `negate`
    negate: bool = False
    next: Optional[int] = None      # None: the script ends


@dataclass
class ScriptDef:
    room: str
    name: str
    params: list[str] = field(default_factory=list)
    body: list[Stmt] = field(default_factory=list)
    # A conversation: it jumps all over, between the menu and the answers.
    flat: list[FlatBlock] = field(default_factory=list)
    # The same for a room's scripts: what runs alongside the game, like an actor walking back and forth.
    routine: list[FlatBlock] = field(default_factory=list)
