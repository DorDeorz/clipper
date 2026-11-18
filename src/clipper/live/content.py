"""Live content path (ROADMAP 4.3b): LiveTranscriber + ContentScanner as session tasks.

- LiveTranscriber: every `chunk_seconds` of recorded audio (+ `overlap_seconds` before
  it) goes to faster-whisper; words already written (overlap) are dropped by time. Clip
  transcripts (`transcribe` jobs) go first; if the live transcript falls more than
  `max_lag_seconds` behind, old audio is skipped.
- ContentScanner: every `every_seconds` the last `window_seconds` of transcript go to
  Haiku; hits become `content` drafts that merge with reactive candidates.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from clipper.content.scanner import ContentAccumulator, ContentScanner, record_scan
from clipper.content.transcript import (
    TRANSCRIPT_FILE,
    TLine,
    append_lines,
    from_whisper,
    read_lines,
)
from clipper.ffmpeg_probe import run_tool
from clipper.log import get_logger
from clipper.media import MediaError, _concat_escape
from clipper.recorder.cutter import segments_for

if TYPE_CHECKING:
    from clipper.live.session import LiveSession
    from clipper.transcribe.whisper import Transcriber

log = get_logger("live.content")


def recorded_until(conn: sqlite3.Connection, session_id: int) -> float:
    row = conn.execute(
        "SELECT MAX(pdt_start + duration) FROM segments WHERE session_id = ? AND is_ad = 0",
        (session_id,),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def clip_jobs_waiting(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE type = 'transcribe' AND status IN ('queued', 'running')"
    ).fetchone()
    return int(row[0]) > 0


async def extract_range(
    conn: sqlite3.Connection, session_id: int, a: float, b: float, wav: Path, ffmpeg: str
) -> float:
    """16 kHz mono WAV of session time [a, b]; returns the session time of its 0."""
    segs = segments_for(conn, session_id, a, b)
    if not segs:
        raise MediaError(f"[{a:.0f}, {b:.0f}] için segment yok")
    first = float(segs[0]["pdt_start"])
    start = max(a, first)
    wav.parent.mkdir(parents=True, exist_ok=True)
    lst = wav.with_suffix(".txt")
    lst.write_text(
        "".join(f"file '{_concat_escape(Path(s['path']).resolve())}'\n" for s in segs),
        encoding="utf-8",
    )
    try:
        res = await run_tool(
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(lst),
            "-ss", f"{start - first:.3f}", "-t", f"{b - start:.3f}",
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
            timeout=300,
        )  # fmt: skip
        if res.returncode != 0:
            raise MediaError(f"ses çıkarılamadı: {res.stderr.strip()[:300]}")
    finally:
        lst.unlink(missing_ok=True)
    return start


def dedupe(lines: list[TLine], after: float) -> list[TLine]:
    """Keep only words that start after `after` (the overlap was already written)."""
    out: list[TLine] = []
    for ln in lines:
        words = tuple(w for w in ln.words if w.s >= after - 0.05)
        if not words:
            continue
        text = " ".join(w.w.strip() for w in words).strip()
        out.append(TLine(words[0].s, words[-1].e, text, words))
    return out


def live_transcriber_task(
    transcriber: Transcriber, *, poll: float = 5.0
) -> Callable[[LiveSession, asyncio.Event], Awaitable[None]]:
    async def run(sess: LiveSession, stop: asyncio.Event) -> None:
        cfg = sess.cfg.live_transcribe
        assert sess.session_id is not None and sess.session_dir is not None
        out = sess.session_dir / TRANSCRIPT_FILE
        work = sess.session_dir / "work"
        cursor = 0.0  # session time up to which the transcript is written
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), poll)
            have = recorded_until(sess.conn, sess.session_id)
            if have - cursor < cfg.chunk_seconds:
                continue
            if have - cursor > cfg.max_lag_seconds + cfg.chunk_seconds:
                skipped = have - cfg.chunk_seconds - cursor
                log.info("live transcript behind, skipping", extra={"seconds": round(skipped)})
                cursor = have - cfg.chunk_seconds
            if clip_jobs_waiting(sess.conn):
                continue  # clip transcripts first (ROADMAP 4.3b priority rule)
            a = max(0.0, cursor - cfg.overlap_seconds)
            b = cursor + cfg.chunk_seconds
            wav = work / "live_chunk.wav"
            try:
                origin = await extract_range(
                    sess.conn, sess.session_id, a, b, wav, sess.cfg.paths.ffmpeg
                )
                segs = await transcriber.transcribe(wav, work / "live_chunk.json")
            except Exception as exc:
                log.warning("live transcribe failed", extra={"error": str(exc)})
                cursor = b  # do not get stuck on a bad chunk
                continue
            finally:
                wav.unlink(missing_ok=True)
            lines = dedupe(from_whisper(segs, offset=origin), cursor)
            append_lines(out, lines)
            cursor = b

    return run


def live_scanner_task(
    make_scanner: Callable[[LiveSession], ContentScanner],
) -> Callable[[LiveSession, asyncio.Event], Awaitable[None]]:
    async def run(sess: LiveSession, stop: asyncio.Event) -> None:
        cfg = sess.cfg.content_scan
        assert sess.session_id is not None and sess.session_dir is not None
        scanner = make_scanner(sess)
        acc = ContentAccumulator(cfg, sess.dcfg.threshold)
        path = sess.session_dir / TRANSCRIPT_FILE
        last_end = 0.0
        try:
            while not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), cfg.every_seconds / 4)
                lines = read_lines(path)
                if not lines:
                    continue
                end = lines[-1].t1
                if end - last_end < cfg.every_seconds:
                    continue
                a = max(0.0, end - cfg.window_seconds)
                recent = [ln for ln in lines if ln.t1 >= a - 5]
                try:
                    o = await scanner.scan(recent, list(sess.recent), a, end)
                except Exception as exc:
                    log.warning("live content scan failed", extra={"error": str(exc)})
                    last_end = end
                    continue
                record_scan(sess.conn, sess.session_id, o)
                for d in acc.feed(o):
                    sess.submit(d)
                last_end = end
        finally:
            for d in acc.flush():
                sess.submit(d)

    return run
