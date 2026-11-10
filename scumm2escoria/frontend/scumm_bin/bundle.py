"""Export a compiled SCUMM v5/v6 game as a bundle: game.json plus its assets.

Usage: python3 -m scumm2escoria.frontend.scumm_bin.bundle GAME_DIR OUTPUT_DIR [STEM]

The bundle has everything the converter needs and no knowledge of any particular
game: images as PNG, sounds as WAV, and every script in one common instruction
form (see script.py), with a readable listing in scripts/.
"""
from __future__ import annotations

import json
import os
import struct
import sys

import numpy as np
from PIL import Image

from . import script_v5, script_v6
from .charset import parse_charset
from .container import Game, open_game
from .costume import parse_costume
from .image import decode_smap, decode_zplane, mask_to_png, palette_of, to_png
from .rooms import parse_boxes, parse_objects
from .script import format_instr
from .sound import find_pcm, write_wav


class Exporter:
    def __init__(self, game: Game, out: str):
        self.game, self.out = game, out
        self.version = game.index.version
        self.dis = script_v6 if self.version >= 6 else script_v5
        self.listing: list[str] = []
        self.problems: list[str] = []
        # Rooms with no real image only store objects and costumes, which are
        # shown elsewhere: their palette is empty or partial, so they use the
        # palette of the first room with a real image.
        rooms = sorted(game.room_numbers().items())
        self.default_palette = next((palette_of(lflf.find("ROOM")) for _, lflf in rooms
                                     if not self.is_storage(lflf)), None)

    @staticmethod
    def is_storage(lflf) -> bool:
        width, height = struct.unpack_from("<HH", lflf.find("ROOM").find("RMHD").payload, 0)
        return width <= 8 or height <= 8

    def palette_for(self, lflf) -> bytes:
        return self.default_palette if self.is_storage(lflf) else palette_of(lflf.find("ROOM"))

    def path(self, *parts: str) -> str:
        full = os.path.join(self.out, *parts)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        return full

    def code(self, label: str, data: bytes) -> list[dict]:
        self.listing.append(f"\n; {label}")
        try:
            instrs = self.dis.disassemble(data)
        except (ValueError, IndexError, KeyError, struct.error) as exc:
            self.problems.append(f"{label}: {exc}")
            self.listing.append(f"; FAILED: {exc}")
            return []
        self.listing += [format_instr(i) for i in instrs]
        return [i.to_dict() for i in instrs]

    def image_states(self, image_block, width: int, height: int, palette: bytes,
                     transparent: int, stem: str) -> list[dict]:
        states = []
        for block in image_block.children:
            if not block.tag.startswith("IM") or block.tag == "IMHD":
                continue
            smap = block.find("SMAP")
            if smap is None or not width or not height:
                continue
            pixels, flags = decode_smap(smap, width, height)
            strip_clear = np.repeat(np.array(flags), 8)[None, :]
            name = f"{stem}_{block.tag.lower()}"
            to_png(pixels, palette, self.path(name + ".png"),
                   (pixels == transparent) & strip_clear)
            planes = []
            for z in block.children:
                if z.tag.startswith("ZP"):
                    planes.append(f"{name}_{z.tag.lower()}.png")
                    mask_to_png(decode_zplane(z, width, height), self.path(planes[-1]))
            states.append({"image": name + ".png", "zplanes": planes})
        return states

    def room(self, number: int, lflf) -> dict:
        room = lflf.find("ROOM")
        width, height, _ = struct.unpack_from("<HHH", room.find("RMHD").payload, 0)
        palette = self.palette_for(lflf)
        transparent = struct.unpack_from("<H", room.find("TRNS").payload, 0)[0] & 0xFF
        stem = f"rooms/{number:03d}"
        pixels, _ = decode_smap(room.find("RMIM").find("IM00").find("SMAP"), width, height)
        to_png(pixels, palette, self.path(stem + ".png"))
        planes = []
        for block in room.find("RMIM").find("IM00").children:
            if block.tag.startswith("ZP"):
                planes.append(f"{stem}_{block.tag.lower()}.png")
                mask_to_png(decode_zplane(block, width, height), self.path(planes[-1]))
        objects = []
        for o in parse_objects(room, self.version):
            label = f"room {number} object {o.number} ({o.name})"
            states = []
            if o.image_block is not None:
                states = self.image_states(o.image_block, o.width, o.height, palette,
                                           transparent, f"objects/{number:03d}_{o.number}")
            objects.append({"number": o.number, "name": o.name, "x": o.x, "y": o.y,
                            "width": o.width, "height": o.height, "parent": o.parent,
                            "walk_x": o.walk_x, "walk_y": o.walk_y, "direction": o.direction,
                            "hotspots": o.hotspots, "parent_state": o.parent_state,
                            "states": states, "verbs": o.entries,
                            "code": self.code(label, o.code)})
        local = {}
        for block in room.find_all("LSCR"):
            local[block.payload[0]] = self.code(f"room {number} local script {block.payload[0]}",
                                                block.payload[1:])
        boxes = [{"corners": b.corners, "mask": b.mask, "flags": b.flags, "scale": b.scale}
                 for b in parse_boxes(room)]
        scale = room.find("SCAL")
        return {"number": number, "name": self.game.index.room_names.get(number, ""),
                "width": width, "height": height, "image": stem + ".png", "zplanes": planes,
                "palette": palette.hex(), "transparent": transparent, "boxes": boxes,
                "box_matrix": room.find("BOXM").payload.hex(),
                "scale_slots": scale.payload.hex() if scale else "",
                "entry": self.code(f"room {number} entry", room.find("ENCD").payload),
                "exit": self.code(f"room {number} exit", room.find("EXCD").payload),
                "objects": objects, "local_scripts": local}

    def costume(self, number: int, block, palette: bytes) -> dict:
        c = parse_costume(number, block)
        slots = np.frombuffer(c.palette, dtype=np.uint8)
        cels: dict[str, dict] = {}
        for anim in c.anims.values():
            for step in anim:
                if step.start is None:
                    continue
                for command in c.commands[step.start:step.start + step.length + 1]:
                    key = f"{step.limb}_{command}"
                    if command >= 0x71 or key in cels:
                        continue
                    cel = c.cel(step.limb, command)
                    if cel is None or not cel.width or not cel.height:
                        continue
                    name = f"costumes/{number:03d}/{key}.png"
                    to_png(slots[cel.pixels], palette, self.path(name), cel.pixels == 0)
                    cels[key] = {"image": name, "rel_x": cel.rel_x, "rel_y": cel.rel_y,
                                 "move_x": cel.move_x, "move_y": cel.move_y}
        anims = {n: [{"limb": s.limb, "start": s.start, "length": s.length, "loop": s.loop}
                     for s in steps] for n, steps in c.anims.items()}
        return {"number": number, "mirror": c.mirror, "palette_slots": c.palette.hex(),
                "commands": c.commands.hex(), "anims": anims, "cels": cels}

    def charset(self, number: int, block) -> dict:
        c = parse_charset(number, block)
        glyphs = {}
        for code, g in c.glyphs.items():
            if not g.width or not g.height:
                glyphs[code] = {"width": g.width, "height": g.height, "x": g.x_offset,
                                "y": g.y_offset, "image": None}
                continue
            name = f"charsets/{number}/{code}.png"
            gray = np.zeros((256, 3), dtype=np.uint8)
            gray[:] = np.arange(256)[:, None]
            to_png(g.pixels, gray.tobytes(), self.path(name), g.pixels == 0)
            glyphs[code] = {"width": g.width, "height": g.height, "x": g.x_offset,
                            "y": g.y_offset, "image": name}
        return {"number": number, "height": c.height, "bits": c.bits,
                "colors": c.colors.hex(), "glyphs": glyphs}

    def sound(self, number: int, block) -> dict:
        pcm = find_pcm(block.payload)
        if pcm:
            name = f"sounds/{number:03d}.wav"
            write_wav(self.path(name), pcm[0], pcm[1])
            return {"number": number, "file": name, "kind": "pcm", "rate": pcm[0]}
        name = f"sounds/{number:03d}.bin"
        with open(self.path(name), "wb") as f:
            f.write(block.payload)
        return {"number": number, "file": name, "kind": "raw"}

    def export(self) -> dict:
        g, index = self.game, self.game.index
        rooms = {n: self.room(n, lflf) for n, lflf in sorted(g.room_numbers().items())}
        scripts = {n: self.code(f"script {n}", b.payload)
                   for n, b in sorted(g.resources(index.scripts, "SCRP").items())}
        palettes = {n: self.palette_for(lflf) for n, lflf in g.room_numbers().items()}
        costumes = {}
        for n, block in sorted(g.resources(index.costumes, "COST").items()):
            palette = palettes.get(index.costumes[n].room, next(iter(palettes.values())))
            try:
                costumes[n] = self.costume(n, block, palette)
            except (ValueError, IndexError, struct.error) as exc:
                self.problems.append(f"costume {n}: {exc}")
        charsets = {n: self.charset(n, b)
                    for n, b in sorted(g.resources(index.charsets, "CHAR").items())}
        sounds = {n: self.sound(n, b)
                  for n, b in sorted(g.resources(index.sounds, "SOUN").items())}
        def owners(directory):
            return {n: e.room for n, e in enumerate(directory) if e.room}

        data = {"version": self.version, "limits": index.max_vars,
                "owners": {"scripts": owners(index.scripts), "costumes": owners(index.costumes),
                           "sounds": owners(index.sounds), "charsets": owners(index.charsets)},
                "object_table": [{"owner": o, "state": s, "classes": c}
                                 for o, s, c in index.objects],
                "rooms": rooms, "scripts": scripts, "costumes": costumes,
                "charsets": charsets, "sounds": sounds, "problems": self.problems}
        # An indexed image of the palette, for colours given as palette indexes.
        with_palette = Image.new("P", (1, 1))
        with_palette.putpalette(self.default_palette)
        with_palette.save(self.path("palette.png"))
        with open(self.path("game.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        with open(self.path("scripts", "listing.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(self.listing))
        return data


def main(argv: list[str]) -> int:
    source, output = argv[0], argv[1]
    data = Exporter(open_game(source, argv[2] if len(argv) > 2 else None), output).export()
    objects = sum(len(r["objects"]) for r in data["rooms"].values())
    print(f"SCUMM v{data['version']}: {len(data['rooms'])} rooms, {objects} objects, "
          f"{len(data['scripts'])} scripts, {len(data['costumes'])} costumes, "
          f"{len(data['charsets'])} charsets, {len(data['sounds'])} sounds")
    for problem in data["problems"]:
        print("  problem:", problem)
    return 1 if data["problems"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
