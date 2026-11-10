"""What to print when a conversion crashes: where it was and what it was looking at."""
from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_VALUE = 300
# Big tables that say nothing about the failure.
SKIP = {"self", "g", "n"}


def short(value) -> str:
    try:
        text = repr(value)
    except Exception as exc:
        return f"<repr failed: {exc}>"
    return text if len(text) <= MAX_VALUE else f"{text[:MAX_VALUE]}... ({len(text)} chars)"


def report_crash(exc: BaseException, frames: int = 3, out=sys.stderr) -> None:
    """The traceback, then the local variables of the innermost frames of the converter."""
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=out)
    ours = [(f, line) for f, line in traceback.walk_tb(exc.__traceback__)
            if os.path.abspath(f.f_code.co_filename).startswith(HERE)]
    print(f"\n--- state at the crash ({type(exc).__name__}: {exc})", file=out)
    for frame, line in reversed(ours[-frames:]):
        path = os.path.relpath(frame.f_code.co_filename, HERE)
        print(f"{path}:{line} in {frame.f_code.co_name}", file=out)
        for name, value in sorted(frame.f_locals.items()):
            if name not in SKIP and not name.startswith("."):
                print(f"    {name} = {short(value)}", file=out)
