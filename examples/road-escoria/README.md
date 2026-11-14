# road-bundle (Godot + Escoria)

A [Godot](https://godotengine.org/) 4 +
[Escoria](https://github.com/godot-escoria/escoria-demo-game) version of
"road-bundle", generated automatically by **scumm2escoria** from the game's
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

- Conversion status: 14 `# TODO scumm:` comments were left in the `.esc` scripts, each with the original code that couldn't be translated.
- Some components come from the Escoria demo game rather than from the original
  game: the Escoria menus (main menu, pause menu, options, save/load), `icon.png`,
  `default_env.tres` and the base project settings (input map, autoloads).
