"""Audio loudness per second (dBFS) from recorded segments, via ffmpeg -> PCM -> numpy."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
SILENCE_DB = -90.0


def rms_dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return SILENCE_DB
    x = samples.astype(np.float64) / 32768.0
    rms = math.sqrt(float(np.mean(x * x)))
    return max(20 * math.log10(rms), SILENCE_DB) if rms > 0 else SILENCE_DB


async def segment_db(path: Path, pdt_start: float, ffmpeg: str = "ffmpeg") -> dict[int, float]:
    """dBFS per whole second (keyed by floor(pdt_start + offset)) for one segment.

    Streams PCM one second at a time, so long VODs never sit in memory.
    """
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "s16le",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    out: dict[int, float] = {}
    chunk_bytes = SAMPLE_RATE * 2
    # Align bins to wall-clock seconds: the first partial second is pdt_start's fraction.
    first = int((math.ceil(pdt_start) - pdt_start) * SAMPLE_RATE) * 2 or chunk_bytes
    buf = b""
    sec = math.floor(pdt_start)
    want = first
    while True:
        data = await proc.stdout.read(65536)
        if not data:
            break
        buf += data
        while len(buf) >= want:
            out[sec] = rms_dbfs(np.frombuffer(buf[:want], dtype="<i2"))
            buf = buf[want:]
            sec += 1
            want = chunk_bytes
    if len(buf) >= 2 * SAMPLE_RATE // 10:  # keep a trailing partial second if >= 0.1 s
        out[sec] = rms_dbfs(np.frombuffer(buf[: len(buf) // 2 * 2], dtype="<i2"))
    await proc.wait()
    return out


async def segments_db(
    segments: list[tuple[Path, float]], ffmpeg: str = "ffmpeg", concurrency: int = 2
) -> dict[int, float]:
    sem = asyncio.Semaphore(concurrency)

    async def one(p: Path, pdt: float) -> dict[int, float]:
        async with sem:
            return await segment_db(p, pdt, ffmpeg)

    merged: dict[int, float] = {}
    for part in await asyncio.gather(*(one(p, t) for p, t in segments)):
        merged.update(part)
    return merged
