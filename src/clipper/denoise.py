"""DeepFilterNet noise suppression for clip audio, through the project's own prebuilt
`deep-filter` program (Rust + tract, model built in; no PyTorch). The binary is fetched
once from the GitHub release into `data/tools/` and checked by SHA-256.

`clean.wav` (48 kHz stereo, raw.mp4 timeline) replaces the clip audio in the render and
`raw.clean.wav` (16 kHz mono) goes to whisper. `atten_lim_db` mixes the original back in,
so game sound and music get quieter by at most that much instead of disappearing."""

from __future__ import annotations

import asyncio
import platform
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from clipper.config import Config
from clipper.log import get_logger
from clipper.util.download import ensure_pinned
from clipper.util.proc import no_window, stop_process

log = get_logger("denoise")

DF_VERSION = "0.5.6"
_RELEASE = f"https://github.com/Rikorose/DeepFilterNet/releases/download/v{DF_VERSION}/"
# (sys.platform, machine) -> (asset, sha256 of the release file)
ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("win32", "amd64"): (
        f"deep-filter-{DF_VERSION}-x86_64-pc-windows-msvc.exe",
        "75e11fa16445f560cb6b021521ddb89e89270d13b83089705d98776f58fd7915",
    ),
    ("linux", "x86_64"): (
        f"deep-filter-{DF_VERSION}-x86_64-unknown-linux-musl",
        "70775e251eee44c0f2451a1e833326cf8bcbbe304d3e7cd12851e6fce72ef7da",
    ),
}

CLEAN_WAV = "clean.wav"
CLEAN_16K = "raw.clean.wav"


class DenoiseError(Exception):
    pass


async def ensure_deep_filter(cfg: Config) -> Path:
    if cfg.denoise.binary is not None:
        b = Path(cfg.denoise.binary)
        found = b if b.is_file() else (Path(p) if (p := shutil.which(str(b))) else None)
        if found is None:
            raise DenoiseError(f"denoise.binary bulunamadı: {b}")
        return found
    asset = ASSETS.get((sys.platform, platform.machine().lower()))
    if asset is None:
        raise DenoiseError("bu sistem için hazır deep-filter yok; denoise.binary ile verin")
    name, sha = asset
    return await ensure_pinned(
        _RELEASE + name, sha, cfg.paths.data_dir / "tools" / name, executable=True, timeout=300
    )


async def _run(args: list[str], timeout: float, what: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        **no_window(),
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        raise DenoiseError(f"{what} zaman aşımı") from exc
    finally:
        await stop_process(proc)
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise DenoiseError(f"{what} başarısız: {' | '.join(tail)}")


async def denoise_clip(
    raw: Path, binary: Path | Sequence[str], *, ffmpeg: str = "ffmpeg", atten_lim_db: float = 12
) -> tuple[Path, Path]:
    """raw.mp4 -> (clean.wav, raw.clean.wav) next to it. `binary` may be a command prefix
    (tests run a fake deep-filter through the Python interpreter)."""
    cmd = [str(binary)] if isinstance(binary, Path) else list(binary)
    out_dir = raw.parent
    clean, clean16 = out_dir / CLEAN_WAV, out_dir / CLEAN_16K
    with tempfile.TemporaryDirectory(prefix=".df-", dir=out_dir) as tmp_s:
        tmp = Path(tmp_s)
        src = tmp / "in.wav"
        await _run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw), "-vn",
             "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(src)],
            300, "ses çıkarma",
        )  # fmt: skip
        out = tmp / "out"
        await _run(
            [*cmd, "-D", "-a", f"{atten_lim_db:g}", "-o", str(out), str(src)],
            600, "deep-filter",
        )  # fmt: skip
        produced = out / src.name
        if not produced.is_file():
            raise DenoiseError("deep-filter çıktı dosyası oluşmadı")
        await _run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(produced),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp / "16k.wav")],
            300, "16 kHz dönüştürme",
        )  # fmt: skip
        produced.replace(clean)
        (tmp / "16k.wav").replace(clean16)
    return clean, clean16
