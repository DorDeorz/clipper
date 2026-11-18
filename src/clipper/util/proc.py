"""Subprocess helpers shared by every spawn site.

On Windows the panel runs under pythonw.exe (no console). Each console program it starts
(ffmpeg, ffprobe, powershell, schtasks) would then flash its own console window, so every
spawn passes CREATE_NO_WINDOW there.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def no_window() -> dict[str, Any]:
    """Extra kwargs for subprocess/asyncio spawns: hide the console window on Windows."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}
