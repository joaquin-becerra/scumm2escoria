"""Backend that writes a Godot 4 + Escoria project from the IR."""
from __future__ import annotations

import os
import re
import shutil
from collections import deque
from dataclasses import replace

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from ..ir import Actor, Box, Costume, Game, Room
from .ashes import statements
from .escoria_objects import ObjectsMixin
from .escoria_ui import UI_ADDON, UiMixin
from .escoria_transitions import TransitionsMixin
from .sounds import voc_to_wav
from ..ir import Stmt
from .godot import Raw, Resource, packed_vec2, rect2, vec2

SCALE = 4                  # SCUMM 320x200 -> 1280x800
VIEWPORT = (320 * SCALE, 200 * SCALE)
# Defaults when the game doesn't say (from VAR_TIMER_NEXT and the actor setup).
FRAMES_PER_SECOND = 12.0  # SCUMM game frames per second
ANIM_FRAMES = 4           # game frames per animation frame (setActorAnimSpeed)
FULL_SCREEN = 200         # a room this tall covers the verb bar
LINE_WIDTH = 2            # half the width of a walk box that is a line
WALK_STEP = 2             # pixels per game frame (setActorWalkSpeed x)
TALK_POS = (0, -80)       # SCUMM v6 default actor talk position, from the feet
ACTOR_WIDTH = 40          # SCUMM v6 default setActorWidth
CHAR_INC = 4              # SCUMM v6 default VAR_CHARINC
FAST_MACHINE = 2          # VAR_MACHINE_SPEED of a v5 game on a fast computer
Z_MAX = 4000               # "always in front of actors"

ADDONS = ("escoria-core", "escoria-dialog-simple", "escoria-ui-keyboard-9verbs")

CORE = "res://addons/escoria-core/game/core-scripts"
ESC_ROOM = f"{CORE}/esc_room.gd"
ESC_TERRAIN = f"{CORE}/esc_terrain.gd"
ESC_LOCATION = f"{CORE}/esc_location.gd"
ESC_PLAYER = f"{CORE}/esc_player.gd"
ESC_ITEM = f"{CORE}/esc_item.gd"
ESC_DIALOG_LOCATION = f"{CORE}/esc_dialog_location.gd"
ESC_INTERACTION_LOCATION = f"{CORE}/esc_interaction_location.gd"
ESC_ANIM_RES = f"{CORE}/resources/esc_animationresource.gd"
ESC_DIR_ANGLE = f"{CORE}/resources/esc_directionangle.gd"
ESC_ANIM_NAME = f"{CORE}/resources/esc_animationname.gd"

# Escoria direction slots: name, SCUMM direction, angle start, angle size.
ESC_DIRECTIONS = (
    ("up", "N", 315, 90),
    ("right", "E", 45, 90),
    ("down", "S", 135, 90),
    ("left", "W", 225, 90),
)


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


