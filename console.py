"""Terminal output that survives the terminals people actually have.

Two small problems, both of which silently ruin a demo.

Windows consoles default to cp1252, which cannot encode the box drawing and
arrows the demo prints, and the failure is an exception rather than a mangled
character. Reconfiguring the stream to UTF-8 fixes it everywhere and costs
nothing on platforms that were already fine.

Colour is escape codes, which are colour in a terminal and noise in a file. The
brief asks for a recorded run as a deliverable, and a transcript full of
`\\033[1m` is not a transcript. So colour turns itself off when the output is
redirected, and honours NO_COLOR.
"""
from __future__ import annotations

import os
import sys


def configure() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def colour_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("SILO_FORCE_COLOR"):
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


class Palette:
    """Escape codes, or empty strings when colour is off."""

    def __init__(self, enabled: bool) -> None:
        codes = {
            "BOLD": "\033[1m", "DIM": "\033[2m", "RESET": "\033[0m",
            "GREEN": "\033[32m", "RED": "\033[31m", "YELLOW": "\033[33m",
            "CYAN": "\033[36m", "BLUE": "\033[34m",
        }
        for name, code in codes.items():
            setattr(self, name, code if enabled else "")


def setup() -> Palette:
    configure()
    return Palette(colour_enabled())
