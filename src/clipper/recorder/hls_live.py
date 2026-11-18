"""Live HLS recorder (ROADMAP 4.2): poll the media playlist, keep every new segment.

Platform specifics live in a `resolve` callable that returns a fresh master playlist URL
(Twitch: new access token; Kick: channel playback_url; YouTube: yt-dlp -g). On 403/404
of the media playlist the URL is resolved again (token expiry) and recording continues.

All times are session time (`wall - t0`, `t0` = session start), the same base the VOD
path uses (`pdt0`), so the cutter, detector and transcript code are shared.

Segments are written to `seg/<seq>.ts` (or `.mp4` for fMP4: the init section is
prepended to every fragment so each file stands alone for the concat demuxer) and to the
`segments` table with their wall-clock `pdt_start`.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from clipper.log import get_logger
from clipper.models import Segment
from clipper.recorder.m3u8 import (
    MediaPlaylist,
    MediaSegment,
    parse_master,
    parse_media,
    pick_variant,
)

log = get_logger("recorder")

Resolver = Callable[[], Awaitable[str]]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class RecorderError(Exception):
    """User-facing (Turkish)."""


class StreamOffline(RecorderError):
    pass


@dataclass
class RecorderStats:
    segments: int = 0
    ads: int = 0
    bytes: int = 0
    refreshes: int = 0
    last_pdt: float | None = None
    variant: str | None = None
    errors: list[str] = field(default_factory=list)


class LiveRecorder:
    def __init__(
        self,
        client: httpx.AsyncClient,
        conn: sqlite3.Connection,
        session_id: int,
        seg_dir: Path,
        resolve: Resolver,
        quality: list[str],
        t0: float,
        *,
        on_segment: Callable[[Segment], None] | None = None,
        offline_after: float = 120.0,
        clock: Clock = time.time,
        sleep: Sleep = asyncio.sleep,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.client = client
        self.conn = conn
        self.session_id = session_id
        self.seg_dir = seg_dir
        self.resolve = resolve
        self.quality = quality
        self.t0 = t0
        self.on_segment = on_segment
        self.offline_after = offline_after
        self.clock = clock
        self.sleep = sleep
        self.headers = headers or {}
        self.stats = RecorderStats()
        self._last_seq = self._resume_seq()
        self._init_cache: dict[str, bytes] = {}
        self._pdt_cursor: float | None = None  # fallback clock when the playlist has no PDT

    def _resume_seq(self) -> int:
        row = self.conn.execute(
            "SELECT MAX(seq) FROM segments WHERE session_id = ?", (self.session_id,)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else -1

    async def _media_url(self) -> str:
        master_url = await self.resolve()
        resp = await self.client.get(master_url, headers=self.headers, timeout=20)
        if resp.status_code in (403, 404):
            raise StreamOffline(f"yayın akışı alınamadı (HTTP {resp.status_code})")
        resp.raise_for_status()
        if "#EXT-X-STREAM-INF" not in resp.text:  # already a media playlist
            self.stats.variant = "media"
            return str(resp.url)
        variant = pick_variant(parse_master(resp.text, str(resp.url)), self.quality)
        self.stats.variant = variant.name
        return variant.url

    async def run(self, stop: asyncio.Event) -> RecorderStats:
        """Record until `stop` is set, the playlist ends, or nothing could be fetched for
        `offline_after` seconds (stream went offline)."""
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        media_url: str | None = None
        last_ok = self.clock()
        backoff = 1.0
        while not stop.is_set():
            try:
                if media_url is None:
                    media_url = await self._media_url()
                    log.info(
                        "recorder playlist",
                        extra={"session_id": self.session_id, "variant": self.stats.variant},
                    )
                resp = await self.client.get(media_url, headers=self.headers, timeout=20)
                if resp.status_code in (403, 404, 410):
                    self.stats.refreshes += 1
                    media_url = None  # token expired or variant gone: resolve again
                    raise RecorderError(f"playlist HTTP {resp.status_code}")
                resp.raise_for_status()
                pl = parse_media(resp.text, str(resp.url))
                await self._take(pl)
                last_ok = self.clock()
                backoff = 1.0
                if pl.ended:
                    log.info("recorder: playlist ended", extra={"session_id": self.session_id})
                    break
                wait = max(0.5, (pl.target_duration or 2.0) / 2)
            except (httpx.HTTPError, RecorderError, ValueError) as exc:
                msg = f"{type(exc).__name__}: {exc}"
                self.stats.errors = [*self.stats.errors[-19:], msg]
                if self.clock() - last_ok > self.offline_after:
                    log.info("recorder: offline", extra={"session_id": self.session_id})
                    break
                log.warning("recorder retry", extra={"session_id": self.session_id, "error": msg})
                wait = backoff
                backoff = min(backoff * 2, 30.0)
            await _wait(stop, wait, self.sleep)
        return self.stats

    async def _take(self, pl: MediaPlaylist) -> None:
        for seg in pl.segments:
            if seg.seq <= self._last_seq:
                continue
            await self._store(seg)
            self._last_seq = seg.seq

    async def _store(self, seg: MediaSegment) -> None:
        now = self.clock()
        pdt = seg.pdt - self.t0 if seg.pdt is not None else None
        if pdt is None:
            # No EXT-X-PROGRAM-DATE-TIME: first download time + cumulative duration.
            fallback = now - self.t0 - seg.duration
            pdt = self._pdt_cursor if self._pdt_cursor is not None else fallback
        self._pdt_cursor = pdt + seg.duration
        if seg.is_ad:
            # Ads are never cut; we keep the row (time accounting) but not the bytes.
            self.stats.ads += 1
            self._insert(seg, "", pdt, is_ad=True)
            return
        ext = ".mp4" if seg.map_url else ".ts"
        dest = self.seg_dir / f"{seg.seq}{ext}"
        data = await self._fetch(seg.url)
        if seg.map_url:
            init = self._init_cache.get(seg.map_url)
            if init is None:
                init = await self._fetch(seg.map_url)
                self._init_cache = {seg.map_url: init}
            data = init + data
        tmp = dest.with_name(dest.name + ".part")
        await asyncio.to_thread(tmp.write_bytes, data)
        tmp.replace(dest)
        self.stats.segments += 1
        self.stats.bytes += len(data)
        self.stats.last_pdt = pdt + seg.duration
        row = self._insert(seg, str(dest), pdt, is_ad=False)
        if self.on_segment is not None:
            self.on_segment(row)

    async def _fetch(self, url: str, attempts: int = 3) -> bytes:
        for attempt in range(1, attempts + 1):
            try:
                resp = await self.client.get(url, headers=self.headers, timeout=30)
                resp.raise_for_status()
                return resp.content
            except httpx.HTTPError:
                if attempt == attempts:
                    raise
                await self.sleep(0.5 * attempt)
        raise AssertionError("unreachable")

    def _insert(self, seg: MediaSegment, path: str, pdt: float, *, is_ad: bool) -> Segment:
        self.conn.execute(
            "INSERT OR IGNORE INTO segments (session_id, seq, path, pdt_start, duration, is_ad,"
            " deleted) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.session_id, seg.seq, path, pdt, seg.duration, int(is_ad), int(is_ad)),
        )
        return Segment(self.session_id, seg.seq, path, pdt, seg.duration, is_ad)


async def _wait(stop: asyncio.Event, seconds: float, sleep: Sleep) -> None:
    if seconds <= 0 or stop.is_set():
        return
    if sleep is asyncio.sleep:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)
    else:
        await sleep(seconds)


# ---- ring buffer ------------------------------------------------------------------------


def protected_ranges(conn: sqlite3.Connection, session_id: int) -> list[tuple[float, float]]:
    """Windows (session time) that pending cut jobs still need."""
    rows = conn.execute(
        "SELECT c.win_start, c.win_end FROM jobs j JOIN candidates c"
        " ON c.id = json_extract(j.payload, '$.candidate_id')"
        " WHERE j.type = 'cut' AND j.status IN ('queued', 'running') AND c.session_id = ?",
        (session_id,),
    ).fetchall()
    return [(float(r[0]), float(r[1])) for r in rows]


def prune_segments(
    conn: sqlite3.Connection,
    session_id: int,
    now: float,
    buffer_minutes: float,
    *,
    margin: float = 30.0,
) -> int:
    """Delete segment files older than the ring buffer unless a pending cut (or an open
    candidate window, protected by the caller via jobs) covers them. `now` is session
    time. Returns how many files were deleted."""
    cutoff = now - buffer_minutes * 60
    keep = [(a - margin, b + margin) for a, b in protected_ranges(conn, session_id)]
    rows = conn.execute(
        "SELECT id, path, pdt_start, duration FROM segments WHERE session_id = ? AND deleted = 0"
        " AND pdt_start + duration < ?",
        (session_id, cutoff),
    ).fetchall()
    n = 0
    for r in rows:
        a, b = float(r["pdt_start"]), float(r["pdt_start"]) + float(r["duration"])
        if any(a < kb and b > ka for ka, kb in keep):
            continue
        if r["path"]:
            Path(r["path"]).unlink(missing_ok=True)
        conn.execute("UPDATE segments SET deleted = 1 WHERE id = ?", (r["id"],))
        n += 1
    return n
