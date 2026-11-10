"""Objects, inventory, verbs UI and their ASHES scripts (mixed into EscoriaWriter)."""
from __future__ import annotations

import os
import re
from typing import Optional

import numpy as np
from PIL import Image

from ..ir import Actor, Obj, Room
from .ashes import AshesTranslator, Context, action_name, snake, statements
from .godot import Raw, Resource, packed_vec2, vec2

# SCUMM object directions -> Escoria angles (0 = up, clockwise).
DIR_ANGLES = {"NORTH": 0, "EAST": 90, "SOUTH": 180, "WEST": 270}
# Verb cases that are SCUMM UI plumbing, not player actions.
INTERNAL_VERBS = {"Preposition", "InventoryObject", "Icon", "SetBoxes", "__prologue__"}
UI_ADDON = "escoria-ui-keyboard-9verbs"
INVENTORY_ROOM = "InventoryItems"


class ObjectsMixin:
    # ------------------------------------------------------------------- ids

    def assign_ids(self, rooms: list[Room]) -> None:
        """Escoria global ids for actors and objects; also the translator."""
        game = self.game
        ids: dict[str, str] = {}
        for name in game.actors:
            ids[name] = "player" if name == game.ego else snake(name)
        names = {r.name for r in rooms}
        taken = set(ids.values())
        for key, obj in game.objects.items():
            if obj.room == INVENTORY_ROOM:
                oid = f"inv_{snake(obj.name)}"
            elif obj.room in names:
                oid = snake(obj.name)
                if oid in taken:
                    oid = f"{snake(obj.room)}_{oid}"
            else:
                continue
            ids[key] = oid
            taken.add(oid)
        # Objects that stand for an actor share the actor's id.
        for actor, key in game.actor_objects.items():
            if actor in ids:
                ids[key] = ids[actor]
        self.ids = ids
        self.scenes = {r.name: f"res://rooms/{snake(r.name)}/{snake(r.name)}.tscn" for r in rooms}
        self.tr = AshesTranslator(game, ids, self.scenes, self.SCALE,
                                  anim_name=self.anim_name_for, pose_resource=self.pose_resource_for)
        self.find_inventory_icons()

    def find_inventory_icons(self) -> None:
        """Objects that go to the inventory themselves, with an icon from another one.

        Some games keep separate inventory objects (InventoryItems). Others pick up
        the object itself, and its Icon verb names the object with the picture:
        `case Icon: startScript2(vrb, [ ResRoom::axe, objA, objB ]);`
        """
        self.inventory_icons: dict[str, Obj] = {}
        for obj in self.game.objects.values():
            if obj.room == INVENTORY_ROOM or obj.key not in self.ids:
                continue
            for st in self.tr.verb_cases(obj.verbs, "Icon") or []:
                vals = [v for _, v in st.expr]
                for i in range(len(vals) - 2):
                    icon = self.game.objects.get(f"{vals[i]}::{vals[i + 2]}") if vals[i + 1] == "::" else None
                    if icon is not None and icon.states and obj.key not in self.inventory_icons:
                        self.inventory_icons[obj.key] = icon
                        self.tr.inventory_ids[obj.key] = f"inv_{self.ids[obj.key]}"

    @property
    def button_verbs(self) -> list:
        return [v for v in self.game.verbs if v.is_button]

    # --------------------------------------------------------------- scripts

    def object_script(self, obj: Obj, is_person: bool = False) -> tuple[str, bool]:
        """ASHES for an object's verbs; also whether it is an exit (handles WalkTo)."""
        classes = set(obj.classes) | ({"Person"} if is_person else set())
        lines: list[str] = []
        is_exit = False
        verbs = [v.name for v in self.button_verbs] + ["WalkTo"]
        for verb in verbs:
            ctx = Context(room=obj.room, obj=obj, obj_id=self.ids.get(obj.key),
                          classes=classes, verb=verb)
            stmts = self.tr.verb_cases(obj.verbs, verb)
            if stmts is None and verb == "PickUp" and "Pickable" in classes:
                stmts = self.tr.default_cases(verb)
            if stmts is None and verb in self.combo_verbs and action_name(verb) in self.target_actions(obj):
                # No script of its own but it takes a second object: the game's
                # default, which answers people differently.
                stmts = self.tr.default_cases(verb)
            if stmts is None and is_person and verb != "WalkTo":
                # Defaults differ for people; action_defaults.esc covers things.
                stmts = self.tr.default_cases(verb)
            if stmts is None and verb == "WalkTo":
                stmts = self.tr.object_default(obj.verbs)   # exits answer to the default entry
            if stmts is None:
                continue
            if verb == "WalkTo":
                is_exit = True
                lines += self.tr.event("exit_scene", stmts, ctx)
            elif verb in self.combo_verbs and action_name(verb) in self.target_actions(obj):
                # This object takes a second one for this verb: one event per
                # second object the script singles out, plus "any other" (_with).
                lines += self.combination_events(obj, verb, stmts, classes)
            else:
                lines += self.tr.event(action_name(verb), stmts, ctx)
        # The other side of a combination: UsedWith runs on the second object.
        used_with = self.tr.verb_cases(obj.verbs, "UsedWith")
        if used_with is not None:
            lines += self.combination_events(obj, "Use", used_with, classes, fallback=False)
        # One event per name (a verb and UsedWith may both handle "use x").
        lines = self._dedupe_events(lines)
        handled = {lbl[0][1] for c in obj.verbs for lbl in c.labels if lbl}
        pending = sorted(handled - INTERNAL_VERBS - set(verbs) - {"UsedWith"})
        if pending:
            lines.insert(0, f"# TODO scumm: verbs not converted yet: {', '.join(pending)}\n")
        return "\n".join(lines), is_exit

    @staticmethod
    def _dedupe_events(lines: list[str]) -> list[str]:
        out, seen, skip = [], set(), False
        for line in lines:
            if line.startswith(":"):
                skip = line in seen
                seen.add(line)
            if not skip:
                out.append(line)
        return out

    @property
    def combo_verbs(self) -> list[str]:
        return self.game.target_verbs or ["Use", "Give"]

    def combination_events(self, obj: Obj, verb: str, stmts, classes, fallback: bool = True) -> list[str]:
        """`:use "y"` events for each second object y the verb body names, and
        `:use_with` for any other one (only if it differs from the game default)."""
        action = action_name(verb)
        ctx = lambda b: Context(room=obj.room, obj=obj, obj_id=self.ids.get(obj.key),
                                classes=set(classes), verb=verb, objB=b)
        lines: list[str] = []
        targets = self.tr.objB_targets(stmts, ctx(None))
        if fallback:
            # People get the answers SCUMM gives for persons ("I can't use that
            # on someone!"), which a generic fallback can't tell apart.
            targets += [a for a, actor in self.game.actors.items()
                        if "Person" in actor.classes and a != self.game.ego and a not in targets
                        and a in self.ids]
        for target in targets:
            tid = self.tr.ref_id(target)
            if tid is None:
                continue
            lines += self.tr.event(f'{action} "{tid}"', stmts, ctx(target))
        if fallback:
            own = self.tr.event(f"{action}_with", stmts, ctx("*"))
            default = self.tr.event(f"{action}_with", self.tr.default_cases(verb), ctx("*"))
            if own != default:
                lines += own
        return lines

    def actor_script(self, actor: Actor) -> str:
        key = self.game.actor_objects.get(actor.name)
        obj = self.game.objects[key] if key else Obj(name=actor.name, room="")
        text, _ = self.object_script(obj, is_person="Person" in actor.classes)
        return text

    def write_action_defaults(self) -> None:
        """What happens when an object has no script for a verb (non-person objects)."""
        lines = ["# Generated from ResRoom::defaultAction", ""]
        for verb in self.button_verbs:
            ctx = Context(verb=verb.name, generic=True)
            lines += self.tr.event(action_name(verb.name), self.tr.default_cases(verb.name), ctx)
        # With a second object ("use X with Y" that nothing handles).
        for verb in self.combo_verbs:
            ctx = Context(verb=verb, objB="*", generic=True)
            lines += self.tr.event(f"{action_name(verb)}_with", self.tr.default_cases(verb), ctx)
        self.write_text("res://action_defaults.esc", "\n".join(lines))

    def scumm_data(self, thing) -> dict:
        """What the default actions ask about an object or actor at runtime
        (res://ui/scumm_defaults.gd): its SCUMM classes, and for objects the
        inventory item its InventoryObject verb returns."""
        data = {"scumm_classes": list(thing.classes), "scumm_actor": not isinstance(thing, Obj)}
        if isinstance(thing, Obj):
            ctx = Context(room=thing.key.split("::")[0], obj=thing)
            stmts = self.tr.verb_cases(thing.verbs, "InventoryObject")
            if stmts and stmts[-1].kind == "return":
                key = self.tr.obj_key(stmts[-1].expr, ctx)
                if key:
                    data["scumm_inventory_object"] = self.ids[key]
        return data

    def write_defaults_support(self) -> None:
        classes = sorted(self.tr.known_classes)
        self.write_text("res://ui/scumm_defaults.gd", """class_name ScummDefaults
# Generated by scumm2escoria. SCUMM's default action script gets objA / objB;
# Escoria's action_defaults.esc doesn't, so before an action runs (patched
# esc_action_manager.gd) its objects are published as globals:
#   scumm_object_a / scumm_object_b: global ids ("" = none)
#   scumm_a_<class> / scumm_b_<class>: their SCUMM classes (custom_data)
#   scumm_inventory_a: the inventory item objA stands for (InventoryObject)

const CLASSES := %s


static func publish(a: ESCObject, b: ESCObject) -> void:
\t_publish_one("a", a)
\t_publish_one("b", b)
\tvar inventory := ""
\tif a and is_instance_valid(a.node) and a.node.custom_data.has("scumm_inventory_object"):
\t\tinventory = a.node.custom_data["scumm_inventory_object"]
\tescoria.globals_manager.set_global("scumm_inventory_a", inventory, true)


static func _publish_one(which: String, obj: ESCObject) -> void:
\tvar classes: Array = []
\tif obj and is_instance_valid(obj.node) and "custom_data" in obj.node:
\t\tclasses = obj.node.custom_data.get("scumm_classes", [])
\tescoria.globals_manager.set_global("scumm_object_" + which, obj.global_id if obj else "", true)
\tvar state = escoria.globals_manager.get_global(obj.global_id + "_state") if obj else 0
\tescoria.globals_manager.set_global("scumm_%%s_state" %% which, state if state else 0, true)
\tvar actor: bool = obj != null and is_instance_valid(obj.node) and "custom_data" in obj.node \\
\t\t\tand obj.node.custom_data.get("scumm_actor", false)
\tescoria.globals_manager.set_global("scumm_%%s_actor" %% which, actor, true)
\tfor cls in CLASSES:
\t\tescoria.globals_manager.set_global("scumm_%%s_%%s" %% [which, CLASSES[cls]], cls in classes, true)
""" % ("{" + ", ".join(f'"{c}": "{snake(c)}"' for c in classes) + "}"))
        self.write_text("res://commands/scumm_state_var.gd", """class_name ScummStateVarCommand
extends ESCBaseCommand
# Generated by scumm2escoria. `scumm_state_var(object, n)`: the <object>_state
# variable the translated scripts test, for an object only known at runtime.


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new(2, [TYPE_STRING, TYPE_INT], [null, null])


func run(command_params: Array) -> int:
\tescoria.globals_manager.set_global(command_params[0] + "_state", command_params[1], true)
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
""")

    def has_twin(self, obj: Obj) -> bool:
        """Is another object of the room in exactly the same place?"""
        return any(o is not obj and o.room == obj.room and (o.x, o.y, o.w, o.h) == (obj.x, obj.y, obj.w, obj.h)
                   for o in self.game.objects.values())

    def linked_objects(self) -> set[str]:
        """Objects the verbs of an object in another room name, as script parameters."""
        if self._linked is None:
            numbered = self.tr.numbered_objects
            self._linked = {numbered[value] for obj in self.game.objects.values() for case in obj.verbs
                            for st in statements(case.body) for kind, value in st.expr
                            if kind == "number" and value in numbered
                            and self.game.objects[numbered[value]].room != obj.room}
        return self._linked

    def room_setup_lines(self, room: Room) -> list[str]:
        """For `:setup`: show/hide objects that depend on state, hide scripted actors."""
        lines = []
        for obj in self.room_objects(room):
            parent = self.game.objects.get(f"{room.name}::{obj.parent}") if obj.parent else None
            if parent is None or parent.key not in self.ids:
                continue
            var = self.tr.object_state_var(parent.key)
            lines += [f"\tif {var} == {obj.parent_state}:",
                      f'\t\tset_active("{self.ids[obj.key]}", true)',
                      "\telse:",
                      f'\t\tset_active("{self.ids[obj.key]}", false)']
        # Objects other rooms change, like the other side of a door.
        for obj in self.room_objects(room):
            if self.game.version < 6 and obj.key in self.linked_objects() and obj.states:
                var = self.tr.object_state_var(obj.key)
                for state in range(len(obj.states) + 1):
                    lines += [f"\tif {var} == {state}:", f'\t\tset_state("{self.ids[obj.key]}", "state_{state}")']
        # Objects already picked up stay out of the room.
        for obj in self.room_objects(room):
            item = self.tr.inventory_ids.get(obj.key)
            if item:
                lines += [f'\tif "{item}" in inventory:', f'\t\tset_interactive("{self.ids[obj.key]}", false)']
        return lines

    # --------------------------------------------------------------- objects

    def room_objects(self, room: Room) -> list[Obj]:
        actor_keys = set(self.game.actor_objects.values())
        return [o for o in self.game.objects.values()
                if o.room == room.name and o.key not in actor_keys and o.key in self.ids]

    def object_rects(self, room: Room) -> list[tuple[int, int, int, int]]:
        return [r for o in self.room_objects(room) if (r := self.object_rect(o))]

    def inventory_action(self) -> str:
        return action_name(self.game.inventory_verb) if self.game.inventory_verb else "look"

    def target_actions(self, obj: Obj) -> list[str]:
        """Verbs for which this object asks for a second one ("Use gun on ...").

        SCUMM runs the object's Preposition case for those verbs and wants a
        second object if it sets a preposition (sntcPrepo).
        """
        prep = self.tr.verb_cases(obj.verbs, "Preposition")
        actions = []
        for verb in self.game.target_verbs or ["Use", "Give"]:
            if prep and self.sets_preposition(prep, verb):
                actions.append(action_name(verb))
        return actions

    def preposition(self, obj: Obj, verb: str) -> Optional[str]:
        """The word an object's Preposition case sets for `verb` ("on", "with", "to")."""
        prep = self.tr.verb_cases(obj.verbs, "Preposition")
        ctx = Context(verb=verb)

        def walk(stmts):
            for st in stmts:
                if st.kind == "expr" and st.expr and st.expr[0][1] == "sntcPrepo":
                    strings = [v for k, v in st.expr if k == "string"]
                    return strings[0] if strings else None
                if st.kind == "if":
                    cond = self.tr.condition(st.expr, ctx)
                    if cond is not None and st.negate:
                        cond = self.tr.negate(cond)
                    branch = st.body if cond == "true" else st.orelse if cond == "false" else st.body
                    found = walk(branch)
                    if found:
                        return found
                if st.kind == "block":
                    found = walk(st.body)
                    if found:
                        return found
            return None
        return walk(prep) if prep else None

    def sets_preposition(self, stmts, verb: str) -> bool:
        ctx = Context(verb=verb)
        for st in stmts:
            if st.kind == "expr" and st.expr and st.expr[0][1] == "sntcPrepo":
                return True
            if st.kind == "if":
                cond = self.tr.condition(st.expr, ctx)
                if cond is not None and st.negate:
                    cond = self.tr.negate(cond)
                if cond == "true" and self.sets_preposition(st.body, verb):
                    return True
                if cond == "false" and self.sets_preposition(st.orelse, verb):
                    return True
                if cond not in ("true", "false") and (self.sets_preposition(st.body, verb)
                                                     or self.sets_preposition(st.orelse, verb)):
                    return True
            if st.kind == "block" and self.sets_preposition(st.body, verb):
                return True
        return False

    @staticmethod
    def object_rect(obj: Obj):
        """Area drawn by an object that changes image (any state), else None."""
        sizes = [Image.open(st.image).size for st in obj.states if st.image]
        if not sizes:
            return None
        return (obj.x, obj.y, max(w for w, _ in sizes), max(h for _, h in sizes))

    @staticmethod
    def object_size(obj: Obj) -> tuple[int, int]:
        if obj.w and obj.h:
            return obj.w, obj.h
        for st in obj.states:
            if st.image:
                return Image.open(st.image).size
        return obj.w, obj.h

    def write_objects(self, scn: Resource, room: Room, base: str, bg: Image.Image) -> None:
        scn.node("objects", "Node2D", ".")
        objects = self.room_objects(room)
        # SCUMM picks (findObject) and draws on top the object declared first.
        # Escoria highlights the highest z_index, but clicks go to whichever item
        # handles _unhandled_input first, i.e. the last in the tree: so objects get
        # z by declaration order (kept below actors, whose z is their y) and are
        # added to the scene in reverse.
        for i, obj in reversed(list(enumerate(objects))):
            self.write_object(scn, room, obj, base, bg, z=len(objects) - i)

    def write_object(self, scn: Resource, room: Room, obj: Obj, base: str, bg: Image.Image,
                     z: int = 0) -> None:
        S = self.SCALE
        oid = self.ids[obj.key]
        w, h = self.object_size(obj)
        if not (w and h):
            self.log(f"  object {obj.name}: no size, skipped")
            return
        path = f"objects/{oid}"
        script, is_exit = self.object_script(obj)
        esc_path = f"{base}/objects/{oid}.esc"
        self.write_text(esc_path, script or "# (no verbs)\n")

        # Walk-to point: per state for objects with images, else the object's own.
        state = obj.states[max(obj.state, 1) - 1] if obj.states else None
        hs = (state.hs_x, state.hs_y) if state else (obj.hs_x, obj.hs_y)
        # SCUMM walks as close as it can and runs the verb anyway; Escoria
        # cancels the action unless the player reaches the point exactly. So
        # the point goes where SCUMM would stop: the nearest walkable spot.
        if room.boxes:
            wx, wy = nearest_walkable(room.boxes, (obj.x + hs[0], obj.y + hs[1]))
            if (round(wx, 2), round(wy, 2)) != (obj.x + hs[0], obj.y + hs[1]):
                self.log(f"  {room.name}::{obj.name}: walk-to point "
                         f"({obj.x + hs[0]}, {obj.y + hs[1]}) moved into the walk boxes ({wx:.1f}, {wy:.1f})")
            hs = (wx - obj.x, wy - obj.y)

        props = {
            "process_mode": 1,
            "position": vec2(obj.x * S, obj.y * S),
            "z_index": z,
            "script": scn.ext("Script", self.ESC_ITEM),
            "global_id": oid,
            "esc_script": esc_path,
            "tooltip_name": obj.display_name or obj.name,
            "default_action": "look",
            "actions_requiring_target_object": Raw("PackedStringArray()"),
            "custom_data": self.scumm_data(obj),
        }
        if is_exit:
            props["is_exit"] = True
        if self.game.version < 6 and ("ClassUntouchable" in obj.classes or not obj.display_name):
            props["is_interactive"] = False
        if obj.dir in DIR_ANGLES:
            props["interaction_angle"] = DIR_ANGLES[obj.dir]
        if obj.states:
            props["animation_player_node"] = Raw('NodePath("animation_player")')
        scn.node(oid, "Area2D", "objects", props)

        textures = {}
        for n, st in enumerate(obj.states, start=1):
            if st.image:
                img = object_image(st.image, obj.trans)
                textures[n] = scn.ext("Texture2D", self.save_png(img, f"{base}/objects/{oid}_{n}.png"))
        if obj.states:
            sprite = {"centered": False, "visible": obj.state in textures}
            if textures:
                sprite["texture"] = textures.get(obj.state, next(iter(textures.values())))
            scn.node("sprite", "Sprite2D", path, sprite)

        scn.node("collision", "CollisionPolygon2D", path, {
            "polygon": packed_vec2([(0, 0), (w * S, 0), (w * S, h * S), (0, h * S)]),
        })
        scn.node("location", "Marker2D", path, {
            "position": vec2(hs[0] * S, hs[1] * S),
            "script": scn.ext("Script", self.ESC_LOCATION),
        })

        # What hides actors in front of this object, per state. SCUMM masks only
        # hide actors, objects are part of the background; here masks are pixels
        # drawn over actors, so they must show the object as it looks in each
        # state: the room's z-planes within the object (cut from background +
        # state image) plus the masks shipped with the state image.
        occluders: dict[int, list] = {}
        rect = self.object_rect(obj)
        if rect:
            rx, ry, rw, rh = rect
            room_planes: dict[int, np.ndarray] = {}
            for i, mpath in enumerate(room.zplanes, start=1):
                if mpath in [room.zplanes[j - 1] for j in room_planes]:
                    continue
                m = np.zeros((bg.height, bg.width), dtype=bool)
                full = np.asarray(Image.open(mpath).convert("L")) > 127
                m[ry:ry + rh, rx:rx + rw] = full[ry:ry + rh, rx:rx + rw]
                room_planes[i] = m
            for n in range(len(obj.states) + 1):
                st = obj.states[n - 1] if n else None
                pixels = np.asarray(bg).copy()
                img = None
                if st and st.image:
                    img = np.asarray(object_image(st.image, obj.trans))
                    ih, iw = min(img.shape[0], bg.height - obj.y), min(img.shape[1], bg.width - obj.x)
                    region = pixels[obj.y:obj.y + ih, obj.x:obj.x + iw]
                    opaque = img[:ih, :iw, 3] > 0     # transparent parts show the background
                    region[opaque] = img[:ih, :iw][opaque]
                masks = {k: m.copy() for k, m in room_planes.items()}
                for plane, mpath in enumerate(st.zplanes if st else [], start=1):
                    if not mpath or img is None:
                        continue
                    own = np.asarray(Image.open(mpath).convert("L")) > 127
                    m = masks.setdefault(plane, np.zeros((bg.height, bg.width), dtype=bool))
                    mh, mw = min(own.shape[0], bg.height - obj.y), min(own.shape[1], bg.width - obj.x)
                    m[obj.y:obj.y + mh, obj.x:obj.x + mw] |= own[:mh, :mw]
                parts = []
                for plane, m in sorted(masks.items()):
                    if m.any():
                        parts += list(self.occluder_strips(room, m, pixels, plane, f"plane{plane}"))
                if parts:
                    occluders[n] = parts
        if self.game.version < 6 and self.has_twin(obj):
            # The bare background in front of actors would also cover the object that
            # takes this one's place: their pictures are frames of the same animation.
            occluders.pop(0, None)
        for n, parts in occluders.items():
            group = f"{path}/occluders_{n}"
            scn.node(f"occluders_{n}", "Node2D", path, {"visible": n == obj.state})
            for name, img, (x, y), z in parts:
                scn.node(name, "Sprite2D", group, {
                    "z_index": z,
                    "z_as_relative": False,     # depth against actors is absolute
                    "position": vec2((x - obj.x) * S, (y - obj.y) * S),
                    "texture": scn.ext("Texture2D", self.save_png(img, f"{base}/objects/{oid}_{n}_{name}.png")),
                    "centered": False,
                })

        if obj.states:
            self.write_state_animations(scn, path, obj, textures, occluders)

    def write_state_animations(self, scn: Resource, path: str, obj: Obj, textures: dict, occluders: dict) -> None:
        """One animation per SCUMM state; Escoria's set_state() plays them."""
        anims = {}
        for n in range(0, len(obj.states) + 1):
            tracks = [("sprite:visible", n in textures)]
            if n in textures:
                tracks.append(("sprite:texture", textures[n]))
            for m in occluders:
                tracks.append((f"occluders_{m}:visible", m == n))
            props = {"resource_name": f"state_{n}", "length": 0.001}
            for i, (track, val) in enumerate(tracks):
                props.update({
                    f"tracks/{i}/type": "value",
                    f"tracks/{i}/imported": False,
                    f"tracks/{i}/enabled": True,
                    f"tracks/{i}/path": Raw(f'NodePath("{track}")'),
                    f"tracks/{i}/interp": 0,
                    f"tracks/{i}/loop_wrap": True,
                    f"tracks/{i}/keys": {
                        "times": Raw("PackedFloat32Array(0)"),
                        "transitions": Raw("PackedFloat32Array(1)"),
                        "update": 1,
                        "values": [val],
                    },
                })
            anims[Raw(f'&"state_{n}"')] = scn.sub("Animation", props)
        lib = scn.sub("AnimationLibrary", {"_data": anims})
        scn.node("animation_player", "AnimationPlayer", path, {"libraries": {Raw('&""'): lib}})

    # ------------------------------------------------------------- inventory

    def swapped_combinations(self, obj: Obj) -> list[str]:
        """Events for "use this on X" written in X's verb, with this object as objB.

        SCUMM's sentence script swaps the objects, so X's script answers; Escoria
        looks for the event on the item being used.
        """
        lines: list[str] = []
        for other in self.game.objects.values():
            if other.key == obj.key or other.key not in self.ids:
                continue
            for verb in self.game.target_verbs or ["Use", "Give"]:
                stmts = self.tr.verb_cases(other.verbs, verb)
                if stmts is None:
                    continue
                ctx = Context(room=other.room, obj=other, obj_id=self.ids[other.key],
                              classes=set(other.classes), verb=verb, objB=obj.key)
                if obj.key in self.tr.objB_targets(stmts, ctx):
                    lines += self.tr.event(f'{action_name(verb)} "{self.ids[other.key]}"', stmts, ctx)
        return lines

    def write_inventory(self) -> None:
        S = self.SCALE
        base = "res://items/inventory"
        items = [(o, self.ids[o.key], o) for o in self.game.objects.values()
                 if o.room == INVENTORY_ROOM and o.key in self.ids]
        items += [(self.game.objects[key], self.tr.inventory_ids[key], icon)
                  for key, icon in self.inventory_icons.items()]
        for obj, oid, picture in items:
            images = [st.image for st in picture.states if st.image]
            if not images:
                continue
            script, _ = self.object_script(obj)
            if obj.key in self.inventory_icons:
                # The same object in the inventory: its verbs, and its combinations.
                events = self.swapped_combinations(obj)
                script = "\n".join(self._dedupe_events(events + [script]))
            self.write_text(f"{base}/{oid}.esc", script or "# (no verbs)\n")
            state = picture.state
            img = Image.open(images[max(state, 1) - 1] if state <= len(images) else images[0])
            img = img.convert("RGBA")
            scn = Resource("gd_scene")
            tex = scn.ext("Texture2D", self.save_png(img, f"{base}/{oid}.png"))
            scn.node(oid, "Area2D", props={
                "process_mode": 1,
                "script": scn.ext("Script", self.ESC_ITEM),
                "global_id": oid,
                "esc_script": f"{base}/{oid}.esc",
                "tooltip_name": obj.display_name or obj.name,
                # mouseWatch: with no verb selected, inventory items get the inventory verb.
                "default_action": self.inventory_action(),
                "default_action_inventory": self.inventory_action(),
                "actions_requiring_target_object": Raw("PackedStringArray(" + ", ".join(
                    f'"{a}"' for a in self.target_actions(obj)) + ")"),
                "inventory_texture": tex,
                "inventory_texture_hovered": tex,
                # The sentence line's word between the two objects ("Use gun on ...").
                "custom_data": {**{f"preposition_{action_name(v)}": p for v in self.combo_verbs
                                   if (p := self.preposition(obj, v))}, **self.scumm_data(obj)},
            })
            scn.node("sprite", "Sprite2D", ".", {"texture": tex, "visible": False})
            scn.node("collision", "CollisionPolygon2D", ".", {
                "polygon": packed_vec2([(0, 0), (img.width * S, 0), (img.width * S, img.height * S),
                                        (0, img.height * S)]),
            })
            scn.save(self.path(f"{base}/{oid}.tscn"))
        self.log(f"inventory: {len(items)} items")

    # -------------------------------------------------------------- verbs UI

    def patch_ui_addon(self) -> None:
        """Fixes to the copied 9-verbs addon, whose scripts drive our UI scene."""
        # verbs_menu.gd connects every child as a button; skip anything else.
        gd = self.path(f"res://addons/{UI_ADDON}/verbs_menu.gd")
        text = open(gd, encoding="utf-8").read()
        text = text.replace("\tfor but in get_children():\n\t\tbut.connect(",
                            "\tfor but in get_children():\n\t\tif not but is BaseButton:\n\t\t\tcontinue\n"
                            "\t\tbut.connect(", 1)
        text = re.sub(r"\tfor but in get_children\(\):\n\t\tbut\.set_pressed\(",
                      "\tfor but in get_children():\n\t\tif but is BaseButton:\n\t\t\tbut.set_pressed(", text)
        open(gd, "w", encoding="utf-8").write(text)

        # Godot 3 leftover in the addon: Camera2D has no `current` in Godot 4 and
        # opening the pause menu crashes. The scene is hidden while paused anyway
        # (the simplemouse UI doesn't touch the camera either), so drop those lines.
        game_gd = self.path(f"res://addons/{UI_ADDON}/game.gd")
        text = open(game_gd, encoding="utf-8").read()
        text = re.sub(r"\n\t\tescoria\.object_manager\.get_object\(ESCObjectManager\.CAMERA\)\.node\.current = (true|false)",
                      "\n\t\t# (scumm2escoria) camera.current removed: not a Godot 4 property", text)
        open(game_gd, "w", encoding="utf-8").write(text)

        # SCUMM (mouseWatch): clicking an inventory item with no verb selected, or
        # with Pick up, uses the inventory verb. The 9-verbs UI did nothing.
        # (js_ports/escoria_patches.js does the same in JS: it reads js_config["patches"])
        self.js_config["patches"] = {"inventoryVerb": bool(self.game.inventory_verb), "pickup": action_name("PickUp")}
        if self.game.inventory_verb:
            game_gd = self.path(f"res://addons/{UI_ADDON}/game.gd")
            text = open(game_gd, encoding="utf-8").read()
            head = "func left_click_on_inventory_item(inventory_item_global_id: String, event: InputEvent) -> void:\n"
            text = text.replace(head, head +
                "\t# (scumm2escoria) no verb (or Pick up): use the item's inventory verb, as SCUMM does\n"
                f"\tif escoria.action_manager.current_action in [\"\", \"walk\", \"{action_name('PickUp')}\"]:\n"
                "\t\tvar item = escoria.object_manager.get_object(inventory_item_global_id).node\n"
                "\t\tif item.default_action_inventory != \"\":\n"
                "\t\t\tescoria.action_manager.set_current_action(item.default_action_inventory)\n", 1)
            open(game_gd, "w", encoding="utf-8").write(text)

        # Combinations nothing handles: SCUMM runs the first object's own fallback
        # (switch(objB) default:) or the game's default *with a second object*.
        # Escoria went straight to the single-object default: look for
        # "<action>_with" on the object, then in action_defaults.esc, first.
        am = self.path("res://addons/escoria-core/game/core-scripts/esc/esc_action_manager.gd")
        text = open(am, encoding="utf-8").read()
        old = ("\t\t\t\t\t\tif escoria.action_default_script \\\n"
               "\t\t\t\t\t\t\tand escoria.action_default_script.has_event_with_target(action):\n")
        new = ("\t\t\t\t\t\t# (scumm2escoria) combination fallbacks: the item's own, then the game's\n"
               "\t\t\t\t\t\tif target.has_event_with_target(action + \"_with\"):\n"
               "\t\t\t\t\t\t\tevent_to_return = target.get_event_with_target(action + \"_with\")\n"
               "\t\t\t\t\t\telif escoria.action_default_script \\\n"
               "\t\t\t\t\t\t\tand escoria.action_default_script.has_event_with_target(action + \"_with\"):\n"
               "\t\t\t\t\t\t\tevent_to_return = escoria.action_default_script.get_event_with_target(action + \"_with\")\n"
               "\t\t\t\t\t\telif escoria.action_default_script \\\n"
               "\t\t\t\t\t\t\tand escoria.action_default_script.has_event_with_target(action):\n")
        if old in text:
            text = text.replace(old, new, 1)
        else:
            self.log("warning: could not patch Escoria's combination fallback")
        open(am, "w", encoding="utf-8").write(text)

        # Inventory items stay registered with the room they were picked up in.
        # get_object() looks there for items in the inventory, but has() didn't,
        # so commands (set_state, ...) on them failed after changing rooms.
        om = self.path("res://addons/escoria-core/game/core-scripts/esc/esc_object_manager.gd")
        text = open(om, encoding="utf-8").read()
        old = ("\tif not _room_exists(room_key):\n\t\treturn false\n\n"
               "\treturn _object_exists_in_room(ESCObject.new(global_id, null), room_key)\n")
        new = ("\tif _room_exists(room_key) and _object_exists_in_room(ESCObject.new(global_id, null), room_key):\n"
               "\t\treturn true\n"
               "\t# (scumm2escoria) like get_object(): inventory items may be registered to another room\n"
               "\tif room == null and escoria.inventory_manager.inventory_has(global_id):\n"
               "\t\tfor single_room in room_objects:\n"
               "\t\t\tif single_room.objects.has(global_id):\n"
               "\t\t\t\treturn true\n"
               "\treturn false\n")
        if old in text:
            text = text.replace(old, new, 1)
        else:
            self.log("warning: could not patch Escoria's object_manager.has()")
        open(om, "w", encoding="utf-8").write(text)

        # Non-looping animations must stay on their last frame (SCUMM does, and so
        # does Escoria for AnimationPlayer: "keep current state"). For AnimatedSprite2D
        # it called stop(), which in Godot 4 rewinds to frame 0; pause() keeps it.
        anim_gd = self.path("res://addons/escoria-core/game/core-scripts/esc_animation_player.gd")
        text = open(anim_gd, encoding="utf-8").read()
        text = text.replace(
            "\telif not _animated_sprite.sprite_frames.get_animation_loop(name):\n\t\t_animated_sprite.stop()",
            "\telif not _animated_sprite.sprite_frames.get_animation_loop(name):\n"
            "\t\t_animated_sprite.pause()  # (scumm2escoria) stop() rewinds to frame 0", 1)
        open(anim_gd, "w", encoding="utf-8").write(text)

        # Recorded speech: Escoria calls the Godot 3 set_loop(), which no audio
        # stream has in Godot 4, so no voice could play. Do what its sound
        # effects player does instead.
        # The default actions need to know which objects an action is about.
        action_gd = self.path("res://addons/escoria-core/game/core-scripts/esc/esc_action_manager.gd")
        text = open(action_gd, encoding="utf-8").read()
        head = "\tvar event_to_return = null\n"
        if "ScummDefaults" not in text:
            text = text.replace(head, head + "\t# (scumm2escoria) objA / objB for action_defaults.esc\n"
                                "\tScummDefaults.publish(target, combine_with)\n", 1)
        open(action_gd, "w", encoding="utf-8").write(text)

        speech_gd = self.path("res://addons/escoria-core/game/scenes/sound/esc_speech_player.gd")
        text = open(speech_gd, encoding="utf-8").read()
        text = text.replace(
            "\t\tstream.stream.set_loop(false)\n",
            "\t\t# (scumm2escoria) Godot 4 streams have no set_loop()\n"
            "\t\tif stream.stream is AudioStreamWAV:\n"
            "\t\t\tstream.stream.loop_mode = AudioStreamWAV.LOOP_DISABLED\n"
            "\t\telif \"loop\" in stream.stream:\n"
            "\t\t\tstream.stream.loop = false\n", 1)
        open(speech_gd, "w", encoding="utf-8").write(text)

        # ESCLocation names EditorInterface / EditorSelection, which only exist in
        # the editor: in an exported game the script doesn't compile, every
        # location loses it and characters walk to the middle of the objects'
        # collision shapes. Look the editor up only when it's there.
        loc_gd = self.path("res://addons/escoria-core/game/core-scripts/esc_location.gd")
        text = open(loc_gd, encoding="utf-8").read()
        text = text.replace("var selection: EditorSelection = EditorInterface.get_selection()",
                            "var selection = Engine.get_singleton(\"EditorInterface\").get_selection()")
        text = text.replace("= EditorInterface.get_selection()",
                            "= Engine.get_singleton(\"EditorInterface\").get_selection()")
        if "EditorInterface." in text or "EditorSelection" in text:
            self.log("warning: could not patch Escoria's esc_location.gd for exported games")
        else:
            text = text.replace("@tool\n", "@tool\n# (scumm2escoria) EditorInterface looked up at runtime: "
                                "the class doesn't exist in exported games\n", 1)
        open(loc_gd, "w", encoding="utf-8").write(text)

    # -------------------------------------------------------------- helpers

    def occluder_strips(self, room: Room, mask: np.ndarray, pixels: np.ndarray, plane: int, prefix: str):
        """Connected regions of `mask`, cut from `pixels` in strips with their z_index."""
        from .escoria import connected_components
        for k, comp in enumerate(connected_components(mask)):
            cols = np.nonzero(comp.any(axis=0))[0]
            zs = [self.column_z(room, comp, int(x), plane, k) for x in cols]
            strips = []
            for x, z in zip(cols, zs):
                if strips and strips[-1][2] == z and strips[-1][1] == x:
                    strips[-1][1] = x + 1
                else:
                    strips.append([x, x + 1, z])
            for j, (x0, x1, z) in enumerate(strips):
                part = comp[:, x0:x1]
                ys = np.nonzero(part.any(axis=1))[0]
                y0, y1 = ys.min(), ys.max() + 1
                rgba = pixels[y0:y1, x0:x1].copy()
                rgba[..., 3] = np.where(part[y0:y1], rgba[..., 3], 0)
                name = f"{prefix}_{k}" + (f"_{j}" if len(strips) > 1 else "")
                yield name, Image.fromarray(rgba, "RGBA"), (int(x0), int(y0)), int(z)


