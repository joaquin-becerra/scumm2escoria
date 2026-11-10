"""Parse ScummC script bodies into the IR statement tree (ir.Stmt).

Expressions are not parsed: they are kept as token lists and left for the
backends to interpret.
"""
from __future__ import annotations

from ..ir import Case, Stmt, Tok
from .scummc import Token, TokenStream

DECL_TYPES = {"int", "bit", "nibble", "byte", "char", "word"}


def _tok(t: Token) -> Tok:
    return (t.kind, t.value)


def _is(t: Token | None, value: str) -> bool:
    return t is not None and t.kind != "string" and t.value == value


def source_text(tokens: list[Token]) -> str:
    out = []
    for t in tokens:
        out.append(f'"{t.value}"' if t.kind == "string" else t.value)
    text = " ".join(out)
    for a, b in ((" ( ", "("), ("( ", "("), (" )", ")"), (" ,", ","), (" ;", ";"),
                 (" ::", "::"), (":: ", "::"), (" [", "["), ("[ ", "["), (" ]", "]")):
        text = text.replace(a, b)
    return text


class StmtParser:
    def __init__(self, tokens: list[Token]):
        self.ts = TokenStream(tokens)

    # ------------------------------------------------------------- helpers

    def _paren_expr(self) -> list[Token]:
        """Tokens between a '(' (next token) and its matching ')'."""
        self.ts.expect("(")
        depth, out = 0, []
        while True:
            t = self.ts.next()
            if t.kind == "punct" and t.value == "(":
                depth += 1
            elif t.kind == "punct" and t.value == ")":
                if depth == 0:
                    return out
                depth -= 1
            out.append(t)

    def _until(self, *stops: str) -> list[Token]:
        """Tokens up to (not including) one of `stops` at nesting depth 0."""
        depth, out = 0, []
        while True:
            t = self.ts.peek()
            if t is None:
                return out
            if t.kind == "punct" and depth == 0 and t.value in stops:
                return out
            if t.kind == "ident" and depth == 0 and t.value in stops:
                return out
            if t.kind == "punct" and t.value in "([{":
                depth += 1
            elif t.kind == "punct" and t.value in ")]}":
                depth -= 1
            out.append(self.ts.next())

    # ---------------------------------------------------------- statements

    def parse_block_body(self) -> list[Stmt]:
        """Statements up to the closing '}' (consumed)."""
        stmts = []
        while not self.ts.accept("}"):
            stmts.append(self.statement())
        return stmts

    def parse_all(self) -> list[Stmt]:
        stmts = []
        while self.ts.peek() is not None:
            stmts.append(self.statement())
        return stmts

    def statement(self) -> Stmt:
        ts = self.ts
        t = ts.peek()
        line = t.line

        if _is(t, "{"):
            ts.next()
            return Stmt("block", line, body=self.parse_block_body())
        if _is(t, ";"):
            ts.next()
            return Stmt("block", line)

        if t.kind == "ident":
            kw = t.value
            if kw in ("if", "unless"):
                ts.next()
                cond = self._paren_expr()
                then = self.statement()
                orelse = None
                if _is(ts.peek(), "else"):
                    ts.next()
                    orelse = self.statement()
                return Stmt("if", line, expr=[_tok(x) for x in cond], body=[then],
                            orelse=[orelse] if orelse else [], negate=kw == "unless",
                            source=f"{kw}({source_text(cond)})")
            if kw in ("while", "until"):
                ts.next()
                cond = self._paren_expr()
                body = self.statement()
                return Stmt("loop", line, expr=[_tok(x) for x in cond], body=[body],
                            name="while", negate=kw == "until",
                            source=f"{kw}({source_text(cond)})")
            if kw == "do":
                ts.next()
                if _is(ts.peek(), "{"):
                    body = self.statement()
                else:
                    start = ts.pos
                    toks = self._until("while", "until", ";")
                    ts.accept(";")
                    body = Stmt("expr", line, expr=[_tok(x) for x in toks],
                                source=source_text(ts.tokens[start:ts.pos]))
                which = ts.next().value
                cond = self._paren_expr()
                ts.accept(";")
                return Stmt("loop", line, expr=[_tok(x) for x in cond], body=[body],
                            name="do", negate=which == "until",
                            source=f"do ... {which}({source_text(cond)})")
            if kw == "for":
                ts.next()
                ts.expect("(")
                init = self._until(";"); ts.expect(";")
                cond = self._until(";"); ts.expect(";")
                step = self._until(")"); ts.expect(")")
                body = self.statement()
                src = f"for({source_text(init)}; {source_text(cond)}; {source_text(step)})"
                return Stmt("loop", line, expr=[_tok(x) for x in cond], name="for",
                            body=[Stmt("expr", line, expr=[_tok(x) for x in init]),
                                  body,
                                  Stmt("expr", line, expr=[_tok(x) for x in step])],
                            source=src)
            if kw == "switch":
                ts.next()
                subject = self._paren_expr()
                ts.expect("{")
                return Stmt("switch", line, expr=[_tok(x) for x in subject],
                            cases=self.case_list(), source=f"switch({source_text(subject)})")
            if kw == "cutscene":
                ts.next()
                arg = self._paren_expr() if _is(ts.peek(), "(") else []
                return Stmt("cutscene", line, expr=[_tok(x) for x in arg], body=[self.statement()])
            if kw == "try":
                ts.next()
                body = self.statement()
                override = []
                if _is(ts.peek(), "override"):
                    ts.next()
                    override = [self.statement()]
                return Stmt("try", line, body=[body], orelse=override)
            if kw == "return":
                ts.next()
                val = self._until(";")
                ts.accept(";")
                return Stmt("return", line, expr=[_tok(x) for x in val],
                            source=f"return {source_text(val)}".strip())
            if kw in ("break", "continue"):
                ts.next()
                ts.accept(";")
                return Stmt(kw, line)
            if kw in DECL_TYPES:
                ts.next()
                toks = self._until(";")
                ts.accept(";")
                names = [x.value for x in toks if x.kind == "ident"]
                return Stmt("decl", line, name=kw, expr=[("ident", n) for n in names],
                            source=f"{kw} {source_text(toks)}")
            # label:  (a single ':' token, '::' is a separate token)
            nxt = ts.peek(1)
            if nxt is not None and nxt.kind == "punct" and nxt.value == ":":
                ts.next(); ts.next()
                # Decompiled code can end in a label: nothing follows it.
                last = ts.peek() is None or _is(ts.peek(), "}")
                return Stmt("label", line, name=kw,
                            body=[Stmt("block", line) if last else self.statement()])

        start = ts.pos
        toks = self._until(";", "}")
        ts.accept(";")
        return Stmt("expr", line, expr=[_tok(x) for x in toks],
                    source=source_text(ts.tokens[start:ts.pos]))

    def case_list(self) -> list[Case]:
        """`case X: ... default: ...` up to the closing '}' (consumed)."""
        cases: list[Case] = []
        while not self.ts.accept("}"):
            t = self.ts.peek()
            if _is(t, "case"):
                self.ts.next()
                label = self._until(":")
                self.ts.expect(":")
                # Consecutive labels share a body.
                if cases and not cases[-1].body:
                    cases[-1].labels.append([_tok(x) for x in label])
                else:
                    cases.append(Case([[_tok(x) for x in label]]))
            elif _is(t, "default"):
                self.ts.next()
                self.ts.expect(":")
                cases.append(Case([]))
            else:
                stmt = self.statement()
                if not cases:
                    cases.append(Case([[("ident", "__prologue__")]]))
                cases[-1].body.append(stmt)
        return cases
