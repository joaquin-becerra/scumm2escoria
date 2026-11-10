"""SCUMM room transitions (screenEffect) for Escoria.

screenEffect(0xAABB) sets the effect for the following room changes: AA when
leaving a room (fadeOut), BB when entering the next one (fadeIn). The effects,
as ScummVM implements them (engines/scumm/gfx.cpp):

    1-6      transitionEffect(n-1): iris / box wipes, drawn in 8x8 blocks
    128      dissolve in 8x4 blocks
    129      instant (fadeOut: the screen goes black)
    130-133  scroll: the new room pushes the old one out (only fadeIn)
    134      pixel dissolve
    135      dissolve in vertical lines

Each block-ordered effect becomes a mask image: the gray level of a pixel says
when, from 0 to 1, it switches to the new picture. A generated overlay
(res://ui/scumm_screen.gd) plays them with a shader.
"""
from __future__ import annotations

import random

from PIL import Image

# ScummVM's transitionEffects table: iterations, delta table, strip table
# (four l/t/r/b rows; strip 24 means the last row of the screen).
TRANSITION_TABLE = [
    (13, [1, 1, -1, 1, -1, 1, -1, -1, 1, -1, -1, -1, 1, 1, 1, -1],
         [0, 0, 39, 0, 39, 0, 39, 24, 0, 24, 39, 24, 0, 0, 0, 24]),       # iris
    (25, [0, 1, 2, 1, 2, 0, 2, 1, 2, 0, 2, 1, 0, 0, 0, 0],
         [0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 255, 0, 0, 0]),             # box wipe from upper left
    (25, [-2, -1, 0, -1, -2, -1, -2, 0, -2, -1, -2, 0, 0, 0, 0, 0],
         [39, 24, 39, 24, 39, 24, 39, 24, 38, 24, 38, 24, 255, 0, 0, 0]),  # box wipe from lower right
    (25, [0, -1, -2, -1, -2, 0, -2, -1, -2, 0, -2, -1, 0, 0, 0, 0],
         [0, 24, 39, 24, 39, 0, 39, 24, 38, 0, 38, 24, 255, 0, 0, 0]),     # inverse box wipe
    (9, [-1, -1, 1, -1, -1, 1, 1, 1, -1, -1, -1, 1, 1, -1, 1, 1],
        [7, 7, 32, 7, 7, 8, 32, 8, 7, 8, 7, 8, 32, 7, 32, 8]),            # inverse iris
    (16, [2, 0, 2, 0, 2, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0],
         [0, 0, 0, 15, 1, 0, 1, 15, 255, 0, 0, 0, 255, 0, 0, 0]),          # horizontal wipe
]
FRAME = 1 / 60          # ScummVM shows each step for (at least) one frame
QUARTER_FRAME = 1 / 240  # waitForTimer(1)


def transition_mask(a: int, width: int, height: int) -> tuple[Image.Image, float]:
    """transitionEffect(a): the iteration in which each 8x8 block is drawn."""
    iterations, delta, strips = TRANSITION_TABLE[a]
    cols, bottom = width // 8, height // 8
    tab = [bottom - 1 if j == 24 else j for j in strips]
    when = [[iterations - 1] * cols for _ in range(bottom)]
    seen = [[False] * cols for _ in range(bottom)]

    def mark(col: int, top: int, last: int, it: int) -> None:
        for row in range(max(top, 0), min(last, bottom - 1) + 1):
            if not seen[row][col]:
                seen[row][col] = True
                when[row][col] = it

    for it in range(iterations):
        for i in range(4):
            l, t, r, b = tab[i * 4:i * 4 + 4]
            if t == b:
                while l <= r:
                    if 0 <= l < cols and t < bottom:
                        mark(l, t, b, it)
                    l += 1
            elif 0 <= l < cols and b > t:
                mark(l, t, b, it)
        tab = [x + d for x, d in zip(tab, delta)]
    img = Image.new("L", (width, height))
    for row in range(bottom):
        for col in range(cols):
            img.paste(_level(when[row][col], iterations), (col * 8, row * 8, col * 8 + 8, row * 8 + 8))
    return img, iterations * FRAME


