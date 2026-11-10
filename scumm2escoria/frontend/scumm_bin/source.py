"""Write the decompiled scripts of a bundle as ScummC-style source, one file per room.

Usage: python3 -m scumm2escoria.frontend.scumm_bin.source BUNDLE_DIR OUTPUT_DIR

It's meant for reading: the files have no declarations, so ScummC can't compile them.
"""
from __future__ import annotations

import os
import sys

from .to_ir import Loader

INDENT = "    "


def indented(text: str, depth: int) -> list[str]:
    """Lines of generated code, indented by their braces."""
    out = []
    for line in text.split("\n"):
        closes = line.startswith("}")
        label = line.startswith(("case ", "default:"))
        level = depth - closes - label
        out.append(INDENT * max(level, 0) + line if line else "")
        depth += line.count("{") - line.count("}") if '"' not in line else \
            line.endswith("{") - line.startswith("}")
    return out


def write_sources(loader: Loader, output: str) -> list[str]:
    os.makedirs(output, exist_ok=True)
    written = []
    for room, parts in loader.sources.items():
        lines = [f"room {room} {{"]
        for heading, text in parts:
            if not text.strip():
                continue
            head, _, comment = heading.partition(" // ")
            lines += ["", INDENT + head + " {" + (f" // {comment}" if comment else "")]
            lines += indented(text, 2)
            lines.append(INDENT + "}")
        lines.append("}")
        path = os.path.join(output, f"{room}.scc")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        written.append(path)
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip())
        return 2
    loader = Loader(argv[0])
    loader.load()
    written = write_sources(loader, argv[1])
    print(f"wrote {len(written)} files to {os.path.abspath(argv[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