def nearest_walkable(boxes, p: tuple[float, float], inset: float = 0.25) -> tuple[float, float]:
    """Closest point to p inside any walk box (slightly inside, for Escoria's navmesh)."""
    def inside(poly, q):
        sign = None
        for i in range(len(poly)):
            (ax, ay), (bx, by) = poly[i], poly[(i + 1) % len(poly)]
            cross = (bx - ax) * (q[1] - ay) - (by - ay) * (q[0] - ax)
            if cross != 0:
                if sign is None:
                    sign = cross > 0
                elif (cross > 0) != sign:
                    return False
        return True

    def closest_on_segment(a, b, q):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / length2))
        return (a[0] + t * dx, a[1] + t * dy)

    best, best_d = p, None
    for box in boxes:
        if box.flags & 0x80:
            continue
        poly = box.corners
        if inside(poly, p):
            return p
        for i in range(len(poly)):
            c = closest_on_segment(poly[i], poly[(i + 1) % len(poly)], p)
            d = (c[0] - p[0]) ** 2 + (c[1] - p[1]) ** 2
            if best_d is None or d < best_d:
                cx = sum(x for x, _ in poly) / len(poly)
                cy = sum(y for _, y in poly) / len(poly)
                # Nudge towards the box center so the point is inside the navmesh.
                vx, vy = cx - c[0], cy - c[1]
                norm = (vx * vx + vy * vy) ** 0.5 or 1
                best, best_d = (c[0] + vx / norm * inset, c[1] + vy / norm * inset), d
    return best


def object_image(path: str, trans: Optional[int]) -> Image.Image:
    """An object's state image; palette index `trans` (the object's `trans = n;`) is transparent."""
    img = Image.open(path)
    rgba = img.convert("RGBA")
    if trans is not None and img.mode == "P":
        alpha = np.where(np.asarray(img) == trans, 0, 255).astype(np.uint8)
        rgba.putalpha(Image.fromarray(alpha, "L"))
    return rgba
