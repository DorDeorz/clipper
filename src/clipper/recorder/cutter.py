"""Cut a wall-clock interval out of recorded segments: raw.mp4 (stream copy) + raw.wav."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from clipper.ffmpeg_probe import run_tool
from clipper.media import MediaError, _concat_escape


@dataclass(frozen=True, slots=True)
class CutResult:
    raw_mp4: Path
    raw_wav: Path
    t0: float  # session time of raw.mp4's 0
    duration: float


def segments_for(
    conn: sqlite3.Connection, session_id: int, t0: float, t1: float
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT path, pdt_start, duration FROM segments WHERE session_id = ? AND deleted = 0"
        " AND is_ad = 0 AND pdt_start < ? AND pdt_start + duration > ? ORDER BY seq",
        (session_id, t1, t0),
    ).fetchall()


async def cut(
    conn: sqlite3.Connection,
    session_id: int,
    t0: float,
    t1: float,
    out_dir: Path,
    ffmpeg: str = "ffmpeg",
) -> CutResult:
    segs = segments_for(conn, session_id, t0, t1)
    if not segs:
        raise MediaError(f"[{t0:.1f}, {t1:.1f}] aralığı için segment yok (silinmiş olabilir)")
    out_dir.mkdir(parents=True, exist_ok=True)
    first = float(segs[0]["pdt_start"])
    start = max(t0, first)
    offset = start - first
    duration = min(t1, float(segs[-1]["pdt_start"]) + float(segs[-1]["duration"])) - start
    list_file = out_dir / "segments.txt"
    list_file.write_text(
        "".join(f"file '{_concat_escape(Path(s['path']).resolve())}'\n" for s in segs),
        encoding="utf-8",
    )
    raw_mp4 = out_dir / "raw.mp4"
    raw_wav = out_dir / "raw.wav"
    try:
        res = await run_tool(
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-ss",
            f"{offset:.3f}",
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(raw_mp4),
            timeout=600,
        )
        if res.returncode != 0:
            raise MediaError(f"kesim başarısız: {res.stderr.strip()[:400]}")
        res = await run_tool(
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(raw_mp4),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(raw_wav),
            timeout=600,
        )
        if res.returncode != 0:
            raise MediaError(f"ses çıkarılamadı: {res.stderr.strip()[:400]}")
    finally:
        list_file.unlink(missing_ok=True)
    return CutResult(raw_mp4, raw_wav, start, duration)
