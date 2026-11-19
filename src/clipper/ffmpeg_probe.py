"""Discover ffmpeg/ffprobe and their capabilities (version, libass, encoders)."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from clipper.util.proc import no_window, stop_process


@dataclass(frozen=True, slots=True)
class ToolResult:
    returncode: int
    stdout: str
    stderr: str


async def run_tool(*args: str, timeout: float = 20.0) -> ToolResult:
    """Run an executable without a shell and capture its output."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **no_window()
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    finally:
        await stop_process(proc)  # timeout or cancel: no orphan ffmpeg (audit B-06)
    return ToolResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
    )


def which(executable: str) -> Path | None:
    """Resolve a configured executable (bare name on PATH or explicit path)."""
    candidate = Path(executable)
    if candidate.parent != Path(".") and candidate.is_file():
        return candidate
    found = shutil.which(executable)
    return Path(found) if found else None


_VERSION_RE = re.compile(r"version\s+(\S+)")


def parse_version(banner: str) -> str | None:
    m = _VERSION_RE.search(banner.splitlines()[0] if banner else "")
    return m.group(1) if m else None


def parse_encoders(output: str) -> set[str]:
    """Parse `ffmpeg -hide_banner -encoders` output into encoder names."""
    names: set[str] = set()
    started = False
    for line in output.splitlines():
        if line.strip().startswith("------"):
            started = True
            continue
        if not started:
            continue
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6:
            names.add(parts[1])
    return names


def parse_filters(output: str) -> set[str]:
    """Parse `ffmpeg -hide_banner -filters` output into filter names."""
    names: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        # Lines look like: " ... subtitles         V->V       Render text subtitles ..."
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return names


@dataclass(slots=True)
class FfmpegInfo:
    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    version: str | None = None
    ffprobe_version: str | None = None
    has_libass: bool = False
    encoders: set[str] = field(default_factory=set)
    error: str | None = None

    @property
    def has_nvenc(self) -> bool:
        return "h264_nvenc" in self.encoders

    @property
    def has_libx264(self) -> bool:
        return "libx264" in self.encoders


async def probe_ffmpeg(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> FfmpegInfo:
    info = FfmpegInfo(ffmpeg=which(ffmpeg), ffprobe=which(ffprobe))
    if info.ffmpeg is None:
        info.error = f"ffmpeg bulunamadı ({ffmpeg})"
        return info
    try:
        ver = await run_tool(str(info.ffmpeg), "-hide_banner", "-version")
        info.version = parse_version(ver.stdout)
        enc = await run_tool(str(info.ffmpeg), "-hide_banner", "-encoders")
        info.encoders = parse_encoders(enc.stdout)
        flt = await run_tool(str(info.ffmpeg), "-hide_banner", "-filters")
        filters = parse_filters(flt.stdout)
        info.has_libass = "subtitles" in filters and "ass" in filters
        if info.ffprobe is not None:
            pver = await run_tool(str(info.ffprobe), "-hide_banner", "-version")
            info.ffprobe_version = parse_version(pver.stdout)
    except (OSError, TimeoutError) as exc:
        info.error = f"ffmpeg çalıştırılamadı: {exc}"
    return info


async def nvenc_works(ffmpeg: Path) -> bool:
    """`h264_nvenc` may be compiled in without a usable GPU; try a tiny encode."""
    try:
        res = await run_tool(
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=256x256:d=0.1",
            "-c:v",
            "h264_nvenc",
            "-f",
            "null",
            "-",
            timeout=30,
        )
    except (OSError, TimeoutError):
        return False
    return res.returncode == 0
