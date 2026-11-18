"""Thin async wrappers around ffprobe/ffmpeg used by ingest and recorder code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from clipper.ffmpeg_probe import run_tool
from clipper.util.timeutil import parse_iso


class MediaError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProbeInfo:
    duration: float
    width: int | None
    height: int | None
    has_audio: bool
    creation_time: float | None


async def probe(path: Path, ffprobe: str = "ffprobe") -> ProbeInfo:
    try:
        res = await run_tool(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:format_tags=creation_time:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
            timeout=60,
        )
    except (OSError, TimeoutError) as exc:
        raise MediaError(f"ffprobe çalıştırılamadı: {exc}") from exc
    if res.returncode != 0:
        raise MediaError(f"{path} okunamadı: {res.stderr.strip()[:300]}")
    data = json.loads(res.stdout or "{}")
    fmt = data.get("format") or {}
    try:
        duration = float(fmt["duration"])
    except (KeyError, ValueError) as exc:
        raise MediaError(f"{path}: süre bilgisi yok") from exc
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    created = (fmt.get("tags") or {}).get("creation_time")
    creation_ts: float | None = None
    if created:
        try:
            creation_ts = parse_iso(created)
        except ValueError:
            creation_ts = None
    return ProbeInfo(
        duration=duration,
        width=video.get("width"),
        height=video.get("height"),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        creation_time=creation_ts,
    )


async def concat_copy(parts: list[Path], out: Path, ffmpeg: str = "ffmpeg") -> None:
    """Join media parts without re-encoding (concat demuxer, `-c copy`)."""
    if not parts:
        raise MediaError("birleştirilecek parça yok")
    out.parent.mkdir(parents=True, exist_ok=True)
    list_file = out.with_name(out.name + ".concat.txt")
    lines = [f"file '{_concat_escape(p.resolve())}'" for p in parts]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp = out.with_name(out.stem + ".part" + out.suffix)
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
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+faststart",
            str(tmp),
            timeout=6 * 3600,
        )
        if res.returncode != 0:
            raise MediaError(f"ffmpeg birleştirme başarısız: {res.stderr.strip()[:500]}")
        tmp.replace(out)
    finally:
        list_file.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


def _concat_escape(path: Path) -> str:
    # concat demuxer: single quotes are closed, escaped and reopened. Forward slashes work
    # on Windows too and avoid backslash escaping issues.
    return path.as_posix().replace("'", "'\\''")
