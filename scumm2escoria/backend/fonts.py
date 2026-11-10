"""Convert ScummC charset images into BMFont fonts (.fnt + .png) Godot imports.

A ScummC charset image holds one red rectangle per character code, in reading
order (left to right, top to bottom, starting at code 0); the inside of each
rectangle is the glyph and its width is the glyph's advance. Rows don't always
have the same number of boxes, so codes come from the order, not a grid. Palette index 0 is transparent; the
other colors are kept, so white text can be tinted and outlines stay dark.
"""
from __future__ import annotations

from typing import Iterable

import hashlib
import json

import numpy as np
from PIL import Image

def _red_index(img: Image.Image) -> int:
    pal = img.getpalette()
    for i in range(len(pal) // 3):
        if tuple(pal[i * 3:i * 3 + 3]) == (255, 0, 0):
            return i
    raise ValueError("charset image has no red box color")


def _bands(mask: np.ndarray) -> list[tuple[int, int]]:
    """Vertical ranges of rows containing red pixels: one per glyph row."""
    rows = mask.any(axis=1)
    bands, start = [], None
    for y, on in enumerate(rows):
        if on and start is None:
            start = y
        elif not on and start is not None:
            bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, len(rows)))
    return bands


def read_glyphs(path: str) -> dict[int, tuple[np.ndarray, int]]:
    """Charset of a compiled game: a JSON list of glyph images (scumm_bin/to_ir.py)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    glyphs: dict[int, tuple[np.ndarray, int]] = {}
    for code, g in data.items():
        # A glyph advances by its width plus its x offset: a space is mostly offset.
        pad = max(g["x"], 0)
        if g["image"] is None:
            glyphs[int(code)] = (np.zeros((1, max(g["width"] + pad, 1), 4), dtype=np.uint8), 0)
            continue
        rgba = np.asarray(Image.open(g["image"]).convert("RGBA")).copy()
        # As in charset images: colour 1 is the text colour, the rest are outline.
        rgba[..., :3] = np.where(rgba[..., :1] == 1, 255, 0)
        rgba = np.pad(rgba, ((0, 0), (pad, 0), (0, 0)))
        glyphs[int(code)] = (rgba, g["y"])
    return glyphs


def read_charset(path: str) -> dict[int, tuple[np.ndarray, int]]:
    """{char code: (RGBA glyph, y offset)}."""
    if path.endswith(".json"):
        return read_glyphs(path)
    img = Image.open(path)
    idx = np.asarray(img)
    rgba = np.asarray(img.convert("RGBA")).copy()
    red = _red_index(img)
    rgba[idx == 0, 3] = 0
    # Index 1 is the text color (white here, tinted per actor). The other
    # indices go through SCUMM's charset color map, which defaults to black:
    # the image's own colors for them (e.g. a blue outline) are editor colors.
    rgba[idx >= 2, :3] = 0
    mask = idx == red
    glyphs: dict[int, tuple[np.ndarray, int]] = {}
    tops = []
    found = []
    for row, (y0, y1) in enumerate(_bands(mask)):
        band = mask[y0:y1]
        cols = band.any(axis=0)
        x = 0
        while x < len(cols):
            if not cols[x]:
                x += 1
                continue
            # A box: a run of red columns at its left edge ... right edge.
            left = x
            # Inner glyph width: the gap until the next red column of the same box.
            top = int(np.nonzero(band[:, left])[0].min()) + y0
            bottom = int(np.nonzero(band[:, left])[0].max()) + y0
            right = left + 1
            while right < len(cols) and not (mask[top, right] and mask[bottom, right] and
                                              mask[top + 1:bottom, right].all()):
                right += 1
            if right >= len(cols):
                break
            # Boxes with no inside are two adjacent red columns (left, left + 1).
            inner_x0, inner_x1 = left + 1, right
            inner_y0, inner_y1 = top + 1, bottom
            code = len(found)
            found.append((code, inner_x0, inner_x1, inner_y0, inner_y1, y0))
            tops.append(inner_y0 - y0)
            x = right + 1
    base_top = min(tops) if tops else 0
    for code, x0, x1, y0, y1, band_y in found:
        glyphs[code] = (rgba[y0:y1, x0:x1], (y0 - band_y) - base_top)
    return glyphs


def borrow_missing(glyphs: dict, others: list[str]) -> None:
    """A compiled game's charset can hold only the letters of its verbs, and object
    names drawn with it need the rest: they come from the charset closest in size."""
    height = max((g.shape[0] + off for g, off in glyphs.values()), default=1)
    charsets = [read_charset(path) for path in others]
    for charset in sorted(charsets, key=lambda c: abs(max(g.shape[0] + off for g, off in c.values()) - height)):
        for code in range(32, 127):
            if code in charset:
                glyphs.setdefault(code, charset[code])


def write_bmfont(charset_path: str, name: str, png_path: str, fnt_path: str, png_res: str, scale: int,
                 others: Iterable[str] = ()) -> int:
    """Write the font; returns its pixel size (controls must use it to draw 1:1)."""
    glyphs = read_charset(charset_path)
    if charset_path.endswith(".json"):
        borrow_missing(glyphs, [path for path in others if path != charset_path])
    line_height = max((g.shape[0] + off for g, off in glyphs.values()), default=1)
    # Pack glyphs in a simple grid atlas.
    cell_w = max((g.shape[1] for g, _ in glyphs.values()), default=1) + 1
    cell_h = line_height + 1
    cols = 16
    rows = (max(glyphs) // cols + 1) if glyphs else 1
    atlas = np.zeros((rows * cell_h, cols * cell_w, 4), dtype=np.uint8)
    lines = []
    for code, (g, off) in sorted(glyphs.items()):
        cx, cy = (code % cols) * cell_w, (code // cols) * cell_h
        h, w = g.shape[:2]
        atlas[cy:cy + h, cx:cx + w] = g
        lines.append(f"char id={code} x={cx * scale} y={cy * scale} width={w * scale} height={h * scale} "
                     f"xoffset=0 yoffset={off * scale} xadvance={w * scale} page=0 chnl=15")
    img = Image.fromarray(atlas, "RGBA")
    img.resize((img.width * scale, img.height * scale), Image.NEAREST).save(png_path)
    # Godot only reimports a .fnt when the .fnt changes, not its page image:
    # put the image's hash in it so a new image means a new .fnt.
    digest = hashlib.md5(open(png_path, "rb").read()).hexdigest()[:12]
    header = [
        f'info face="{name}-{digest}" size={line_height * scale} bold=0 italic=0 charset="" unicode=1 '
        f"stretchH=100 smooth=0 aa=1 padding=0,0,0,0 spacing=0,0",
        f"common lineHeight={line_height * scale} base={line_height * scale} "
        f"scaleW={img.width * scale} scaleH={img.height * scale} pages=1 packed=0",
        f'page id=0 file="{png_res.rsplit("/", 1)[-1]}"',
        f"chars count={len(lines)}",
    ]
    with open(fnt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(header + lines) + "\n")
    return line_height * scale
