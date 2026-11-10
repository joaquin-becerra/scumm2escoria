"""A tiny evaluator for ScummC setup scripts.

Setup scripts (verbs, inventory slots...) compute positions with loops and
arithmetic, e.g. `for(c = 0; c < INVENTORY_COL; c++) setVerbXY(216 + c*48, ...)`.
This walks the statement tree with integer variables and reports every call
with its evaluated arguments. Anything it can't evaluate is skipped.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from ..ir import Stmt, Tok

_SAFE = re.compile(r"^[\d\s+\-*/%()<>=!&|]*$")


class SetupEvaluator:
    def __init__(self, constants: dict[str, int], on_call: Callable[[str, list], None]):
        self.constants = constants        # #defines, verb ids...
        self.vars: dict[str, int] = {}
        self.on_call = on_call

    def value(self, toks: list[Tok]):
        """Evaluate an expression; strings and unknown identifiers stay as text."""
        if len(toks) == 1:
            kind, val = toks[0]
            if kind == "string":
                return val
            if kind == "ident" and val not in self.vars and val not in self.constants:
                return val
        if len(toks) == 3 and toks[1][1] == "::":
            return f"{toks[0][1]}::{toks[2][1]}"
        out = []
        for kind, val in toks:
            if kind == "number":
                out.append(str(int(val, 0)))
            elif kind == "ident":
                if val in self.vars:
                    out.append(str(self.vars[val]))
                elif val in self.constants:
                    out.append(str(self.constants[val]))
                else:
                    return None
            elif val in ("&&", "||"):
                out.append(" and " if val == "&&" else " or ")
            elif val == "!":
                out.append(" not ")
            else:
                out.append(val)
        text = "".join(out)
        if not _SAFE.match(text.replace(" and ", "&").replace(" or ", "|").replace(" not ", "!")):
            return None
        try:
            return int(eval(text, {"__builtins__": {}}, {}))   # digits and operators only
        except Exception:
            return None

    def run(self, stmts: list[Stmt], limit: int = 10000) -> None:
        self._budget = limit
        self._block(stmts)

    def _block(self, stmts: list[Stmt]) -> None:
        for st in stmts:
            self._stmt(st)

    def _stmt(self, st: Stmt) -> None:
        self._budget -= 1
        if self._budget < 0:
            return
        if st.kind in ("block", "cutscene", "label", "try"):
            self._block(st.body)
        elif st.kind == "expr":
            self._expr_list(st.expr)
        elif st.kind == "if":
            cond = self.value(st.expr)
            if cond is None:
                return
            if bool(cond) != st.negate:
                self._block(st.body)
            else:
                self._block(st.orelse)
        elif st.kind == "loop" and st.name == "for":
            init, body, step = st.body
            self._expr_list(init.expr)
            for _ in range(1000):
                cond = self.value(st.expr) if st.expr else 1
                if not cond:
                    break
                self._stmt(body)
                self._expr_list(step.expr)

    def _expr_list(self, toks: list[Tok]) -> None:
        """Comma-separated expressions at depth 0: assignments, ++/--, calls."""
        parts: list[list[Tok]] = [[]]
        depth = 0
        for t in toks:
            if t[0] == "punct" and t[1] in "([":
                depth += 1
            elif t[0] == "punct" and t[1] in ")]":
                depth -= 1
            elif t[0] == "punct" and t[1] == "," and depth == 0:
                parts.append([])
                continue
            parts[-1].append(t)
        for p in parts:
            self._expr(p)

    def _expr(self, toks: list[Tok]) -> None:
        if not toks:
            return
        vals = [v for _, v in toks]
        if len(toks) >= 3 and toks[0][0] == "ident" and vals[1] == "=":
            v = self.value(toks[2:])
            if isinstance(v, int):
                self.vars[vals[0]] = v
            return
        if len(toks) == 2 and toks[0][0] == "ident" and vals[1] in ("++", "--") and vals[0] in self.vars:
            self.vars[vals[0]] += 1 if vals[1] == "++" else -1
            return
        if toks[0][0] == "ident" and len(toks) >= 3 and vals[1] == "(" and vals[-1] == ")":
            args: list[list[Tok]] = [[]]
            depth = 0
            for t in toks[2:-1]:
                if t[0] == "punct" and t[1] in "([":
                    depth += 1
                elif t[0] == "punct" and t[1] in ")]":
                    depth -= 1
                elif t[0] == "punct" and t[1] == "," and depth == 0:
                    args.append([])
                    continue
                args[-1].append(t)
            self.on_call(vals[0], [self.value(a) for a in args if a])
