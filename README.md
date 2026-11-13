# scumm2escoria

Converts SCUMM games into Godot 4 + Escoria projects. It reads either the ScummC
source of a game or the compiled files of a SCUMM v5 or v6 game.

```
reading (frontend)              intermediate representation     output (backend)
ScummC (.scc/.scost/.box) ────► ir.Game (JSON with --ir) ─────► Godot + Escoria project
compiled .000/.001 ──► bundle ──┘
```

There are three commands. All of them run from the root of this repository:

| Command | What it does |
|---|---|
| `python3 -m scumm2escoria` | Converts a ScummC project or a bundle into a Godot project |
| `python3 -m scumm2escoria.frontend.scumm_bin.bundle` | Extracts a compiled game into a bundle: images, sounds, data and a disassembly of its scripts |
| `python3 -m scumm2escoria.frontend.scumm_bin.source` | Decompiles the scripts of a bundle into readable ScummC-style source |

## Requirements

- Python 3.10+ with [Pillow](https://pypi.org/project/pillow/) and
  [numpy](https://pypi.org/project/numpy/)
- The [Escoria demo game](https://github.com/godot-escoria/escoria-demo-game)
  repository (see below)
- [Godot](https://godotengine.org/) 4.7.x, to open, run or export the generated project
- A game to convert: the source code of a game written in
  [ScummC](https://github.com/AlbanBedel/scummc), such as OpenQuest, or the compiled
  files of a SCUMM v5 or v6 game

## Setup

The converter doesn't include Escoria itself. Each generated project gets a copy of
these Escoria addons, taken from the Escoria demo game repository:
`escoria-core`, `escoria-dialog-simple` and `escoria-ui-keyboard-9verbs`.

By default the converter looks for that repository in a folder named
`escoria-demo-game` next to this one, so the simplest setup is to clone everything
side by side:

```bash
mkdir adventures && cd adventures

git clone <this repository> scumm2escoria
git clone -b main https://github.com/godot-escoria/escoria-demo-game.git
git clone https://github.com/JenniBee/openquest.git    # a game to convert

pip install pillow numpy
```

```
adventures/
├── scumm2escoria/         this repository
├── escoria-demo-game/     Escoria addons are copied from here
└── openquest/             the game's ScummC source
```

If you clone the Escoria demo game somewhere else, pass its path with `--escoria`.

## Converting a ScummC project

Run it from the root of this repository:

```bash
cd scumm2escoria
python3 -m scumm2escoria "../openquest/Open Quest ScummC" ../openquest-escoria
```

`python3 -m scumm2escoria` runs the `scumm2escoria` Python package (the folder of
the same name inside this repository) as a program. It has to be run from the
repository root so that Python can find that folder.

The command above reads OpenQuest's ScummC source and writes a Godot project to
`../openquest-escoria`. Then open that folder in Godot (Project Manager → Import)
and press Play.

| Argument | Required | Description |
|---|---|---|
| `source` | yes | The game's ScummC directory: the one containing the `.scc` files. In OpenQuest it's `Open Quest ScummC` (quoted because of the spaces). It can also be a bundle: see [Converting a compiled game](#converting-a-compiled-game). |
| `output` | yes | Directory of the Godot project to create. If it already exists, the converter regenerates `characters/`, `rooms/` and `items/`, and replaces the addons. It also writes a `README.md` for the project and copies the game's readme and license files to `original/`. |
| `--escoria DIR` | no | The Escoria demo game checkout to copy the addons from. Default: `../escoria-demo-game`, relative to this repository. |
| `--ir FILE` | no | Also save the intermediate representation (everything read from the game, before generating the project) as a JSON file. Useful for debugging. |
| `--rooms A,B` | no | Export only these rooms and the scripts, actors, costumes and sounds they use. The game starts in the first one, running its entry script, or its first local script if it has none. Doors to other rooms lead nowhere. |
| `-v` | no | For a bundle, show what it holds and each step of reading it. |

Example with every option:

```bash
python3 -m scumm2escoria "../openquest/Open Quest ScummC" ../openquest-escoria \
    --escoria ~/src/escoria-demo-game --ir ../openquest.ir.json
```

At the end it prints how many `# TODO scumm:` comments were left in the generated
scripts.

## Converting a compiled game

A compiled game is a pair of files such as `GAME.000` (index) and `GAME.001` (data).
This example uses `road`, a small game that comes with ScummC. Build
[ScummC](https://github.com/AlbanBedel/scummc), run `make` in its `examples/road`
folder, and you'll have `scummc6.000` and `scummc6.001` there.

### 1. Extract the game

```bash
python3 -m scumm2escoria.frontend.scumm_bin.bundle ../scummc/examples/road ../road-bundle
```

It prints what it found:

```
SCUMM v6: 2 rooms, 10 objects, 24 scripts, 2 costumes, 3 charsets, 0 sounds
```

| Argument | Required | Description |
|---|---|---|
| `GAME_DIR` | yes | Folder with the `.000` and `.001` files. |
| `OUTPUT_DIR` | yes | Folder of the bundle to create. |
| `STEM` | no | Name of the files without their extension, for example `scummc6`. Without it the first `.000` in alphabetical order is used, so pass it when the folder holds more than one game. |

The result is a *bundle*, a folder with everything in the game:

| Path | Content |
|---|---|
| `game.json` | Rooms, walk boxes, objects, costumes, charsets, sounds and scripts |
| `rooms/` | Backgrounds and z-planes as PNG |
| `objects/` | Object states and their z-planes as PNG |
| `costumes/` | Costume cels as PNG, one per limb |
| `charsets/` | Glyphs as PNG |
| `sounds/` | Digital sounds as WAV; other kinds as raw `.bin` |
| `scripts/listing.txt` | Disassembly of every script |
| `palette.png` | The palette, as an indexed image |

If a script can't be read, the command names it as a `problem` and exits with an
error code; the rest of the bundle is still written.

### 2. Read the scripts (optional)

The bundle's `scripts/listing.txt` is the disassembly: one line per instruction,
with its offset and numbers as they are in the game. Decompiling gives the same
code as readable source, one file per room:

```bash
python3 -m scumm2escoria.frontend.scumm_bin.source ../road-bundle ../road-source
```

| Argument | Required | Description |
|---|---|---|
| `BUNDLE_DIR` | yes | The bundle. |
| `OUTPUT_DIR` | yes | Folder for the `.scc` files. |

This is the light switch in OpenQuest. In the disassembly:

```
; room 7 object 35 (light switch)
[0000] talkEgo("Some kind of power distribution grid.")
[0027] stopObjectCode()
[0028] if(getState(35)) -> 0075
[002F] cutscene([0])
[0034] talkEgo("I'll turn this off.")
[0049] wait(message())
[004B] animateActor(V1, 24)
```

And decompiled:

```c
object obj_35 { // "light switch"
case LookAt:
    egoSay("Some kind of power distribution grid.");
    return;
case Use:
case Move:
    unless(getObjectState(obj_35)) {
        cutscene(0) {
            egoSay("I'll turn this off.");
            waitForMessage();
            animateActor(VAR_EGO, costume_1_anim_anim24);
```

The decompiled files are for reading: they have no declarations, so ScummC can't
compile them. You don't need this step to convert a game.

### 3. Convert it

Pass the bundle to the converter instead of a ScummC project. The options are the
same:

```bash
python3 -m scumm2escoria ../road-bundle ../road-escoria
```

Then open `../road-escoria` in Godot and press Play. The conversion adds a
`composed/` folder to the bundle, with the costume frames it builds.

### Names in the decompiled code

Compiling removes the names, so the decompiler generates them:

| Name | What it is |
|---|---|
| `Room7` | Room 7, or the room's own name if the game has one |
| `obj_36` | Object 36. Its display name is in the comment next to it |
| `actor_5` | Actor 5 |
| `script_12`, `local_200` | Global script 12, local script 200 of a room |
| `entry`, `exit` | The scripts that run when a room is entered and left |
| `costume_1`, `sound_9`, `charset_2` | Costume, sound and charset resources |
| `costume_1_anim_anim24` | Animation 24. The standard ones have names: `init`, `walk`, `stand`, `talkStart`, `talkStop` |
| `g270`, `b8` | Global variable and bit variable |
| `l0` | Local variable or script parameter |
| `t0` | Temporary added by the decompiler for a value that crosses a branch |
| `VAR_EGO` | Variable of the engine, with its usual name |
| `Class3` | Object class |

These names come from the game, or from how it uses something:

| Name | Where it comes from |
|---|---|
| Verbs (`LookAt`, `PickUp`) | Their label: "Pick up" becomes `PickUp`, and "Examine" becomes `LookAt` |
| `UsedWith`, `InventoryObject`, `Preposition`, `SetBoxes`, `Icon` | Internal verbs, by how scripts call them |
| `Person`, `Pickable` | Classes, by what actors have in common and what "pick up" checks |
| `main` | Script 1, which the engine runs first |
| `defaultAction` | The script objects fall back on |
| `showVerbs`, `showDialog`, `dialogAdd`, `dialogStart`, `dialogHide`, `dialogEnd`, `dialogClear` | Scripts of the interface and of ScummC's dialog library |

### How the decompiler works

1. **Disassembly.** v5 keeps operands inside each instruction. v6 passes them through
   a stack, so its reader simulates the stack to rebuild them. Both give the same
   form of instruction.
2. **Structure.** Jumps become `if`, `while`, `for`, `switch`, `cutscene` and `try`
   again. A jump that fits none of them becomes a `goto`.
3. **Names.** Literal numbers get a name from the function that receives them: the
   first argument of `walkActorTo` is an actor, for example.
4. **Setup.** Actors, verbs, the ego and the first room aren't stored as data, so
   the setup scripts are run to find them.

The text then goes through the same parser as a real ScummC project, so the rest of
the conversion works the same way for both.

## What it converts

| SCUMM | Escoria |
|---|---|
| room `image` | background `Sprite2D` (scaled x4, no filtering) |
| boxes (`.box`: boxd/BOXM/SCAL) | `NavigationPolygon` in an `ESCTerrain` (with T-junctions resolved) |
| z-planes | strips cut from the background, with a `z_index` computed from each box's mask |
| ego costume (`.scost`) | `ESCPlayer` with `SpriteFrames` and an `ESCAnimationResource` (walk/idle/speak, W mirrored) |
| other actors | `ESCItem`s instanced in the room, with name, dialog color and starting position |
| `startRoom(...)` | `start_game.esc` |
| title screen, intro, verb bar, fonts | rooms without boxes, a UI generated from `setupVerbs()`, charsets → BMFont |
| objects, verbs and inventory | `ESCItem`s with states, a 9-verb UI with the game's verbs, verb scripts translated to ASHES |
| inventory items | From a room called `InventoryItems`, or from an object whose `Icon` verb names its picture: that object gets an inventory item with its own verbs |
| dialogs (`dialog.scc` library) | ASHES `?!` dialog blocks, with or without a loop around them. Each choice stores its number, for code that reads it later |
| combining objects (use X with Y, give X to Y) | Escoria combine actions |
| room `entry` scripts, cutscenes | ASHES `:setup` / `:ready` events, `accept_input`, `set_gui_visible` |
| sounds, voices, `screenEffect`, `setRoomRGBIntensity`, roaming actors | generated ASHES commands (`res://commands/scumm_*.gd`) |

## Contributing

Suggestions, bug reports and pull requests are welcome: open an issue to talk
about an idea or a problem.

The converter gets better with every game it meets. If you made, or know of, a
SCUMM game under a free license (or one whose authors allow it), I'd be glad to
work with it to test and improve the exporter.

If you find it useful, you can support its development with a
[donation via PayPal](https://www.paypal.com/donate/?hosted_button_id=AGB8C4H72NQMJ).

## License

MIT, see [LICENSE](LICENSE). The license covers the converter only: the games
you convert, and the projects generated from them, keep the license of the
original game.

## Notes

- So far the converter has only been tested with
  [OpenQuest](https://github.com/JenniBee/openquest), from its ScummC source and
  compiled, and with ScummC's `road` example, compiled. Other games will probably
  need changes to the converter.
- OpenQuest converts into a complete, playable game. Anything that can't be
  translated is left as a `# TODO scumm:` comment with the original code; for
  OpenQuest these are only the scripts that switch the controlled character
  (`VAR_EGO`).
- Some components of the generated project are taken from the Escoria demo game
  rather than from the SCUMM game: the Escoria menus (main menu, pause menu,
  options, save/load), the `icon.png` and `default_env.tres` files, and the
  base project settings (input map, autoloads).
- This converter was created with [Claude](https://claude.ai) (Anthropic).

- The generated project is a plain Godot project. The converter changes a few
  functions of Escoria's addons; it lists what it patched for each game in a
  `[scumm2escoria]` section of `project.godot`, which
  [GEJE](https://github.com/joaquin-becerra/godot-escoria-js-engine) (Godot
  Escoria JS Engine) reads to install the JS side of those patches when it
  builds the game for the browser.

- Bundles hold the game's own assets, so don't commit a bundle made from a game
  you can't redistribute.
- Only SCUMM v6 games made with ScummC have gone through the whole conversion. The
  v5 path of the bundle reader hasn't, and commercial v6 games may use opcodes
  ScummC doesn't. Multi-disk games, with a `.002` file, aren't supported.
- `# TODO scumm:` comments left: 21 from compiled and 18 from source for OpenQuest,
  14 and 20 for `road`. In `road` they include the camera pan of the intro and the door's
  `SetBoxes`: walk boxes can't change at runtime, because Escoria has one active
  navigation polygon.
- The number of TODO comments doesn't show what's missing by itself. To check a
  conversion, compare the generated `.esc` files with those of the game's source, if
  you have it.
- Only the dialogs of ScummC's own library become ASHES dialogs. A game with its own
  dialog code keeps it as plain script, or as TODO comments.
- Costume animations and classes keep numbers as names (`anim24`, `Class1`) unless
  they have a role the converter knows.
- Resource management calls (`loadRoom`-style preloads, locks, `dimArray`) are left
  out of the converted scripts: another engine has no use for them.
- Costume cels use the palette of the room that stores them, but the game can show
  them in rooms with different palettes. `palette_slots` in `game.json` has the
  costume's colour indexes for recolouring.
