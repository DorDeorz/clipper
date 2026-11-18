"""Download a finished HLS VOD (master playlist URL) to one mp4. Shared by Twitch and Kick."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from clipper.ffmpeg_probe import run_tool
from clipper.log import get_logger
from clipper.media import MediaError, concat_copy
from clipper.recorder.m3u8 import MediaSegment, parse_master, parse_media, pick_variant

log = get_logger("hls_vod")
Progress = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class HlsVod:
    video: Path
    variant: str
    first_pdt: float | None  # EXT-X-PROGRAM-DATE-TIME of the first segment (unix s)


async def first_segment_pdt(
    client: httpx.AsyncClient, master_url: str, quality: list[str]
) -> float | None:
    """Wall-clock time of the VOD's first segment, without downloading video."""
    master = await client.get(master_url, timeout=30)
    master.raise_for_status()
    variant = pick_variant(parse_master(master.text, str(master.url)), quality)
    media = await client.get(variant.url, timeout=30)
    media.raise_for_status()
    pl = parse_media(media.text, str(media.url))
    return pl.segments[0].pdt if pl.segments else None


async def download_hls_vod(
    client: httpx.AsyncClient,
    master_url: str | httpx.URL,
    out_dir: Path,
    quality: list[str],
    *,
    concurrency: int = 6,
    ffmpeg: str = "ffmpeg",
    progress: Progress | None = None,
    forbidden_message: str = "VOD erişimi reddedildi (HTTP 403).",
) -> HlsVod:
    """Download every segment of the chosen variant into `out_dir/parts/` (resumable),
    then stream-copy them into `out_dir/video.mp4`."""
    master_resp = await client.get(master_url, timeout=30)
    if master_resp.status_code == 403:
        raise MediaError(forbidden_message)
    master_resp.raise_for_status()
    variant = pick_variant(parse_master(master_resp.text, str(master_resp.url)), quality)
    log.info("vod variant", extra={"variant": variant.name})
    media_resp = await client.get(variant.url, timeout=30)
    media_resp.raise_for_status()
    try:
        playlist = parse_media(media_resp.text, str(media_resp.url))
    except ValueError as exc:
        raise MediaError(f"VOD playlist'i okunamadı: {exc}") from exc
    if not playlist.segments:
        raise MediaError("VOD playlist'i boş.")

    parts_dir = out_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0
    total = len(playlist.segments)
    paths: list[Path] = []

    async def fetch(url: str, dest: Path) -> None:
        nonlocal done
        async with sem:
            if not dest.exists():
                await _download_file(client, url, dest)
            done += 1
            if progress is not None:
                progress(done, total)

    # fMP4/CMAF playlists (EXT-X-MAP) need their init section in front of the fragments.
    maps = list(dict.fromkeys(s.map_url for s in playlist.segments if s.map_url))
    map_paths = {u: parts_dir / f"init-{i}{_ext(u, '.mp4')}" for i, u in enumerate(maps)}
    tasks = [fetch(u, p) for u, p in map_paths.items()]
    for seg in playlist.segments:
        dest = parts_dir / f"{seg.seq:06d}{_ext(seg.url, '.ts')}"
        paths.append(dest)
        tasks.append(fetch(seg.url, dest))
    total += len(map_paths)
    await asyncio.gather(*tasks)

    video = out_dir / "video.mp4"
    if maps:
        joined = out_dir / "joined.mp4"
        await asyncio.to_thread(_join_fmp4, playlist.segments, paths, map_paths, joined)
        try:
            await _remux(joined, video, ffmpeg)
        finally:
            joined.unlink(missing_ok=True)
    else:
        await concat_copy(paths, video, ffmpeg)
    for p in [*paths, *map_paths.values()]:
        p.unlink(missing_ok=True)
    parts_dir.rmdir()
    return HlsVod(video, variant.name, playlist.segments[0].pdt)


def _ext(url: str, default: str) -> str:
    suffix = Path(httpx.URL(url).path).suffix.lower()
    return suffix if suffix in {".ts", ".mp4", ".m4s", ".m4v", ".aac"} else default


def _join_fmp4(
    segments: list[MediaSegment], paths: list[Path], maps: dict[str, Path], out: Path
) -> None:
    """Binary-append init section(s) and fragments in playlist order (blocking)."""
    current: str | None = None
    with out.open("wb") as dst:
        for seg, part in zip(segments, paths, strict=True):
            if seg.map_url and seg.map_url != current:
                current = seg.map_url
                with maps[current].open("rb") as src:
                    shutil.copyfileobj(src, dst, 1 << 20)
            with part.open("rb") as src:
                shutil.copyfileobj(src, dst, 1 << 20)


async def _remux(src: Path, out: Path, ffmpeg: str) -> None:
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    try:
        res = await run_tool(
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(tmp),
            timeout=6 * 3600,
        )
        if res.returncode != 0:
            raise MediaError(f"ffmpeg fMP4 birleştirme başarısız: {res.stderr.strip()[:500]}")
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)


async def _download_file(
    client: httpx.AsyncClient, url: str, dest: Path, attempts: int = 4
) -> None:
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(1, attempts + 1):
        try:
            async with client.stream("GET", url, timeout=60) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        fh.write(chunk)
            tmp.replace(dest)
            return
        except httpx.HTTPError:
            tmp.unlink(missing_ok=True)
            if attempt == attempts:
                raise
            await asyncio.sleep(min(2**attempt, 30))
