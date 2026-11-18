"""Render command builder + runner with progress parsing (ROADMAP 4.6)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from clipper.config import RenderConfig
from clipper.ffmpeg_probe import nvenc_works, run_tool, which
from clipper.log import get_logger
from clipper.util.proc import no_window

log = get_logger("render")
_ENCODER_CACHE: dict[str, str] = {}


class RenderError(Exception):
    pass


def encoder_args(encoder: str, fps: int = 30) -> list[str]:
    common = ["-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    if encoder == "h264_nvenc":
        video = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23", "-b:v", "0"]
    else:
        video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
    return [*video, *common, "-movflags", "+faststart"]


async def pick_encoder(cfg: RenderConfig, ffmpeg: str = "ffmpeg") -> str:
    """`auto`: h264_nvenc if a tiny trial encode works, else libx264 (cached per ffmpeg)."""
    if cfg.encoder != "auto":
        return cfg.encoder
    if ffmpeg in _ENCODER_CACHE:
        return _ENCODER_CACHE[ffmpeg]
    path = which(ffmpeg)
    enc = "h264_nvenc" if path is not None and await nvenc_works(path) else "libx264"
    _ENCODER_CACHE[ffmpeg] = enc
    log.info("render encoder", extra={"encoder": enc})
    return enc


def build_render_args(
    ffmpeg: str,
    raw: Path,
    start: float,
    end: float,
    graph: str,
    out: Path | str,
    encoder: str,
    fps: int = 30,
) -> list[str]:
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        "-progress", "pipe:1",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(raw),
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        *encoder_args(encoder, fps),
        str(out),
    ]  # fmt: skip


def parse_progress(line: str) -> float | None:
    """Seconds of output written, from an ffmpeg `-progress` line."""
    key, _, val = line.strip().partition("=")
    if key == "out_time_us":
        try:
            return int(val) / 1_000_000
        except ValueError:
            return None
    return None


async def run_render(
    args: list[str],
    cwd: Path,
    duration: float,
    progress: Callable[[float], None] | None = None,
    timeout: float = 1800,
) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **no_window(),
    )
    assert proc.stdout is not None and proc.stderr is not None

    async def read_progress() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            t = parse_progress(raw.decode("utf-8", "replace"))
            if t is not None and progress is not None and duration > 0:
                progress(min(1.0, t / duration))

    try:
        _, err = await asyncio.wait_for(
            asyncio.gather(read_progress(), proc.stderr.read()), timeout=timeout
        )
        await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RenderError("render zaman aşımına uğradı") from None
    if proc.returncode != 0:
        raise RenderError(f"render başarısız: {err.decode('utf-8', 'replace').strip()[:600]}")


async def thumbnail(ffmpeg: str, video: Path, at: float, out: Path) -> Path:
    res = await run_tool(
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(at, 0.0):.3f}", "-i", str(video),
        "-frames:v", "1", "-q:v", "3", str(out),
        timeout=120,
    )  # fmt: skip
    if res.returncode != 0 or not out.is_file():
        raise RenderError(f"thumbnail üretilemedi: {res.stderr.strip()[:300]}")
    return out


def install_fonts(fonts_src: Path | None, work: Path) -> str | None:
    """Copy fonts next to the render (relative fontsdir). None if no font is available,
    then libass falls back to a system font via fontconfig."""
    if fonts_src is None or not fonts_src.is_dir():
        return None
    fonts = [p for p in fonts_src.iterdir() if p.suffix.lower() in (".ttf", ".otf")]
    if not fonts:
        return None
    dst = work / "fonts"
    dst.mkdir(parents=True, exist_ok=True)
    for f in fonts:
        target = dst / f.name
        if not target.is_file():
            shutil.copy2(f, target)
    return "fonts"


@dataclass(frozen=True, slots=True)
class RenderResult:
    final: Path
    thumb: Path
    duration: float
    encoder: str
