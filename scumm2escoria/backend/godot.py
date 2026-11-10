"""Small helpers to write Godot 4 text resources (.tscn / .tres)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Raw:
    """A value written verbatim, e.g. ExtResource("1") or Vector2(1, 2)."""
    text: str

    def __str__(self) -> str:
        return self.text


def vec2(x: float, y: float) -> Raw:
    return Raw(f"Vector2({_num(x)}, {_num(y)})")


def rect2(x: float, y: float, w: float, h: float) -> Raw:
    return Raw(f"Rect2({_num(x)}, {_num(y)}, {_num(w)}, {_num(h)})")


def packed_vec2(points) -> Raw:
    return Raw("PackedVector2Array(" + ", ".join(f"{_num(x)}, {_num(y)}" for x, y in points) + ")")


def _num(v: float) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def value(v) -> str:
    if isinstance(v, Raw):
        return v.text
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _num(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(value(i) for i in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{\n" + ",\n".join(f"{value(k)}: {value(i)}" for k, i in v.items()) + "\n}"
    raise TypeError(f"can't serialize {v!r}")


class Resource:
    """Builds a .tscn or .tres file: ext/sub resources, then nodes or [resource]."""

    def __init__(self, kind: str, script_class: str | None = None, res_type: str = "Resource"):
        self.kind = kind                  # "gd_scene" or "gd_resource"
        self.script_class = script_class
        self.res_type = res_type
        self._ext: dict[tuple[str, str], str] = {}
        self._subs: list[str] = []
        self._body: list[str] = []

    def ext(self, type_: str, path: str) -> Raw:
        key = (type_, path)
        if key not in self._ext:
            self._ext[key] = str(len(self._ext) + 1)
        return Raw(f'ExtResource("{self._ext[key]}")')

    def sub(self, type_: str, props: dict) -> Raw:
        sid = f"{type_}_{len(self._subs) + 1}"
        self._subs.append(self._section(f'sub_resource type="{type_}" id="{sid}"', props))
        return Raw(f'SubResource("{sid}")')

    def node(self, name: str, type_: str | None = None, parent: str | None = None,
             props: dict | None = None, instance: Raw | None = None) -> None:
        head = f'node name="{name}"'
        if type_:
            head += f' type="{type_}"'
        if parent is not None:
            head += f' parent="{parent}"'
        if instance is not None:
            head += f" instance={instance}"
        self._body.append(self._section(head, props or {}))

    def resource(self, props: dict) -> None:
        self._body.append(self._section("resource", props))

    @staticmethod
    def _section(head: str, props: dict) -> str:
        lines = [f"[{head}]"]
        lines += [f"{k} = {value(v)}" for k, v in props.items()]
        return "\n".join(lines)

    def render(self) -> str:
        head = f"[{self.kind}"
        if self.kind == "gd_resource":
            head += f' type="{self.res_type}"'
            if self.script_class:
                head += f' script_class="{self.script_class}"'
        head += " format=3]"
        parts = [head]
        if self._ext:
            parts.append("\n".join(f'[ext_resource type="{t}" path="{p}" id="{i}"]'
                                   for (t, p), i in self._ext.items()))
        parts += self._subs
        parts += self._body
        return "\n\n".join(parts) + "\n"

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.render())
