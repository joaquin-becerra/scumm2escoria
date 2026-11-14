# Examples

Two Godot 4 + Escoria projects made by scumm2escoria, as the converter writes
them (nothing edited by hand). Open either folder in Godot 4.7 (Project Manager →
Import → `project.godot`) and press Play.

| Project | Converted from | Play in the browser | License of the game |
|---|---|---|---|
| [`openquest-escoria/`](openquest-escoria/) | OpenQuest, the ScummC port of Michael Sheail's demo adventure ([source](https://github.com/JenniBee/openquest)) | [Play](https://joaquin-becerra.github.io/godot-escoria-js-engine/player/?game=openquest-escoria) | [CC BY 4.0](openquest-escoria/original/LICENSE) |
| [`road-escoria/`](road-escoria/) | road, ScummC's example game ([source](https://github.com/AlbanBedel/scummc/tree/master/examples/road)) | [Play](https://joaquin-becerra.github.io/godot-escoria-js-engine/player/?game=road-escoria) | GPL v2 or later, like ScummC |

The browser versions run on
[godot-escoria-js-engine](https://github.com/joaquin-becerra/godot-escoria-js-engine),
a JavaScript engine that plays these same projects without Godot's WebAssembly
runtime ([all its demos](https://joaquin-becerra.github.io/godot-escoria-js-engine/)).

The `addons/` folders are Escoria's, copied from the
[Escoria demo game](https://github.com/godot-escoria/escoria-demo-game) (MIT).

## Updating them

After regenerating the projects (see the main [README](../README.md)), copy them
here with:

```bash
examples/update.sh [OPENQUEST_PROJECT] [ROAD_PROJECT]
```

It defaults to `../openquest-escoria` and `../road-escoria`, next to this
repository, and leaves out what Godot makes locally (`.godot/`, `build/`).