class EscoriaWriter(ObjectsMixin, UiMixin, TransitionsMixin):
    SCALE = SCALE
    ESC_ITEM = ESC_ITEM
    ESC_LOCATION = ESC_LOCATION

    def __init__(self, game: Game, out_dir: str, escoria_src: str, log=print):
        self.game = game
        self.out = os.path.abspath(out_dir)
        self.escoria_src = os.path.abspath(escoria_src)
        self.log = log
        self.player_scene: str | None = None
        self.actor_scenes: dict[str, str] = {}   # actor name -> res:// scene
        self.costume_scenes: dict[tuple, str] = {}   # (actor, costume) -> res:// scene
        self.pose_variants: dict[str, tuple] = {}  # res:// path -> (costume, stand, talk)
        self._linked: set[str] | None = None
        self.actor_size = (27, 45)   # updated from the ego costume
        self._warned: set = set()
        # what js_ports/escoria_patches.js needs to know of this game (js_ports/config.js)
        self.js_config: dict = {}

    # ------------------------------------------------------------------ utils

    def path(self, res_path: str) -> str:
        """res://foo -> absolute path in the output project (dirs created)."""
        p = os.path.join(self.out, res_path.removeprefix("res://"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def write_text(self, res_path: str, text: str) -> None:
        with open(self.path(res_path), "w", encoding="utf-8") as f:
            f.write(text)

    def save_png(self, img: Image.Image, res_path: str) -> str:
        img.resize((img.width * SCALE, img.height * SCALE), Image.NEAREST).save(self.path(res_path))
        return res_path

    # ---------------------------------------------------------------- project

    def write(self) -> None:
        os.makedirs(self.out, exist_ok=True)
        # Generated content is rebuilt from scratch on every run.
        for d in ("characters", "rooms", "items", "sounds", "speech", "original"):
            shutil.rmtree(os.path.join(self.out, d), ignore_errors=True)
        self.copy_addons()
        self.patch_ui_addon()
        if self.game.version < 6:
            self.patch_exit_analyzer()
            self.patch_background_events()
        self.write_fonts()
        # Rooms with walk boxes are playable; the others (title, cutscenes) are shown as is.
        rooms = [r for r in self.game.rooms.values() if r.background]
        self.assign_ids(rooms)
        self.write_project_settings()
        self.write_export_presets()
        # The ego first: its size is used to place z-plane strips.
        ego = self.game.actors.get(self.game.ego) if self.game.ego else None
        if ego and ego.costume in self.game.costumes:
            self.player_scene = self.write_actor(ego, is_player=True)
        for room in rooms:
            for name in list(room.actor_positions) + list(room.scripted_actors):
                actor = self.game.actors[name]
                costume = self.game.costume_of(name, room.name)
                if name != self.game.ego and (name, costume) not in self.costume_scenes \
                        and costume in self.game.costumes:
                    folder = self.actor_folder(name, costume)
                    scene = self.write_actor(replace(actor, costume=costume), False, folder)
                    self.costume_scenes[name, costume] = scene
                    self.actor_scenes.setdefault(name, scene)
        for room in rooms:
            self.write_room(room)
        self.write_inventory()
        self.write_action_defaults()
        self.write_ui()
        self.write_speech()
        self.write_dialog_chooser()
        self.write_pose_variants()
        self.write_background_commands()
        if self.game.version < 6:
            self.write_print_commands()
        self.write_start_script()
        self.write_commands()
        self.write_flow_commands()
        self.write_transitions()
        self.write_name_commands()
        self.write_palette_commands()
        self.write_roaming()
        self.write_sounds()
        self.write_defaults_support()
        self.write_validator()
        self.write_js_ports()
        # Count what is left in the files (scripts translated more than once,
        # e.g. for deduplicated events, would count twice otherwise).
        todos = sum(open(os.path.join(d, f), encoding="utf-8").read().count("# TODO scumm:")
                    for d, _, files in os.walk(self.out) if "/addons" not in d
                    for f in files if f.endswith(".esc"))
        self.log(f"scripts: {todos} TODO comments left for manual work")
        self.write_readme(todos)

    def write_readme(self, todos: int) -> None:
        """README.md for the generated project, plus the source's credits in original/."""
        docs = []
        for src in self.game.docs:
            name = os.path.basename(src)
            # The source and its parent can both have a LICENSE.
            while name in docs:
                name = "parent_" + name
            shutil.copy(src, self.path(f"res://original/{name}"))
            docs.append(name)
        credits = ""
        if docs:
            credits = ("\n- The game's story, graphics, sounds and texts belong to the authors of "
                       "the original game. Its readme and license files are copied to "
                       "`original/`: " + ", ".join(f"`{d}`" for d in docs) + ".")
        todo_note = ("every script was translated" if todos == 0 else
                     f"{todos} `# TODO scumm:` comments were left in the `.esc` scripts, "
                     "each with the original code that couldn't be translated")
        self.write_text("res://README.md", f"""# {self.game.name} (Godot + Escoria)

A [Godot](https://godotengine.org/) 4 +
[Escoria](https://github.com/godot-escoria/escoria-demo-game) version of
"{self.game.name}", generated automatically by **scumm2escoria** from the game's
ScummC source.

Every file in this project comes from the converter (plus the files Godot creates
when it imports the project). Running the converter again regenerates it, so
manual changes will be lost.

## Requirements

- [Godot](https://godotengine.org/) 4.7.x

## Running

Open Godot's Project Manager, choose **Import**, select this folder's
`project.godot` and press **Run** (F5). From the command line:

```bash
godot --path .
```

Play with the mouse: click a verb in the verb bar, then an object or character.
**Esc** opens the Escoria menu (new game, save, load, options, quit).

## Web version

The project includes a "Web" export preset. It is single-threaded, so it works on
any web server, without special headers.

```bash
godot --headless --path . --export-release "Web" build/web/index.html
cd build/web && python3 -m http.server 8000 --bind 127.0.0.1
# open http://localhost:8000
```

It doesn't work by opening `index.html` directly: browsers block loading the
engine from `file://`.

## Contents

| Path | Contents |
|---|---|
| `rooms/` | one folder per room: scene, background, z-planes, objects and scripts |
| `characters/` | the actors: scenes, animation frames and scripts |
| `items/inventory/` | inventory objects |
| `ui/` | verb bar, sentence line, inventory and dialog choices, built from the game's own interface |
| `fonts/` | the game's charsets as bitmap fonts |
| `commands/` | extra ASHES commands for SCUMM behaviour Escoria doesn't have (`scumm_*`) |
| `sounds/`, `speech/` | sound effects and voices |
| `start_game.esc` | game start: global variables and the first room |
| `action_defaults.esc` | the game's default responses for each verb |
| `tools/` | `validate_esc.tscn`, a scene that checks every `.esc` script compiles |
| `addons/` | Escoria, copied from the Escoria demo game |

## Notes

- Conversion status: {todo_note}.
- Some components come from the Escoria demo game rather than from the original
  game: the Escoria menus (main menu, pause menu, options, save/load), `icon.png`,
  `default_env.tres` and the base project settings (input map, autoloads).{credits}
""")

    def write_commands(self) -> None:
        """ASHES commands for SCUMM behaviour Escoria lacks (res://commands)."""
        self.write_text("res://commands/scumm_anim.gd", """class_name ScummAnimCommand
extends ESCBaseCommand
# Generated by scumm2escoria.
# `scumm_anim(object, animation)`: play a costume animation the way SCUMM's
# animateActor does, with the frames for where the actor faces right now
# (<animation>_up/right/down/left). Costumes that mirror the left side only
# have _right: the sprite is already flipped while the actor faces left.

const DIRECTIONS := ["up", "right", "down", "left"]   # order of the animation resource


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(2, [TYPE_STRING, TYPE_STRING], [null, null])


func validate(arguments: Array):
\tif not super.validate(arguments):
\t\treturn false
\tif not escoria.object_manager.has(arguments[0]):
\t\traise_invalid_object_error(self, arguments[0])
\t\treturn false
\treturn true


func run(command_params: Array) -> int:
\tvar node: ESCItem = escoria.object_manager.get_object(command_params[0]).node
\tvar animator: ESCAnimationPlayer = node.get_animation_player()
\tvar dir := 2
\tif node._movable:
\t\tdir = clampi(node._movable.last_dir, 0, DIRECTIONS.size() - 1)
\tvar candidates := [DIRECTIONS[dir]]
\tif DIRECTIONS[dir] == "left":
\t\tcandidates.append("right")
\tcandidates.append_array(["down", "right", "up", "left"])
\tfor d in candidates:
\t\tvar anim_name: String = "%s_%s" % [command_params[1], d]
\t\tif animator.has_animation(anim_name):
\t\t\tanimator.play(anim_name)
\t\t\treturn ESCExecution.RC_OK
\tescoria.logger.warn(self, "No animation %s for %s" % [command_params[1], command_params[0]])
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")
        self.write_text("res://commands/scumm_elevation.gd", """class_name ScummElevationCommand
extends ESCBaseCommand
# Generated by scumm2escoria.
# `scumm_elevation(object, pixels)`: SCUMM's setActorElevation, the actor is
# drawn `pixels` (SCUMM pixels) above its position; depth still uses the feet.

const SCALE := """ + str(SCALE) + """


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(2, [TYPE_STRING, TYPE_INT], [null, null])


func validate(arguments: Array):
\tif not super.validate(arguments):
\t\treturn false
\tif not escoria.object_manager.has(arguments[0]):
\t\traise_invalid_object_error(self, arguments[0])
\t\treturn false
\treturn true


func run(command_params: Array) -> int:
\tvar node: ESCItem = escoria.object_manager.get_object(command_params[0]).node
\tvar sprite = node.get_node_or_null("sprite")
\tif sprite and node.custom_data.has("scumm_base_offset_y"):
\t\tsprite.offset.y = node.custom_data["scumm_base_offset_y"] - command_params[1] * SCALE
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")

    def write_flow_commands(self) -> None:
        self.write_text("res://commands/scumm_wait_click.gd", """class_name ScummWaitClickCommand
extends ESCBaseCommand
# Generated by scumm2escoria.
# `scumm_wait_click()`: block until the player clicks. SCUMM scripts hand the
# next click to another script (VAR_VERB_SCRIPT = x), e.g. "click to quit".


class _Waiter extends Node:
\tsignal clicked

\tfunc _input(event: InputEvent) -> void:
\t\tif event is InputEventMouseButton and event.pressed:
\t\t\tget_viewport().set_input_as_handled()
\t\t\tclicked.emit()


var _waiter: _Waiter


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(0, [], [])


func run(_command_params: Array) -> int:
\t_waiter = _Waiter.new()
\tescoria.add_child(_waiter)
\tawait _waiter.clicked
\t_waiter.queue_free()
\t_waiter = null
\treturn ESCExecution.RC_OK


func interrupt():
\tif is_instance_valid(_waiter):
\t\t_waiter.clicked.emit()
""")
        self.write_text("res://commands/scumm_quit.gd", """class_name ScummQuitCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_quit()`: SCUMM's shutdown(), end the game.


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(0, [], [])


func run(_command_params: Array) -> int:
\tescoria.quit()
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")

    def write_name_commands(self) -> None:
        self.write_text("res://commands/scumm_set_name.gd", """class_name ScummSetNameCommand
extends ESCBaseCommand
# Generated by scumm2escoria.
# `scumm_set_name(object, name)`: SCUMM's setObjectName, the name the sentence
# line shows. Kept in a global (scumm_name_<object>) so it survives room
# changes and saved games; scumm_restore_names() applies it again.

const PREFIX := "scumm_name_"


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(2, [TYPE_STRING, TYPE_STRING], [null, null])


func run(command_params: Array) -> int:
\tescoria.globals_manager.set_global(PREFIX + command_params[0], command_params[1], true)
\tapply(command_params[0], command_params[1])
\treturn ESCExecution.RC_OK


static func apply(global_id: String, name: String) -> void:
\tif escoria.object_manager.has(global_id):
\t\tvar obj = escoria.object_manager.get_object(global_id)
\t\tif obj and is_instance_valid(obj.node) and "tooltip_name" in obj.node:
\t\t\tobj.node.tooltip_name = name


func interrupt():
\tpass
""")
        self.write_text("res://commands/scumm_restore_names.gd", """class_name ScummRestoreNamesCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_restore_names()`: in a room's :setup,
# give its objects the names scumm_set_name() changed.


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(0, [], [])


func run(_command_params: Array) -> int:
\tvar globals: Dictionary = escoria.globals_manager.get_globals()
\tfor key in globals:
\t\tif str(key).begins_with(ScummSetNameCommand.PREFIX):
\t\t\tScummSetNameCommand.apply(str(key).substr(ScummSetNameCommand.PREFIX.length()), str(globals[key]))
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")

    def write_palette_commands(self) -> None:
        self.write_text("res://commands/scumm_room_intensity.gd", """class_name ScummRoomIntensityCommand
extends ESCBaseCommand
# Generated by scumm2escoria.
# `scumm_room_intensity(red, green, blue)`: SCUMM's setRoomRGBIntensity over the
# whole palette, each channel scaled by n/255 (255, 255, 255: setRoomPalette(0),
# the room's own colors). Everything on screen dims, verb bar included, like
# SCUMM's single palette. The layer belongs to the room: entering another room
# reloads its palette, as in SCUMM (entry scripts dim it again if needed).

const LAYER_NAME := "ScummPalette"
const SHADER := \"\"\"
shader_type canvas_item;
render_mode blend_mul;
uniform vec3 scale = vec3(1.0);
void fragment() {
\tCOLOR = vec4(scale, 1.0);
}
\"\"\"


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(3, [TYPE_INT, TYPE_INT, TYPE_INT], [null, null, null])


func run(command_params: Array) -> int:
\tvar room: Node = escoria.main.current_scene
\tvar layer: CanvasLayer = room.get_node_or_null(LAYER_NAME)
\tvar scale := Vector3(command_params[0], command_params[1], command_params[2]) / 255.0
\tif scale.x >= 1.0 and scale.y >= 1.0 and scale.z >= 1.0:
\t\tif layer:
\t\t\tlayer.queue_free()
\t\treturn ESCExecution.RC_OK
\tif layer == null:
\t\tlayer = CanvasLayer.new()
\t\tlayer.name = LAYER_NAME
\t\tlayer.layer = 99     # over the room and the UI, under the room transitions
\t\tvar rect := ColorRect.new()
\t\trect.set_anchors_preset(Control.PRESET_FULL_RECT)
\t\trect.mouse_filter = Control.MOUSE_FILTER_IGNORE
\t\tvar shader := Shader.new()
\t\tshader.code = SHADER
\t\trect.material = ShaderMaterial.new()
\t\trect.material.shader = shader
\t\tlayer.add_child(rect)
\t\troom.add_child(layer)
\tlayer.get_child(0).material.set_shader_parameter("scale", scale)
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")

    def write_sounds(self) -> None:
        """Sounds to res://sounds, voices to res://speech (Escoria's speech folder)."""
        for sound in self.game.sounds.values():
            folder = "speech" if sound.voice else "sounds"
            voc_to_wav(sound.source, self.path(f"res://{folder}/{snake(sound.name)}.wav"))
        self.write_text("res://commands/scumm_sound.gd", """class_name ScummSoundCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_sound(file)`: SCUMM's startSound. Escoria's
# play_snd has a single effects channel (a new sound cuts the last one); SCUMM
# mixes them (a shot and the fall that starts while it still sounds), so each
# sound gets its own player, on Escoria's effects bus.


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(1, [TYPE_STRING], [null])


func run(command_params: Array) -> int:
\tvar player := AudioStreamPlayer.new()
\tplayer.stream = load(command_params[0])
\tplayer.bus = "SFX"
\tescoria.add_child(player)
\tplayer.finished.connect(player.queue_free)
\tplayer.play()
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")

    def write_js_ports(self) -> None:
        """For the JS engine (godot-escoria-js-engine), which translates the
        generated GDScript itself: the JS side of the converter's patches to
        Escoria's addons (backend/js_ports/escoria_patches.js, the same for
        every game) plus config.js, which says what was patched for this game."""
        import json
        if self.game.version < 6:
            self.js_config.setdefault("patches", {})["backgroundChannels"] = True
        src = os.path.join(os.path.dirname(__file__), "js_ports")
        dst = os.path.join(self.out, "js_ports")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        with open(os.path.join(dst, "config.js"), "w", encoding="utf-8") as f:
            f.write("// Generated by scumm2escoria: what it patched in Escoria's addons for\n"
                    "// this game, read by escoria_patches.js.\n"
                    "export default " + json.dumps(self.js_config, indent=1, ensure_ascii=False) + ";\n")

    def write_roaming(self) -> None:
        """The game's roaming library (frontend/scummc_roam.py) as a runtime node."""
        lib = self.game.roaming
        if lib is None:
            return
        lo, hi = lib.delay
        self.write_text("res://ui/scumm_roaming.gd", ROAMING_SCRIPT
                        .replace("%SOURCE%", lib.script)
                        .replace("%DELAY_MIN%", str(round(lo / 60, 3)))
                        .replace("%DELAY_MAX%", str(round(hi / 60, 3)))
                        .replace("%CHANCE%", str(round(lib.chance, 3))))
        self.write_text("res://commands/scumm_roam.gd", """class_name ScummRoamCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_roam(actor, op[, x_min, x_max, y_min, y_max])`
# op: start (with the rectangle), pause, resume, stop; "setup" in a room's
# :setup makes sure the roaming node runs (e.g. after loading a game).


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(
\t\t2,
\t\t[TYPE_STRING, TYPE_STRING, TYPE_INT, TYPE_INT, TYPE_INT, TYPE_INT],
\t\t[null, null, 0, 0, 0, 0]
\t)


func run(command_params: Array) -> int:
\tScummRoaming.get_node_instance().control(command_params[0], command_params[1], command_params.slice(2))
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")

    def write_validator(self) -> None:
        """A scene that compiles every .esc file, for `godot --headless res://tools/validate_esc.tscn`."""
        self.write_text("res://tools/validate_esc.gd", """extends Node
# Compiles every ESC script in the project and quits. Errors show up in the log
# as "Error(s) found during parsing of script ...".

func _ready() -> void:
\tvar files: Array[String] = []
\t_collect("res://", files)
\tfor path in files:
\t\tescoria.esc_compiler.load_esc_file(path)
\tprint("validate_esc: checked %d scripts" % files.size())
\tget_tree().quit()


func _collect(dir_path: String, files: Array[String]) -> void:
\tfor d in DirAccess.get_directories_at(dir_path):
\t\tif not d.begins_with(".") and d != "addons" and d != "build":
\t\t\t_collect(dir_path.path_join(d), files)
\tfor f in DirAccess.get_files_at(dir_path):
\t\tif f.ends_with(".esc"):
\t\t\tfiles.append(dir_path.path_join(f))
""")
        scn = Resource("gd_scene")
        scn.node("validate_esc", "Node", props={"script": scn.ext("Script", "res://tools/validate_esc.gd")})
        scn.save(self.path("res://tools/validate_esc.tscn"))

    def copy_addons(self) -> None:
        for addon in ADDONS:
            src = os.path.join(self.escoria_src, "addons", addon)
            dst = os.path.join(self.out, "addons", addon)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            # escoria-core/testing needs gdUnit4, which we don't ship.
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("testing"))
        for f in ("icon.png", "default_env.tres"):
            shutil.copy(os.path.join(self.escoria_src, f), os.path.join(self.out, f))
        for d in ("items/inventory", "speech", "dialog_avatars"):
            self.write_text(f"res://{d}/.gdkeep", "")
        # Export output lives inside the project; keep Godot from importing it.
        self.write_text("res://build/.gdignore", "")

    def write_project_settings(self) -> None:
        """Reuse the demo's project.godot, keeping only what an Escoria game needs."""
        sections = _read_godot_ini(os.path.join(self.escoria_src, "project.godot"))
        keep = ("application", "audio", "autoload", "display", "editor",
                "editor_plugins", "escoria", "input", "rendering")
        sections = {k: v for k, v in sections.items() if k in keep}

        sections["application"]["config/name"] = f'"{self.game.name}"'
        sections["display"]["window/size/viewport_width"] = str(VIEWPORT[0])
        sections["display"]["window/size/viewport_height"] = str(VIEWPORT[1])
        # SCUMM's screen is black outside the room and the verb bar.
        sections.setdefault("rendering", {})["environment/defaults/default_clear_color"] = "Color(0, 0, 0, 1)"
        if self.theme_path("gui"):
            # The engine GUI (Escoria's menus) uses the game's GUI charset.
            sections.setdefault("gui", {})["theme/custom"] = f'"{self.theme_path("gui")}"'
        sections["editor_plugins"]["enabled"] = "PackedStringArray(" + ", ".join(
            f'"res://addons/{a}/plugin.cfg"' for a in ADDONS) + ")"
        esc = sections["escoria"]
        esc.update({
            "main/game_start_script": '"res://start_game.esc"',
            "main/text_lang": '"en_US"',
            "main/voice_lang": '"en_US"',
            "main/savegames_path": '"user://saves/"',
            "main/action_default_script": '"res://action_defaults.esc"',
            "ui/inventory_items_path": '"res://items/inventory"',
            "sound/speech_folder": '"res://speech"',
            "sound/speech_extension": '"wav"',
            "dialog_simple/avatars_path": '"res://dialog_avatars"',
            # SCUMM prints a whole line at once (res://ui/scumm_say.gd times it).
            "dialog_simple/text_time_per_letter_ms": "0",
            "dialog_simple/text_time_per_fast_letter_ms": "0",
            "debug/enable_room_selector": "false",
            "debug/room_selector_room_dir": '"res://rooms"',
            "debug/enable_hover_stack_viewer": "false",
            "ui/game_scene": '"res://ui/game.tscn"',
            "main/command_directories": '["res://addons/escoria-core/game/core-scripts/esc/commands", "res://commands"]',
            "ui/inventory_item_size": "Vector2(%d, %d)" % tuple(c * SCALE for c in self.inventory_icon_size()),
        })
        for k in [k for k in esc if k.startswith("wizard/")]:
            del esc[k]

        lines = ["; Generated by scumm2escoria", "", "config_version=5", ""]
        for name, values in sections.items():
            lines.append(f"[{name}]")
            lines.append("")
            lines += [f"{k}={v}" for k, v in values.items()]
            lines.append("")
        self.write_text("res://project.godot", "\n".join(lines))

    def write_export_presets(self) -> None:
        """A Web preset, single-threaded so it runs without COOP/COEP headers."""
        self.write_text("res://export_presets.cfg", f"""[preset.0]

name="Web"
platform="Web"
runnable=true
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter="*.esc, *.cfg"
exclude_filter=""
export_path="build/web/index.html"
encryption_include_filters=""
encryption_exclude_filters=""
encrypt_pck=false
encrypt_directory=false
script_export_mode=1

[preset.0.options]

custom_template/debug=""
custom_template/release=""
variant/extensions_support=false
variant/thread_support=false
vram_texture_compression/for_desktop=true
vram_texture_compression/for_mobile=false
html/export_icon=true
html/custom_html_shell=""
html/head_include=""
html/canvas_resize_policy=2
html/focus_canvas_on_start=true
html/experimental_virtual_keyboard=false
progressive_web_app/enabled=false
""")

    def write_start_script(self) -> None:
        start = self.game.start_room
        if not start:
            self.log("warning: no start room found")
            return
        scene = f"res://rooms/{snake(start)}/{snake(start)}.tscn"
        # Every variable the translated scripts use, with its initial value.
        state_vars = {f"{self.ids[k]}_state": o.state for k, o in self.game.objects.items() if k in self.ids}
        # Where game setup puts each actor ("" = in no room yet).
        state_vars.update({f"{snake(n)}_room": f'"{a.start_room or ""}"' for n, a in self.game.actors.items()})
        if self.game.version < 6:
            # VAR_MACHINE_SPEED: games leave out their idle animations on slow machines.
            state_vars["g6"] = FAST_MACHINE
            state_vars["scumm_conversation"] = '""'
        globals_ = "\n".join(f"\tglobal {g} = {state_vars.get(g, 0)}" for g in sorted(self.tr.used_globals))
        inventory = "".join(f'\tinventory_add("{self.ids[k]}")\n' for k in self.game.start_inventory if k in self.ids)
        # SCUMM games boot straight into their first room (here, the title
        # screen); Escoria's menu stays available with Esc. "New game" from that
        # menu runs :newgame, which clears everything, so both events start alike.
        # Objects (inventory too) register with the current room: add them once it exists.
        # The boot script's screenEffect, from an empty screen.
        effect = (f"\tscumm_screen_effect({self.game.boot_effect})\n" if self.game.boot_effect else "") + \
            "\tscumm_fade_out(true)\n"
        body = f'{globals_}\n{effect}\tchange_scene("{scene}")\n{inventory}'
        self.write_text("res://start_game.esc", f":init\n{body}\n:newgame\n{body}")

    # ------------------------------------------------------------------ rooms

    def patch_exit_analyzer(self) -> None:
        """Escoria's static check of :exit_scene runs without the game's globals and stops
        the game when an assignment reads one, as doors do with their state. It doesn't
        care about values."""
        path = self.path("res://addons/escoria-core/tools/script_analysis/analyzers/esc_script_analyzer.gd")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        old = "\t\treturn _globals.get_value(name)\n\treturn _environment.get_at("
        new = "\t\treturn _globals.get_values().get(name.get_lexeme(), 0)\n\treturn _environment.get_at("
        if old not in text:
            self.log("warning: Escoria's script analyzer changed: not patched")
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new))

    def patch_background_events(self) -> None:
        """Idle animations run on channels of their own. Escoria stops every channel on
        a click, an interrupted `while` skips its commands and never ends, and the loops
        assign all kinds of values."""
        core = "res://addons/escoria-core/game/core-scripts/esc"
        keep = '(_changing_scene or not channel_name.begins_with("scumm_bg"))'
        patches = {
            f"{core}/esc_event_manager.gd": [
                ("if _running_events[channel_name] != null and not _running_events[channel_name]"
                 ".get_event_name() in exceptions:",
                 f"if {keep} and _running_events[channel_name] != null and not _running_events[channel_name]"
                 ".get_event_name() in exceptions:"),
                ("\t\tif events_queue[channel_name] != null:\n\t\t\tvar found_exception",
                 f"\t\tif {keep} and events_queue[channel_name] != null:\n\t\t\tvar found_exception")],
            f"{core}/compiler/esc_interpreter.gd": [
                ("\t\tvar ret = await _execute(stmt.get_body())\n\n\t\tif ret is ESCGrammarStmts.Break:",
                 "\t\tvar ret = await _execute(stmt.get_body())\n\n"
                 "\t\tif _current_event and _current_event.is_interrupted():\n\t\t\treturn null\n\n"
                 "\t\tif ret is ESCGrammarStmts.Break:"),
                # An assignment gives its value, read as a return code: `x = 2` is an error.
                ("\treturn await _evaluate(stmt.get_expression())\n",
                 "\tvar value = await _evaluate(stmt.get_expression())\n"
                 "\treturn null if stmt.get_expression() is ESCGrammarExprs.Assign else value\n")],
        }
        for res, changes in patches.items():
            path = self.path(res)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for old, new in changes:
                if old not in text:
                    self.log(f"warning: {res} changed: not patched")
                text = text.replace(old, new)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

    def write_background_commands(self) -> None:
        """What background scripts use: objects are numbers there, known at runtime."""
        rooms = {room for room, events in self.tr.background_events.items() if any(events.values())}
        if not rooms:
            return
        objects = {int(n): self.game.objects[k] for n, k in self.tr.numbered_objects.items()
                   if k in self.ids and self.game.objects[k].room in rooms}
        rows = []
        for n, obj in sorted(objects.items()):
            # Drawing an object clears the ones in exactly the same place.
            same = [m for m, o in objects.items() if m != n and o.room == obj.room
                    and (o.x, o.y, o.w, o.h) == (obj.x, obj.y, obj.w, obj.h)]
            rows.append(f'\t{n}: ["{self.ids[obj.key]}", "{self.tr.object_state_var(obj.key)}", {same}],')
        self.write_text("res://commands/scumm_draw.gd", """class_name ScummDrawCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_draw(number)`: SCUMM's drawObject for an object
# only known at runtime. It takes the place of the others at the same position.

# number: [global id, state variable, objects at the same position]
const OBJECTS := {
%ROWS%
}


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(1, [[TYPE_INT, TYPE_FLOAT], [TYPE_INT, TYPE_FLOAT]], [null, 1])


func run(command_params: Array) -> int:
\tvar number := int(command_params[0])
\tif number in OBJECTS:
\t\tif int(command_params[1]) == 0:
\t\t\t_put(number, 0)
\t\t\treturn ESCExecution.RC_OK
\t\tfor other in OBJECTS[number][2]:
\t\t\t_put(other, 0)
\t\t_put(number, 1)
\treturn ESCExecution.RC_OK


func _put(number: int, state: int) -> void:
\tvar row: Array = OBJECTS[number]
\tescoria.globals_manager.set_global(row[1], state, true)
\tif escoria.object_manager.has(row[0]):
\t\tescoria.object_manager.get_object(row[0]).set_state("state_%d" % state)


func interrupt():
\tpass
""".replace("%ROWS%", "\n".join(rows)))
        self.write_text("res://commands/scumm_get_state.gd", """class_name ScummGetStateCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_get_state(variable, number)`: the state of an
# object only known at runtime.


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(2, [TYPE_STRING, [TYPE_INT, TYPE_FLOAT]], [null, null])


func run(command_params: Array) -> int:
\tvar row = ScummDrawCommand.OBJECTS.get(int(command_params[1]))
\tvar state = escoria.globals_manager.get_global(row[1]) if row else 0
\tescoria.globals_manager.set_global(command_params[0], state if state else 0, true)
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")
        walk = """func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(%s)


func run(p: Array) -> int:
\t_walker = escoria.object_manager.get_object(p[0]).node
\tvar target = %s
\tif target == null:
\t\treturn ESCExecution.RC_OK
\t_walker.arrived.connect(_done, CONNECT_ONE_SHOT)
\t_walker.walk_to(target, ESCWalkContext.new(null, target, false, true))
\tget_tree_timer().timeout.connect(_done.bind(null))
\tawait finished
\treturn ESCExecution.RC_OK


func get_tree_timer() -> SceneTreeTimer:
\treturn escoria.get_tree().create_timer(LONGEST_WALK)


func _done(_context) -> void:
\tif _walker != null:
\t\t_walker = null
\t\tfinished.emit()


func interrupt():
\tif _walker != null:
\t\t_walker.stop_walking_now()
\t\t_done(null)
"""
        head = """extends ESCBaseCommand
# Generated by scumm2escoria. %s
# Unlike walk, it leaves alone what the player is doing.

signal finished

const LONGEST_WALK := 20.0      # in case the place can't be reached

var _walker: ESCItem


"""
        number = "[TYPE_INT, TYPE_FLOAT]"
        self.write_text("res://commands/scumm_walk.gd", "class_name ScummWalkCommand\n"
                        + head % "`scumm_walk(actor, x, y)`: an actor on its own walks there."
                        + walk % (f"3, [TYPE_STRING, {number}, {number}], [null, null, null]", "Vector2(p[1], p[2])"))
        self.write_text("res://commands/scumm_walk_object.gd", "class_name ScummWalkObjectCommand\n"
                        + head % "`scumm_walk_object(actor, number)`: an actor on its own walks to an object."
                        + walk % (f"2, [TYPE_STRING, {number}], [null, null]", "_place(int(p[1]))")
                        + """

func _place(number: int):
\tvar row = ScummDrawCommand.OBJECTS.get(number)
\tif row and escoria.object_manager.has(row[0]):
\t\treturn escoria.object_manager.get_object(row[0]).node.get_interact_position()
\treturn null
""")
        self.write_text("res://commands/scumm_stop_bg.gd", """class_name ScummStopBgCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_stop_bg(event)`: SCUMM's stopScript for what runs
# on a channel of its own.


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(1, [TYPE_STRING], [null])


func run(p: Array) -> int:
\tescoria.event_manager.interrupt_channel(p[0])
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")
        self.write_text("res://commands/scumm_running.gd", """class_name ScummRunningCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_running(variable, event)`: SCUMM's isScriptRunning.


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(2, [TYPE_STRING, TYPE_STRING], [null, null])


func run(p: Array) -> int:
\tvar running := 0
\tfor channel in [p[1], escoria.event_manager.CHANNEL_FRONT]:
\t\tvar event = escoria.event_manager.get_running_event(channel)
\t\tif event != null and event.get_event_name() == p[1]:
\t\t\trunning = 1
\tescoria.globals_manager.set_global(p[0], running, true)
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")
        self.write_text("res://commands/scumm_get_var.gd", """class_name ScummGetVarCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_get_var(variable, array, index)`: a v5 array is a
# run of numbered variables, b374[2] being b376.


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(3, [TYPE_STRING, TYPE_STRING, [TYPE_INT, TYPE_FLOAT]], [null, null, null])


func run(command_params: Array) -> int:
\tvar array: String = command_params[1]
\tvar name := "%s%d" % [array.left(1), int(array.substr(1)) + int(command_params[2])]
\tvar value = escoria.globals_manager.get_global(name)
\tescoria.globals_manager.set_global(command_params[0], value if value else 0, true)
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")
        self.write_text("res://commands/scumm_rand.gd", """class_name ScummRandCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_rand(variable, limit)`: 0..limit, both included.
# rand_global only takes whole numbers, and SCUMM's divisions can leave a float here.


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(2, [TYPE_STRING, [TYPE_INT, TYPE_FLOAT]], [null, null])


func run(command_params: Array) -> int:
\tescoria.globals_manager.set_global(command_params[0], randi() % (int(command_params[1]) + 1), true)
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")

    def enters_with_ego(self) -> bool:
        """Does any code bring the ego into a room at a place of its own?"""
        code = [*(r.entry for r in self.game.rooms.values()),
                *(s.body for s in self.game.scripts.values()),
                *(c.body for o in self.game.objects.values() for c in o.verbs)]
        return any(st.expr[:1] == [("ident", "startRoomWithEgo")] for body in code for st in statements(body))

    def write_room(self, room: Room) -> None:
        rid = snake(room.name)
        base = f"res://rooms/{rid}"
        bg = Image.open(room.background).convert("RGBA")
        w, h = bg.size
        self.log(f"room {room.name}: {w}x{h}, {len(room.boxes)} boxes, {len(room.zplanes)} z-planes")

        playable = bool(room.boxes)
        scn = Resource("gd_scene")
        props = {
            "script": scn.ext("Script", ESC_ROOM),
            "global_id": rid,
            "camera_limits": [rect2(0, 0, w * SCALE, h * SCALE)],
            "esc_script": f"{base}/{rid}.esc",
        }
        if self.player_scene:
            # Cutscene rooms get the player too, hidden: Escoria's `say` refuses
            # to show any text in a room without a player.
            props["player_scene"] = scn.ext("PackedScene", self.player_scene)
        scn.node(rid, "Node2D", props=props)
        self.write_text(f"{base}/{rid}.esc", self.room_script(room, playable, h >= FULL_SCREEN))

        scn.node("background", "Sprite2D", ".", {
            "texture": scn.ext("Texture2D", self.save_png(bg, f"{base}/background.png")),
            "centered": False,
        })

        if playable:
            vertices, polygons = navigation_mesh(room.boxes)
        else:
            # No boxes: actors (like a flying ufo) move freely, even off screen.
            vertices = [(-w, 0), (2 * w, 0), (2 * w, h), (-w, h)]
            polygons = [[0, 1, 2, 3]]
        nav = scn.sub("NavigationPolygon", {
            "vertices": packed_vec2((x * SCALE, y * SCALE) for x, y in vertices),
            "polygons": Raw("Array[PackedInt32Array]([" + ", ".join(
                "PackedInt32Array(" + ", ".join(map(str, p)) + ")" for p in polygons) + "])"),
        })
        scn.node("walkable_area", "Node2D", ".", {"script": scn.ext("Script", ESC_TERRAIN)})
        scn.node("NavigationRegion2D", "NavigationRegion2D", "walkable_area",
                 {"navigation_polygon": nav})

        scn.node("zplanes", "Node2D", ".")
        for name, img, (x, y), z in self.zplane_sprites(room, bg, self.object_rects(room)):
            scn.node(name, "Sprite2D", "zplanes", {
                "z_index": z,
                "position": vec2(x * SCALE, y * SCALE),
                "texture": scn.ext("Texture2D", self.save_png(img, f"{base}/{name}.png")),
                "centered": False,
            })

        self.write_objects(scn, room, base, bg)

        scn.node("actors", "Node2D", ".")
        for name, (x, y) in list(room.actor_positions.items()) + list(room.scripted_actors.items()):
            if name not in self.actor_scenes:
                continue
            scene = self.costume_scenes[name, self.game.costume_of(name, room.name)]
            scn.node(snake(name), parent="actors",
                     instance=scn.ext("PackedScene", scene), props={
                         "position": vec2(x * SCALE, y * SCALE),
                         "z_index": y * SCALE,
                     })

        if room.click_script:
            self.write_click_area(scn, room, base, w, h)

        if self.player_scene:
            if playable:
                sx, sy = room.actor_positions.get(self.game.ego) or start_point(room.boxes)
            else:
                sx, sy = -w // 2, h // 2      # off screen; hidden by :setup anyway
            scn.node("player_start", "Marker2D", ".", {
                "position": vec2(sx * SCALE, sy * SCALE),
                "script": scn.ext("Script", ESC_LOCATION),
                "global_id": f"{rid}_start",
                "is_start_location": True,
            })
        scn.save(self.path(f"{base}/{rid}.tscn"))

    def room_script(self, room: Room, playable: bool, full_screen: bool = False) -> str:
        """:setup (UI visibility, state-dependent objects) and, for cutscene rooms, :ready."""
        from .ashes import Context
        setup = []
        if not playable:
            # Rooms without boxes (title, cutscenes) never show the verb bar; in
            # playable rooms the scripts turn it on (Verbs::showVerbs).
            setup.append("\tset_gui_visible(false)")
        elif not any(k.endswith("::showVerbs") for k in self.game.scripts):
            # Nothing in the game's scripts turns the bar on: cutscenes hide it themselves.
            setup.append(f"\tset_gui_visible({'false' if full_screen else 'true'})")
        setup += self.room_setup_lines(room)
        if self.enters_with_ego():
            setup += ["\tif ego_entry_x != 0:", '\t\tteleport_pos("player", ego_entry_x, ego_entry_y)',
                      "\t\tego_entry_x = 0"]
            self.tr.used_globals |= {"ego_entry_x", "ego_entry_y"}
        # SCUMM actors are in one room at a time (putActorAt): show those that are here.
        present = [self.game.ego] + [a for a in list(room.actor_positions) + list(room.scripted_actors)
                                     if a in self.actor_scenes]
        for actor in dict.fromkeys(a for a in present if a):
            aid = "player" if actor == self.game.ego else self.ids[actor]
            setup += [f'\tif {self.tr.actor_room_var(actor)} == "{room.name}":',
                      f'\t\tset_active("{aid}", true)',
                      "\telse:",
                      f'\t\tset_active("{aid}", false)']
        # The room is set up under the old picture; now reveal it (screenEffect).
        setup.append("\tscumm_restore_names()")     # names changed by setObjectName
        if self.game.roaming:
            setup.append('\tscumm_roam("", "setup")')
        setup.append("\tscumm_fade_in()")
        text = ":setup\n" + "\n".join(setup) + "\n"
        ready = [":ready"]
        if room.entry:
            # SCUMM runs a room's entry script every time it is entered.
            self.tr.ready_room = room.name
            ready = self.tr.event("ready", room.entry, Context(room=room.name, in_entry=True))
            self.tr.ready_room = None
        if self.game.version < 6:
            ready += self.tr.room_conversations(room.name)
        if self.enters_with_ego():
            # startRoomWithEgo can say where the ego walks to from the door.
            ready[1:1] = ["\tif ego_walk_x != 0:", '\t\twalk_to_pos("player", ego_walk_x, ego_walk_y)',
                          "\t\tego_walk_x = 0"]
            self.tr.used_globals |= {"ego_walk_x", "ego_walk_y"}
        background = [line for lines in self.tr.background_events.get(room.name, {}).values() if lines
                      for line in lines]
        return text + "\n" + "\n".join(ready + background) if len(ready) > 1 else text

    def write_click_area(self, scn: Resource, room: Room, base: str, w: int, h: int) -> None:
        """`VAR_VERB_SCRIPT = x` in the entry: any click runs x. An exit covering the room."""
        from .ashes import Context
        cid = f"{snake(room.name)}_click"
        self.write_text(f"{base}/{cid}.esc", "\n".join(
            self.tr.event("exit_scene", room.click_script, Context(room=room.name))))
        scn.node(cid, "Area2D", ".", {
            "script": scn.ext("Script", ESC_ITEM),
            "global_id": cid,
            "esc_script": f"{base}/{cid}.esc",
            "is_exit": True,
            "player_orients_on_arrival": False,
            "actions_requiring_target_object": Raw("PackedStringArray()"),
        })
        scn.node("collision", "CollisionPolygon2D", cid, {
            "polygon": packed_vec2([(0, 0), (w * SCALE, 0), (w * SCALE, h * SCALE), (0, h * SCALE)]),
        })

    def zplane_sprites(self, room: Room, bg: Image.Image, exclude=()):
        """Cut each connected region of each z-plane out of the background.

        SCUMM hides an actor behind z-plane p when the box it stands in has
        mask >= p. Escoria sorts actors by z_index == y, so each region gets
        the z_index that puts actors of "behind" boxes under it and actors of
        the other boxes over it. Boxes on either side of a region often need
        different values, so regions are split into vertical strips.

        Areas covered by objects that change image are left out (`exclude`):
        each object carries those strips itself, one set per state.
        """
        bg_px = np.asarray(bg)
        # The same image can be listed for several plane numbers.
        planes: dict[str, list[int]] = {}
        for i, path in enumerate(room.zplanes, start=1):
            planes.setdefault(path, []).append(i)

        for path, numbers in planes.items():
            mask = (np.asarray(Image.open(path).convert("L")) > 127).copy()
            for x, y, w, h in exclude:
                mask[y:y + h, x:x + w] = False
            first = min(numbers)
            yield from self.occluder_strips(room, mask, bg_px, first, f"zplane{first}")

    def column_z(self, room: Room, comp: np.ndarray, x: int, plane: int, region: int) -> int:
        aw, ah = self.actor_size
        column = comp[:, x]
        behind, front = [], []
        for box in room.boxes:
            # Can an actor standing in this box overlap this column of the region?
            if not box.min_x - aw // 2 <= x <= box.max_x + aw // 2:
                continue
            if not column[max(0, box.min_y - ah):box.max_y + 1].any():
                continue
            (behind if box.mask >= plane else front).append(box)

        if behind and not front:
            return Z_MAX
        if front and not behind:
            return min(b.min_y for b in front) * SCALE - 1
        if not behind:
            return int(np.nonzero(column)[0].max() + 1) * SCALE
        back_y = max(b.max_y for b in behind)
        front_y = min(b.min_y for b in front)
        if back_y > front_y:
            key = (room.name, plane, region, tuple(b.index for b in behind), tuple(b.index for b in front))
            if key not in self._warned:
                self._warned.add(key)
                self.log(f"  warning: {room.name} z-plane {plane} region {region}: boxes "
                         f"{list(key[3])} (behind) and {list(key[4])} (in front) overlap in depth")
        # Halfway into the last row of the "behind" boxes; a shared edge row is ambiguous anyway.
        return back_y * SCALE + SCALE // 2

    # ----------------------------------------------------------------- actors

    def actor_approach(self, actor: Actor) -> int:
        """Where the ego stops when walking to this actor, as an x offset.

        SCUMM v6 (walkActorToObj with distance 0) stops width * 1.5 pixels to
        the side the ego comes from, at the actor's y. Escoria needs a fixed
        point: use the side facing the middle of the room the actor starts in.
        """
        dist = (actor.width if actor.width is not None else ACTOR_WIDTH) * 3 // 2
        x = next((pos[0] for room in self.game.rooms.values()
                  for name, pos in list(room.actor_positions.items()) + list(room.scripted_actors.items())
                  if name == actor.name), 160)
        return dist if x < 160 else -dist

    def actor_folder(self, actor: str, costume: str) -> str:
        """Where an actor's scene goes: the usual costume has the plain name."""
        usual = costume == self.game.actors[actor].costume
        return snake(actor) if usual else f"{snake(actor)}_{snake(costume)}"

    def write_actor(self, actor: Actor, is_player: bool, folder: str | None = None) -> str:
        cost = self.game.costumes[actor.costume]
        aid = snake(actor.name)
        base = f"res://characters/{folder or aid}"

        # Frames, deduplicated by source image.
        textures: dict[str, str] = {}
        for pic in cost.pictures.values():
            if pic.path not in textures:
                img = Image.open(pic.path)
                textures[pic.path] = self.save_png(
                    _transparent(img), f"{base}/frames/{os.path.splitext(os.path.basename(pic.path))[0]}.png")

        anims, slots = escoria_animations(cost)
        if not anims:
            raise ValueError(f"costume {cost.name} has no usable animations")
        # SCUMM moves and animates actors once per game frame.
        game_fps = self.game.frames_per_second or FRAMES_PER_SECOND
        fps = game_fps / (actor.anim_speed or ANIM_FRAMES)

        # Pictures in a costume share the actor's anchor; take it from the first frame.
        # Elevated actors (setActorElevation) are drawn above their position.
        first = cost.pictures[next(iter(anims.values()))[0][0]]
        ox, oy = first.position
        oy -= actor.elevation
        sizes = [Image.open(p.path).size for p in cost.pictures.values()]
        w = max(s[0] for s in sizes)
        h = max(s[1] for s in sizes)
        if is_player:
            self.actor_size = (w, h)

        scn = Resource("gd_scene")
        frames = scn.sub("SpriteFrames", {"animations": Raw("[" + ", ".join(
            "{\n"
            f'"frames": [' + ", ".join(
                f'{{\n"duration": 1.0,\n"texture": {scn.ext("Texture2D", textures[cost.pictures[f].path])}\n}}'
                for f in pics) + "],\n"
            f'"loop": {"true" if loop else "false"},\n'
            f'"name": &"{name}",\n'
            f'"speed": {round(fps, 2)}\n'
            "}" for name, (pics, loop) in anims.items()) + "]")})
        shape = scn.sub("RectangleShape2D", {"size": vec2(w * SCALE, h * SCALE)})

        anim_res = f"{base}/{aid}_animations.tres"
        self.write_animation_resource(anim_res, slots)
        idle = slots["idle"]["down"][0] or next(iter(anims))

        props = {
            "process_mode": 1,
            "script": scn.ext("Script", ESC_PLAYER if is_player else ESC_ITEM),
            "global_id": "player" if is_player else aid,
            "esc_script": f"{base}/{aid}.esc",
        }
        props.update({
            "tooltip_name": actor.display_name or actor.name,
            "default_action": "look",
            "is_movable": True,
        })
        # In v5 what the player clicks is an object drawn under the actor, not the actor itself.
        if actor.untouchable or self.game.version < 6:
            props["is_interactive"] = False
        if not is_player:
            # The ego faces the actor when it arrives (Escoria reads the item's angle).
            props["interaction_angle"] = 270 if self.actor_approach(actor) > 0 else 90
        # For scumm_elevation: the sprite offset with no elevation.
        props["custom_data"] = {"scumm_base_offset_y": (oy + actor.elevation) * SCALE, **self.scumm_data(actor)}
        color = self.palette_color(cost, actor.talk_color)
        if color:
            props["dialog_color"] = Raw("Color(%.4f, %.4f, %.4f, 1)" % color)
        # Escoria has a single walking speed: use SCUMM's horizontal step. (Its
        # v_speed_damp scales *all* movement, so it can't model the slower y step.)
        step = actor.walk_speed[0] if actor.walk_speed else WALK_STEP
        props["speed"] = round(step * game_fps * SCALE)
        props["animations"] = scn.ext("Resource", anim_res)
        scn.node(aid, "Area2D", props=props)

        scn.node("sprite", "AnimatedSprite2D", ".", {
            "sprite_frames": frames,
            "animation": Raw(f'&"{idle}"'),
            "autoplay": idle,
            "centered": False,
            "offset": vec2(ox * SCALE, oy * SCALE),
        })
        scn.node("collision", "CollisionShape2D", ".", {
            "position": vec2((ox + w / 2) * SCALE, (oy + h / 2) * SCALE),
            "shape": shape,
        })
        # Speech is anchored like SCUMM's: talk position from the feet, minus elevation.
        tx, ty = actor.talk_pos or TALK_POS
        scn.node("dialog_position", "Marker2D", ".", {
            "position": vec2(tx * SCALE, (ty - actor.elevation) * SCALE),
            "script": scn.ext("Script", ESC_DIALOG_LOCATION),
        })
        if True:   # the ego too: other actors walk to it (walkActorToObj)
            dx = self.actor_approach(actor)
            # Escoria walks to the *last* ESCLocation child, and the dialog marker
            # is one too (ESCInteractionLocation should win, but get_interact_position
            # tests `is ESCLocation` first): so this goes after the dialog marker.
            scn.node("approach", "Marker2D", ".", {
                "position": vec2(dx * SCALE, 0),
                "script": scn.ext("Script", ESC_INTERACTION_LOCATION),
                # Face the actor on arrival.
                "interaction_angle": 270 if dx > 0 else 90,
            })
        scene_path = f"{base}/{aid}.tscn"
        scn.save(self.path(scene_path))
        self.write_text(f"{base}/{aid}.esc", self.actor_script(actor) or "# (no verbs)\n")
        self.log(f"actor {actor.name} ({cost.name}): {len(textures)} frames, "
                 f"{len(anims)} animations{' [player]' if is_player else ''}")
        return scene_path

    @staticmethod
    def palette_color(cost: Costume, index: int | None):
        """RGB (0..1) of a palette index in the costume's palette image."""
        if index is None or not cost.palette or not os.path.exists(cost.palette):
            return None
        pal = Image.open(cost.palette).getpalette()
        if not pal or len(pal) < (index + 1) * 3:
            return None
        return tuple(c / 255 for c in pal[index * 3:index * 3 + 3])

    # ------------------------------------------------------- script callbacks

    def anim_name_for(self, actor: str, anim: str) -> str | None:
        """Escoria animation for a SCUMM costume animation, e.g. beam -> beam_down.

        Scripts can't know where the actor faces when converted; SCUMM picks
        the frames by facing, here the down (or first available) one is used.
        """
        cost = self.game.costumes.get(self.game.costume_of(actor, self.tr.room) or "")
        if cost is None or anim not in cost.anims:
            return None
        _, slots = escoria_animations(cost)
        by_dir = slots.get(_STANDARD.get(anim, snake(anim)), {})
        for d in ("down", "right", "up", "left"):
            if d in by_dir and by_dir[d][0]:
                return by_dir[d][0]
        return None

    def pose_resource_for(self, actor: str, stand: str, talk: str) -> str | None:
        """An animation resource whose idle / speak animations are `stand` / `talk`."""
        cost = self.game.costumes.get(self.game.costume_of(actor, self.tr.room) or "")
        if cost is None:
            return None
        aid = snake(actor)
        folder = self.actor_folder(actor, cost.name)
        if (stand, talk) == ("stand", "talkStart"):
            return f"res://characters/{folder}/{aid}_animations.tres"
        res = f"res://characters/{folder}/{aid}_{snake(stand)}_{snake(talk)}.tres"
        self.pose_variants[res] = (cost, stand, talk)
        return res

    def write_pose_variants(self) -> None:
        for res, (cost, stand, talk) in self.pose_variants.items():
            _, slots = escoria_animations(cost)
            slots = dict(slots)
            slots["idle"] = slots.get(_STANDARD.get(stand, snake(stand)), slots["idle"])
            slots["speak"] = slots.get(_STANDARD.get(talk, snake(talk)), slots["speak"])
            self.write_animation_resource(res, slots)
        if self.pose_variants:
            self.log(f"poses: {len(self.pose_variants)} extra animation sets")

    def write_animation_resource(self, res_path: str, slots: dict) -> None:
        res = Resource("gd_resource", "ESCAnimationResource")
        angle_script = res.ext("Script", ESC_DIR_ANGLE)
        name_script = res.ext("Script", ESC_ANIM_NAME)
        angles, lists = [], {"walk": [], "idle": [], "speak": []}
        for esc_dir, _, start, size in ESC_DIRECTIONS:
            angles.append(res.sub("Resource", {"script": angle_script,
                                               "angle_start": start, "angle_size": size}))
            for kind in lists:
                name, mirrored = slots[kind][esc_dir]
                props = {"script": name_script, "animation": name}
                if mirrored:
                    props["mirrored"] = True
                lists[kind].append(res.sub("Resource", props))

        def typed(script: Raw, items: list[Raw]) -> Raw:
            return Raw(f"Array[{script}]([" + ", ".join(map(str, items)) + "])")

        res.resource({
            "script": res.ext("Script", ESC_ANIM_RES),
            "dir_angles": typed(angle_script, angles),
            "directions": typed(name_script, lists["walk"]),
            "idles": typed(name_script, lists["idle"]),
            "speaks": typed(name_script, lists["speak"]),
        })
        res.save(self.path(res_path))


# --------------------------------------------------------------------------
# Costume -> Escoria animations
# --------------------------------------------------------------------------

_STANDARD = {"walk": "walk", "stand": "idle", "talkStart": "speak"}


def escoria_animations(cost: Costume):
    """Return ({anim name: (picture names, loop)}, slots).

    slots[kind][escoria direction] = (animation name, mirrored) for every
    animation kind; walk / idle / speak feed ESCAnimationResource, and the
    others (e.g. stand_with_phone) can replace idle / speak for a pose.
    """
    anims: dict[str, tuple[list[str], bool]] = {}
    slots: dict[str, dict[str, tuple[str, bool]]] = {k: {} for k in _STANDARD.values()}
    for anim in cost.anims.values():
        slots.setdefault(_STANDARD.get(anim.name, snake(anim.name)), {})

    for anim in cost.anims.values():
        kind = _STANDARD.get(anim.name, snake(anim.name))
        for esc_dir, scumm_dir, _, _ in ESC_DIRECTIONS:
            frames, loop = anim.frames_for(scumm_dir)
            if not frames:
                continue
            name, mirrored = f"{kind}_{esc_dir}", False
            if scumm_dir == "W" and cost.flip:
                # SCUMM mirrors the W direction of FLIP costumes.
                mirrored = True
                east, _ = anim.frames_for("E")
                if frames == east:
                    name = f"{kind}_right"
            if name not in anims:
                anims[name] = (frames, loop)
            slots[kind][esc_dir] = (name, mirrored)

    # Fill missing directions with whatever this kind has (e.g. single-direction costumes).
    for kind, by_dir in slots.items():
        fallback = by_dir.get("down") or next(iter(by_dir.values()), None)
        if fallback is None:
            fallback = next(((n, False) for n in anims), ("", False))
        for esc_dir, *_ in ESC_DIRECTIONS:
            by_dir.setdefault(esc_dir, fallback)
    return anims, slots


def _transparent(img: Image.Image) -> Image.Image:
    """Palette index 0 is the transparent color in SCUMM costumes."""
    rgba = img.convert("RGBA")
    if img.mode == "P":
        alpha = np.where(np.asarray(img) == 0, 0, 255).astype(np.uint8)
        rgba.putalpha(Image.fromarray(alpha, "L"))
    return rgba


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def _on_segment(p, a, b) -> float | None:
    """Parameter t in (0, 1) if p lies strictly inside segment ab, else None."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if (px - ax) * dy - (py - ay) * dx != 0:
        return None
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return None
    t = ((px - ax) * dx + (py - ay) * dy) / length2
    return t if 0 < t < 1 else None


def line_ends(box: Box) -> tuple | None:
    """The two ends of a box drawn as a line, else None."""
    ends = list(dict.fromkeys(box.corners))
    return tuple(ends) if len(ends) == 2 else None


def widen_lines(boxes: list[Box]) -> list[Box]:
    """Give some width to the boxes that are lines: SCUMM walks along them, a mesh can't.

    Where a line meets a box, its end spreads along the box's edge; where it meets
    another line both take the same two points. Either way the neighbours share an edge.
    """
    solid = [b for b in boxes if line_ends(b) is None and len(set(b.corners)) > 2]
    joints: dict[tuple, tuple] = {}

    def spread(point, other):
        if point in joints:
            return joints[point]
        for box in solid:
            for i, a in enumerate(box.corners):
                b = box.corners[(i + 1) % len(box.corners)]
                t = _on_segment(point, a, b)
                if a == point or b == point or t is not None:
                    length = max(1.0, ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** .5)
                    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
                    joints[point] = ((round(point[0] - ux * LINE_WIDTH), round(point[1] - uy * LINE_WIDTH)),
                                     (round(point[0] + ux * LINE_WIDTH), round(point[1] + uy * LINE_WIDTH)))
                    return joints[point]
        dx, dy = other[0] - point[0], other[1] - point[1]
        length = max(1.0, (dx * dx + dy * dy) ** .5)
        nx, ny = -dy / length * LINE_WIDTH, dx / length * LINE_WIDTH
        joints[point] = ((round(point[0] + nx), round(point[1] + ny)),
                         (round(point[0] - nx), round(point[1] - ny)))
        return joints[point]

    out = []
    for box in boxes:
        ends = line_ends(box)
        if ends is None:
            out.append(box)
            continue
        a1, a2 = spread(ends[0], ends[1])
        b1, b2 = spread(ends[1], ends[0])
        # The two sides of the strip mustn't cross.
        dx, dy = ends[1][0] - ends[0][0], ends[1][1] - ends[0][1]
        side = lambda p, o: (p[0] - o[0]) * dy - (p[1] - o[1]) * dx
        if side(a1, ends[0]) * side(b1, ends[1]) < 0:
            b1, b2 = b2, b1
        out.append(replace(box, corners=[a1, b1, b2, a2]))
    return out


def navigation_mesh(boxes: list[Box]):
    """Turn walk boxes into a NavigationPolygon (vertices, polygons).

    SCUMM boxes often touch along part of an edge (T-junctions). Godot only
    links polygons through shared edges, so every box corner lying on another
    box's edge is inserted into that edge.
    """
    usable = widen_lines([b for b in boxes if not b.flags & 0x80])
    vertices, polygons = mesh_of(usable)
    if not connected(polygons):
        # Boxes that only touch at a corner, or along part of a line.
        vertices, polygons = mesh_of(walkable_strips(usable))
    return vertices, polygons


def connected(polygons: list[list[int]]) -> bool:
    """Can one walk between all the polygons? Godot links them through shared edges."""
    edges = [{frozenset((p[i], p[(i + 1) % len(p)])) for i in range(len(p))} for p in polygons]
    reached, pending = {0}, [0]
    while pending:
        here = pending.pop()
        for other in set(range(len(polygons))) - reached:
            if edges[here] & edges[other]:
                reached.add(other)
                pending.append(other)
    return len(reached) >= len(polygons)


def walkable_strips(boxes: list[Box]) -> list[Box]:
    """The boxes drawn together, a pixel fatter, as rectangles: whatever touches, joins."""
    width = max(x for b in boxes for x, _ in b.corners) + 4
    height = max(y for b in boxes for _, y in b.corners) + 4
    image = Image.new("L", (width, height))
    draw = ImageDraw.Draw(image)
    for box in boxes:
        draw.polygon(box.corners, fill=255, outline=255)
    mask = np.array(image.filter(ImageFilter.MaxFilter(3))) > 0
    strips, open_runs = [], {}
    for y in range(height + 1):
        row = mask[y] if y < height else np.zeros(width, bool)
        changes = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
        runs = set(zip(changes[::2].tolist(), changes[1::2].tolist()))
        for run in set(open_runs) - runs:
            strips.append((*run, open_runs.pop(run), y))
        for run in runs - set(open_runs):
            open_runs[run] = y
    return [Box(i, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], 0, 0, 255)
            for i, (x0, x1, y0, y1) in enumerate(strips)]


def mesh_of(usable: list[Box]):
    corners = {c for b in usable for c in b.corners}
    vertices: list[tuple[int, int]] = []
    index: dict[tuple[int, int], int] = {}
    polygons = []
    for box in usable:
        ring = []
        pts = box.corners
        for i, a in enumerate(pts):
            b = pts[(i + 1) % len(pts)]
            ring.append(a)
            inner = sorted((t, c) for c in corners if (t := _on_segment(c, a, b)) is not None)
            ring += [c for _, c in inner]
        # Drop repeated points (degenerate boxes have coincident corners).
        clean = [p for i, p in enumerate(ring) if p != ring[i - 1]]
        if len(set(clean)) < 3:
            continue
        poly = []
        for p in clean:
            if p not in index:
                index[p] = len(vertices)
                vertices.append(p)
            poly.append(index[p])
        polygons.append(poly)
    return vertices, polygons


def start_point(boxes: list[Box]) -> tuple[int, int]:
    """Center of the biggest box: a safe default spot for the player."""
    def area(b: Box) -> float:
        pts = b.corners
        return abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1]
                       for i in range(len(pts)))) / 2
    best = max(boxes, key=area)
    return (sum(x for x, _ in best.corners) // 4, sum(y for _, y in best.corners) // 4)


def connected_components(mask: np.ndarray):
    """4-connected regions of a boolean image, as boolean images."""
    seen = np.zeros_like(mask)
    h, w = mask.shape
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        comp = np.zeros_like(mask)
        queue = deque([(y0, x0)])
        seen[y0, x0] = True
        while queue:
            y, x = queue.popleft()
            comp[y, x] = True
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    queue.append((ny, nx))
        yield comp


def _read_godot_ini(path: str) -> dict[str, dict[str, str]]:
    """Minimal project.godot reader: {section: {key: raw value}}, multi-line aware."""
    sections: dict[str, dict[str, str]] = {}
    current = None
    key = None
    for line in open(path, encoding="utf-8").read().split("\n"):
        if line.startswith(";"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            current = sections.setdefault(m.group(1), {})
            key = None
            continue
        m = re.match(r'^([A-Za-z0-9_/."]+)=(.*)$', line)
        if m and current is not None:
            key = m.group(1)
            current[key] = m.group(2)
        elif current is not None and key is not None and line:
            current[key] += "\n" + line
    return sections


ROAMING_SCRIPT = """class_name ScummRoaming
extends Node
# Generated by scumm2escoria from the game's roaming script (%SOURCE%):
# while an actor roams, every DELAY_MIN..DELAY_MAX seconds it may (CHANCE)
# walk to a random point of its rectangle. The state of each actor lives in a
# global (scumm_roam_<actor>: "state,x_min,x_max,y_min,y_max") so it survives
# room changes and saved games. Like the original it keeps going while the
# actor is in another room, but only walks it when it is in the current one,
# and (unlike SCUMM) only when no game event is running, so it never fights a
# script that moves the actor.

const PREFIX := "scumm_roam_"
const DELAY_MIN := %DELAY_MIN%
const DELAY_MAX := %DELAY_MAX%
const CHANCE := %CHANCE%

var _timers := {}


static func get_node_instance() -> ScummRoaming:
\tvar node = escoria.get_node_or_null("ScummRoaming")
\tif node == null:
\t\tnode = ScummRoaming.new()
\t\tnode.name = "ScummRoaming"
\t\tescoria.add_child(node)
\treturn node


func control(actor: String, op: String, rect: Array) -> void:
\tif op == "setup":
\t\treturn
\tvar state := _state(actor)
\tmatch op:
\t\t"start":
\t\t\tstate = ["running"] + rect
\t\t"pause", "resume", "stop":
\t\t\tif state.is_empty() or state[0] == "stopped":
\t\t\t\treturn      # like the original: nothing to pause / resume / stop
\t\t\tstate[0] = {"pause": "paused", "resume": "running", "stop": "stopped"}[op]
\tescoria.globals_manager.set_global(PREFIX + actor, ",".join(state.map(func(v): return str(v))), true)
\tif op in ["pause", "stop"]:
\t\tvar node = _node(actor)
\t\tif node and node.is_moving():
\t\t\tnode.stop_walking_now()       # setActorStanding


func _process(delta: float) -> void:
\tvar globals: Dictionary = escoria.globals_manager.get_globals()
\tfor key in globals:
\t\tif not str(key).begins_with(PREFIX):
\t\t\tcontinue
\t\tvar actor := str(key).substr(PREFIX.length())
\t\tvar state := _state(actor)
\t\tif state.size() < 5 or state[0] != "running":
\t\t\tcontinue
\t\t_timers[actor] = _timers.get(actor, 0.0) - delta
\t\tif _timers[actor] > 0.0:
\t\t\tcontinue
\t\t_timers[actor] = randf_range(DELAY_MIN, DELAY_MAX)
\t\tif randf() >= CHANCE:
\t\t\tcontinue
\t\tvar node = _node(actor)
\t\tif node == null or not node.is_visible_in_tree() or node.is_moving() \\
\t\t\t\tor not escoria.event_manager.is_channel_free(escoria.event_manager.CHANNEL_FRONT):
\t\t\tcontinue
\t\tvar target := Vector2(randi_range(int(state[1]), int(state[2])), randi_range(int(state[3]), int(state[4])))
\t\tnode.walk_to(target, ESCWalkContext.new(null, target, false, true))


func _state(actor: String) -> Array:
\tif not escoria.globals_manager.has(PREFIX + actor):
\t\treturn []
\treturn Array(str(escoria.globals_manager.get_global(PREFIX + actor)).split(","))


func _node(actor: String):
\tif not escoria.object_manager.has(actor):
\t\treturn null
\tvar obj = escoria.object_manager.get_object(actor)
\treturn obj.node if obj and is_instance_valid(obj.node) else null
"""

