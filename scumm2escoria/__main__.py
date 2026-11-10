"""scumm2escoria: convert a SCUMM game into a Godot 4 + Escoria project.

Usage:
    python3 -m scumm2escoria SOURCE OUTPUT_DIR [--escoria DEMO_DIR] [--ir FILE] [--rooms A,B] [-v]

SOURCE is a ScummC project (.scc files) or a bundle made by
scumm2escoria.frontend.scumm_bin.bundle (game.json).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .backend.escoria import EscoriaWriter
from .debug import report_crash
from .frontend.scumm_bin.to_ir import load_bundle
from .frontend.scummc import load_game
from .subset import keep_rooms

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ESCORIA = os.path.normpath(os.path.join(HERE, "..", "..", "escoria-demo-game"))


def convert(args) -> int:
    bundle = os.path.exists(os.path.join(args.source, "game.json"))
    game = load_bundle(args.source, args.verbose) if bundle else load_game(args.source)
    print(f"read {len(game.rooms)} rooms, {len(game.costumes)} costumes "
          f"(ego: {game.ego_costume}, start: {game.start_room})")
    if args.rooms:
        keep_rooms(game, args.rooms.split(","))
        print(f"kept {len([r for r in game.rooms.values() if r.background])} rooms, "
              f"{len(game.objects)} objects, {len(game.scripts)} scripts, {len(game.actors)} actors, "
              f"{len(game.costumes)} costumes, {len(game.sounds)} sounds (start: {game.start_room})")
    if args.ir:
        with open(args.ir, "w", encoding="utf-8") as f:
            json.dump(game.to_dict(), f, indent=1)

    EscoriaWriter(game, args.output, args.escoria).write()
    print(f"wrote Godot project to {os.path.abspath(args.output)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scumm2escoria", description=__doc__.split("\n")[0])
    ap.add_argument("source", help="ScummC project directory, or a bundle of a compiled game")
    ap.add_argument("output", help="Godot project directory to create/update")
    ap.add_argument("--escoria", default=DEFAULT_ESCORIA,
                    help="Escoria demo game checkout to take the addons from "
                         "(default: %(default)s)")
    ap.add_argument("--ir", help="also dump the intermediate representation as JSON here")
    ap.add_argument("--rooms", help="export only these rooms (comma separated) and what they use; "
                                    "the game starts in the first one")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="say what the bundle holds and each step of reading it")
    args = ap.parse_args(argv)

    if not os.path.isdir(os.path.join(args.escoria, "addons", "escoria-core")):
        ap.error(f"no Escoria addons found in {args.escoria}")

    try:
        return convert(args)
    except ValueError as exc:
        if args.rooms and "no room called" in str(exc):
            ap.error(str(exc))
        report_crash(exc)
        return 1
    except Exception as exc:
        report_crash(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