def dissolve_mask(bw: int, bh: int, width: int, height: int) -> tuple[Image.Image, float]:
    """dissolveEffect(bw, bh): blocks in random order, shown in batches."""
    rng = random.Random(bw * 1000 + bh)
    blocks = [(x, y) for x in range(0, width, bw) for y in range(0, height, bh)]
    rng.shuffle(blocks)
    img = Image.new("L", (width, height))
    if (bw, bh) == (1, 1):
        # The screen is refreshed every w*h/(h/8) pixels.
        batches = max(height // 8, 1)
        per = -(-len(blocks) // batches)
        for i, (x, y) in enumerate(blocks):
            img.putpixel((x, y), _level(i // per, batches))
        return img, batches * FRAME
    # Other sizes stop for a quarter frame after each block whose offset
    # (y * pitch + x) is under the screen height: the top row's first ones.
    stops = sum(1 for x, y in blocks if y == 0 and x < height)
    batch = 0
    for x, y in blocks:
        img.paste(_level(batch, stops + 1), (x, y, x + bw, y + bh))
        if y == 0 and x < height:
            batch += 1
    return img, max(stops * QUARTER_FRAME, 4 * FRAME)


def _level(step: int, steps: int) -> int:
    """Gray level for "switches in step `step` of `steps`" (never 0)."""
    return max(1, round((step + 1) / steps * 255))


def effect_masks(width: int, height: int) -> dict[int, tuple[Image.Image, float]]:
    masks = {n: transition_mask(n - 1, width, height) for n in range(1, 7)}
    masks[128] = dissolve_mask(8, 4, width, height)
    masks[134] = dissolve_mask(1, 1, width, height)
    masks[135] = dissolve_mask(1, height, width, height)
    return masks


class TransitionsMixin:
    """Writes the transition overlay, its masks and the ASHES commands."""

    def screen_height(self) -> int:
        """Height of SCUMM's main virtual screen: down to the verb bar."""
        bar = next((v for v in self.game.verbs if v.image and v.pos), None)
        return bar.pos[1] if bar else 144

    def write_transitions(self) -> None:
        S = self.SCALE
        height = self.screen_height()
        durations = {}
        for n, (img, seconds) in effect_masks(320, height).items():
            img.save(self.path(f"res://ui/transitions/fx_{n}.png"))
            durations[n] = seconds
        scroll_h = 320 // 8 * (1 / 60)
        scroll_v = height // 8 * (1 / 60)
        effects = ",\n".join(
            f'\t{n}: ["res://ui/transitions/fx_{n}.png", {round(s, 3)}]' for n, s in sorted(durations.items()))
        self.write_text("res://ui/scumm_screen.gd", SCREEN_SCRIPT
                        .replace("%EFFECTS%", effects)
                        .replace("%WIDTH%", str(320 * S))
                        .replace("%HEIGHT%", str(height * S))
                        .replace("%VIEW_HEIGHT%", str(200 * S))
                        .replace("%SCROLL_H%", str(round(scroll_h, 3)))
                        .replace("%SCROLL_V%", str(round(scroll_v, 3)))
                        .replace("%STEPS_H%", str(320 // 8))
                        .replace("%STEPS_V%", str(height // 8)))
        for name, cls, args, body, doc in COMMANDS:
            self.write_text(f"res://commands/{name}.gd", COMMAND_TEMPLATE.format(
                cls=cls, args=args, body=body, doc=doc))


SCREEN_SCRIPT = '''class_name ScummScreen
extends CanvasLayer
# Generated by scumm2escoria: SCUMM room transitions (screenEffect).
# An overlay over the room area (SCUMM's main virtual screen) that keeps the
# old picture while the room changes underneath, then reveals the new one.

const GLOBAL := "scumm_effect_code"  # not the command name: ASHES would resolve it as the variable
# Effect number: [mask (when each pixel switches, 0..1), seconds]
const EFFECTS := {
%EFFECTS%
}
# Scroll effects: the direction the old picture moves, seconds, steps.
const SCROLLS := {
	130: [Vector2(1, 0), %SCROLL_H%, %STEPS_H%],
	131: [Vector2(-1, 0), %SCROLL_H%, %STEPS_H%],
	132: [Vector2(0, 1), %SCROLL_V%, %STEPS_V%],
	133: [Vector2(0, -1), %SCROLL_V%, %STEPS_V%],
}
const SIZE := Vector2(%WIDTH%, %HEIGHT%)
const VIEW_HEIGHT := %VIEW_HEIGHT%.0

const SHADER := """
shader_type canvas_item;
uniform sampler2D screen : hint_screen_texture, filter_nearest;
uniform sampler2D old_picture : filter_nearest;
uniform sampler2D mask : filter_nearest;
uniform float progress = 0.0;
uniform float area = 1.0;           // room area height / viewport height
uniform bool old_black = false;
uniform bool new_black = false;     // fading out: the new picture is black
uniform vec2 scroll = vec2(0.0);
uniform float steps = 1.0;

vec4 old_at(vec2 uv) {
	return old_black ? vec4(0.0, 0.0, 0.0, 1.0) : texture(old_picture, uv);
}
vec4 new_at(vec2 uv) {
	return new_black ? vec4(0.0, 0.0, 0.0, 1.0) : vec4(texture(screen, vec2(uv.x, uv.y * area)).rgb, 1.0);
}
void fragment() {
	if (scroll != vec2(0.0)) {
		// SCUMM scrolls 8 pixels at a time.
		vec2 q = UV - scroll * floor(progress * steps) / steps;
		if (q.x >= 0.0 && q.x <= 1.0 && q.y >= 0.0 && q.y <= 1.0)
			COLOR = old_at(q);
		else
			COLOR = new_at(q + scroll);
	} else {
		COLOR = texture(mask, UV).r <= progress ? new_at(UV) : old_at(UV);
	}
}
"""

var _rect: ColorRect
var _material: ShaderMaterial
# SCUMM's _screenEffectFlag: fadeOut only runs after a fadeIn.
var _effect_flag := true


static func get_screen() -> ScummScreen:
	var screen = escoria.get_node_or_null("ScummScreen")
	if screen == null:
		screen = ScummScreen.new()
		screen.name = "ScummScreen"
		escoria.add_child(screen)
	return screen


static func effect_code() -> int:
	if escoria.globals_manager.has(GLOBAL):
		return int(escoria.globals_manager.get_global(GLOBAL))
	return 0


func _init() -> void:
	layer = 100
	var shader := Shader.new()
	shader.code = SHADER
	_material = ShaderMaterial.new()
	_material.shader = shader
	_material.set_shader_parameter("area", SIZE.y / VIEW_HEIGHT)
	_rect = ColorRect.new()
	_rect.size = SIZE
	_rect.material = _material
	_rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect.visible = false
	add_child(_rect)


## Leaving a room: keep the current picture, then play the "out" effect.
func fade_out(from_black: bool = false) -> void:
	var effect := (effect_code() >> 8) & 0xFF
	_reset()
	if from_black:
		_material.set_shader_parameter("old_black", true)
	else:
		await RenderingServer.frame_post_draw
		var image := get_viewport().get_texture().get_image()
		var area := Rect2i(Vector2i.ZERO, Vector2i(image.get_width(), int(image.get_height() * SIZE.y / VIEW_HEIGHT)))
		_material.set_shader_parameter("old_picture", ImageTexture.create_from_image(image.get_region(area)))
	_rect.visible = true
	if _effect_flag and effect != 0 and not from_black:
		if effect == 129:
			_material.set_shader_parameter("old_black", true)
		elif EFFECTS.has(effect):
			_material.set_shader_parameter("mask", load(EFFECTS[effect][0]))
			_material.set_shader_parameter("new_black", true)
			await _play(EFFECTS[effect][1])
			_material.set_shader_parameter("old_black", true)
			_material.set_shader_parameter("new_black", false)
			_material.set_shader_parameter("progress", 0.0)
	_effect_flag = false


## Entering a room: reveal it with the "in" effect (doesn't block).
func fade_in() -> void:
	_effect_flag = true
	if not _rect.visible:
		return
	var effect := effect_code() & 0xFF
	if EFFECTS.has(effect):
		_material.set_shader_parameter("mask", load(EFFECTS[effect][0]))
		await _play(EFFECTS[effect][1])
	elif SCROLLS.has(effect):
		_material.set_shader_parameter("scroll", SCROLLS[effect][0])
		_material.set_shader_parameter("steps", float(SCROLLS[effect][2]))
		await _play(SCROLLS[effect][1])
	_rect.visible = false
	_reset()


func _play(seconds: float) -> void:
	var tween := create_tween()
	tween.tween_property(_material, "shader_parameter/progress", 1.0, seconds).from(0.0)
	await tween.finished


func _reset() -> void:
	_material.set_shader_parameter("progress", 0.0)
	_material.set_shader_parameter("old_black", false)
	_material.set_shader_parameter("new_black", false)
	_material.set_shader_parameter("scroll", Vector2.ZERO)
'''

COMMAND_TEMPLATE = '''class_name {cls}
extends ESCBaseCommand
# Generated by scumm2escoria. {doc}


func configure() -> ESCCommandArgumentDescriptor:
\treturn ESCCommandArgumentDescriptor.new({args})


func run(command_params: Array) -> int:
{body}
\treturn ESCExecution.RC_OK


func interrupt():
\tpass
'''

COMMANDS = [
    ("scumm_screen_effect", "ScummScreenEffectCommand", "1, [TYPE_INT], [null]",
     "\tescoria.globals_manager.set_global(ScummScreen.GLOBAL, command_params[0], true)",
     "`scumm_screen_effect(code)`: SCUMM's screenEffect(0xAABB), the effects\n"
     "# for the next room changes (AA leaving, BB entering)."),
    ("scumm_fade_out", "ScummFadeOutCommand", "0, [TYPE_BOOL], [false]",
     "\tawait ScummScreen.get_screen().fade_out(command_params[0])",
     "`scumm_fade_out([from_black])`: leaving a room, before change_scene.\n"
     "# from_black: the game starts from an empty screen."),
    ("scumm_fade_in", "ScummFadeInCommand", "0, [], []",
     "\tScummScreen.get_screen().fade_in()",
     "`scumm_fade_in()`: entering a room (its :setup). Doesn't block."),
]
