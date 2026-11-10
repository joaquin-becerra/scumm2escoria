"""Recognise a game's "roaming" library: an actor wandering about on its own.

SCUMM has no such feature; games write it. OpenQuest's (actors.scc) is a
background script `roam(a)` that loops while a per-actor state is above
"stopped", waits while it is "paused", now and then walks the actor to a random
point inside a rectangle, then waits a random delay. Small scripts start it
(storing the rectangle), pause, resume and stop it by writing that state.

ASHES can't run such a loop in the background, so this reads what the scripts
do (which fields hold the rectangle, the delay range, the chance of walking,
which state each control script writes) and the backend reproduces it with a
generated runtime node.
"""
from __future__ import annotations

from typing import Optional

from ..ir import Game, RoamLib, Stmt, Tok


def _walk(stmts: list[Stmt]):
    for st in stmts:
        yield st
        yield from _walk(st.body)
        yield from _walk(st.orelse)
        for case in st.cases:
            yield from _walk(case.body)


def _vals(toks: list[Tok]) -> list[str]:
    return [v for _, v in toks]


def _array_fields(vals: list[str], index: str) -> list[tuple[int, str, str]]:
    """(position, array, field) for every `array[index, field]` in vals."""
    out = []
    for i in range(len(vals) - 5):
        if vals[i + 1] == "[" and vals[i + 2] == index and vals[i + 3] == "," and vals[i + 5] == "]":
            out.append((i, vals[i], vals[i + 4]))
    return out


def _number(text: str, defines: dict[str, int]) -> Optional[int]:
    if text in defines:
        return defines[text]
    try:
        return int(text, 0)
    except ValueError:
        return None


def find_roaming(game: Game) -> Optional[RoamLib]:
    defines = game.defines
    for key, script in game.scripts.items():
        lib = _roam_script(key, script, defines)
        if lib:
            _control_scripts(game, lib, defines)
            return lib if lib.start else None
    return None


def _roam_script(key, script, defines) -> Optional[RoamLib]:
    if not script.params:
        return None
    a = script.params[0]
    loops = [st for st in _walk(script.body) if st.kind == "loop" and st.name == "while"]
    for loop in loops:
        cond = _vals(loop.expr)
        state = _array_fields(cond, a)
        if len(state) != 1 or cond[state[0][0] + 6:state[0][0] + 7] != [">"]:
            continue
        array, state_field = state[0][1], state[0][2]
        lib = RoamLib(script=key, array=array, state_field=state_field)
        lib.stopped = _number(cond[state[0][0] + 7], defines)
        dest: dict[str, tuple[str, str]] = {}
        for st in _walk(loop.body):
            vals = _vals(st.expr)
            # until(... array[a, state] > PAUSED): waits while paused
            if st.kind == "loop" and st.name == "while" and st.negate:
                for pos, arr, fld in _array_fields(vals, a):
                    if arr == array and fld == state_field and vals[pos + 6:pos + 7] == [">"]:
                        lib.paused = _number(vals[pos + 7], defines)
            # dst = getRandomNumberRange(array[a, F1], array[a, F2])
            if st.kind == "expr" and len(vals) > 3 and vals[1] == "=" and vals[2] == "getRandomNumberRange":
                fields = [f for _, arr, f in _array_fields(vals, a) if arr == array]
                if len(fields) == 2:
                    dest[vals[0]] = (fields[0], fields[1])
            # delay(getRandomNumberRange(lo, hi))
            if st.kind == "expr" and vals[:4] == ["delay", "(", "getRandomNumberRange", "("]:
                lo, hi = _number(vals[4], defines), _number(vals[6], defines)
                if lo is not None and hi is not None:
                    lib.delay = (lo, hi)
            # switch(event) with event = getRandomNumber(n): the walk's chance
            if st.kind == "expr" and len(vals) >= 5 and vals[1:4] == ["=", "getRandomNumber", "("]:
                n = _number(vals[4], defines)
                if n is not None:
                    lib.choices[vals[0]] = n + 1
            if st.kind == "switch" and len(st.expr) == 1:
                for case in st.cases:
                    if any(_vals(s.expr)[:1] == ["walkActorTo"] for s in _walk(case.body)):
                        lib.walk_cases[st.expr[0][1]] = lib.walk_cases.get(st.expr[0][1], 0) + max(len(case.labels), 1)
            if st.kind == "expr" and vals[:3] == ["walkActorTo", "(", a] and len(vals) >= 8:
                xs, ys = dest.get(vals[4]), dest.get(vals[6])
                if xs and ys:
                    lib.fields = [xs[0], xs[1], ys[0], ys[1]]
        if lib.fields and lib.delay and lib.stopped is not None:
            lib.chance = 1.0
            for var, n in lib.choices.items():
                if var in lib.walk_cases:
                    lib.chance = lib.walk_cases[var] / n
            return lib
    return None


def _control_scripts(game: Game, lib: RoamLib, defines) -> None:
    """The scripts that start (startScript(roam)), pause, resume and stop it."""
    roam_name = lib.script.split("::")[-1]
    for key, script in game.scripts.items():
        if key == lib.script or not script.params:
            continue
        a = script.params[0]
        starts = False
        writes: list[int] = []
        bounds: dict[str, int] = {}
        for st in _walk(script.body):
            vals = _vals(st.expr)
            if st.kind != "expr":
                continue
            if vals[:1] == ["startScript"] and roam_name in vals:
                starts = True
            # array[a, field] = value (several per statement: `x = 1; y = 2;`)
            for pos, arr, fld in _array_fields(vals, a):
                if arr != lib.array or vals[pos + 6:pos + 7] != ["="] or pos + 7 >= len(vals):
                    continue
                value = vals[pos + 7]
                if fld == lib.state_field:
                    n = _number(value, defines)
                    if n is not None:
                        writes.append(n)
                elif fld in lib.fields and value in script.params:
                    bounds[fld] = script.params.index(value)
        if starts and all(f in bounds for f in lib.fields):
            lib.start = key
            lib.start_params = [bounds[f] for f in lib.fields]
        elif not starts and len(set(writes)) == 1:
            value = writes[0]
            if value == lib.stopped:
                lib.stop = key
            elif value == lib.paused:
                lib.pause = key
            else:
                lib.resume = key
