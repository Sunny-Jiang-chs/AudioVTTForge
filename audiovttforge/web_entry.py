"""PyInstaller entry point for the packaged AudioVTTForge browser service.

Kept as a plain script (not ``python -m audiovttforge.web``) because PyInstaller
needs a real entry file.  It runs as a console build on purpose: the console
window is how the user stops the local service, exactly like ``start_web.bat``.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audiovttforge.web import main as web_main


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Double-clicking the packaged EXE should land in the browser; passing either
    # flag explicitly lets scripts and headless checks keep control.
    if not any(flag in arguments for flag in ("--open-browser", "--no-open-browser")):
        arguments.append("--open-browser")
    return web_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
