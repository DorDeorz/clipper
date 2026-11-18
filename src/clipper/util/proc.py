"""Subprocess helpers shared by every place that starts an external tool."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def no_window() -> dict[str, Any]:
    """Keyword args that keep a child process from opening a console window.

    Under pythonw (the desktop shortcut) there is no console to inherit, so on Windows
    every ffmpeg/ffprobe/powershell child would otherwise pop up its own cmd window.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
