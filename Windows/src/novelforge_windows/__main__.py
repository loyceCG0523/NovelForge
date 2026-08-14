"""Run NovelForge Windows with ``python -m novelforge_windows``."""

from __future__ import annotations

import os
import sys

from novelforge_windows.rendering import configure_webengine_rendering


configure_webengine_rendering()

# PyInstaller windowed applications do not have console streams. Some embedded
# libraries inspect these streams while configuring logging, so provide harmless
# writable sinks before importing the desktop runtime.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from novelforge_windows.app import run


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
