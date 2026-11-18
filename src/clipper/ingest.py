"""Offline input: turn a video + chat into a session (pdt=0 time base)."""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from clipper import chatlog
from clipper.config import Config
from clipper.detect.events import write_events
from clipper.log import get_logger
from clipper.media import probe
from clipper.models import ChatMessage, Segment
from clipper.platforms.kick import api as kick_api
from clipper.platforms.kick import vod as kick_vod
from clipper.platforms.twitch import clips as twitch_clips
from clipper.platforms.twitch import vod as twitch_vod
from clipper.platforms.twitch.gql import TwitchGQL, TwitchGQLError
from clipper.platforms.twitch.vod_chat import iter_vod_comments
from clipper.platforms.youtube import innertube
from clipper.platforms.youtube import vod as youtube_vod
from clipper.recorder.hls_vod import download_hls_vod, first_segment_pdt
from clipper.sessions import (
    CHAT_FILE,
    PlatformName,
    add_segment,
    create_session,
    finish_session,
    get_or_create_streamer,
    write_session_info,
)

log = get_logger("ingest")


class IngestError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class IngestResult:
    session_id: int
    session_dir: Path
    duration: float
    chat_count: int
    chat_offset: float


def place_file(src: Path, dst: Path, move: bool = False) -> None:
    """Move, hardlink (same disk, no extra space) or copy `src` to `dst`."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(src, dst)
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def resolve_chat_offset(
    messages: list[ChatMessage], video_start: float | None, creation_time: float | None
) -> float:
    """Offset to subtract so that chat ts becomes seconds from video start."""
    if not chatlog.is_absolute(messages):
        return 0.0
    if video_start is not None:
        return video_start
    if creation_time is not None:
        log.info("chat offset from video creation_time", extra={"offset": creation_time})
        return creation_time
    first = min(m.ts for m in messages)
    log.warning(
        "video başlangıç zamanı bilinmiyor; ilk chat mesajı 0 kabul edildi"
        " (--video-start ile düzeltilebilir)",
        extra={"offset": first},
    )
    return first


async def ingest_local(
    conn: sqlite3.Connection,
    data_dir: Path,
    video: Path,
    chat: Path | None,
    streamer: str,
    platform: PlatformName = "twitch",
    channel: str | None = None,
    video_start: float | None = None,
    title: str | None = None,
    move: bool = False,
    ffprobe: str = "ffprobe",
    chat_messages: list[ChatMessage] | None = None,
) -> IngestResult:
    if not video.is_file():
        raise IngestError(f"video bulunamadı: {video}")
    if chat is not None and not chat.is_file():
        raise IngestError(f"chat dosyası bulunamadı: {chat}")
    info = await probe(video, ffprobe)
    if chat_messages is None:
        chat_messages = chatlog.load_any(chat) if chat is not None else []
    offset = resolve_chat_offset(chat_messages, video_start, info.creation_time)

    streamer_id = get_or_create_streamer(conn, streamer, platform, channel or streamer)
    session_id, session_dir = create_session(conn, data_dir, streamer_id, "vod", title=title)
    try:
        seg_path = session_dir / "seg" / f"0{video.suffix.lower() or '.mp4'}"
        place_file(video, seg_path, move=move)
        add_segment(
            conn,
            Segment(
                session_id=session_id,
                seq=0,
                path=str(seg_path),
                pdt_start=0.0,
                duration=info.duration,
            ),
        )
        shifted = (m for m in chatlog.shift(chat_messages, offset) if m.ts <= info.duration)
        count = chatlog.write_jsonl(session_dir / CHAT_FILE, shifted)
        write_session_info(
            session_dir,
            {
                "session_id": session_id,
                "streamer": streamer,
                "platform": platform,
                "source_video": str(video),
                "source_chat": str(chat) if chat else None,
                "duration": info.duration,
                "chat_messages": count,
                "chat_offset": offset,
                "time_base": "pdt0",
            },
        )
        finish_session(conn, session_id, "ended")
    except BaseException:
        finish_session(conn, session_id, "failed")
        raise
    log.info(
        "session ingested",
        extra={"session_id": session_id, "duration": info.duration, "chat": count},
    )
    return IngestResult(session_id, session_dir, info.duration, count, offset)


# --- Platform VODs ---------------------------------------------------------------------


def _downloads_dir(data_dir: Path, platform: str, vod_id: str) -> Path:
    path = data_dir / "downloads" / f"{platform}-{vod_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def ingest_twitch_vod(
    conn: sqlite3.Connection,
    cfg: Config,
    client: httpx.AsyncClient,
    vod_id: str,
    say: Callable[[str], None] = lambda _m: None,
) -> IngestResult:
    gql = TwitchGQL(client, cfg.twitch)
    meta = await twitch_vod.get_metadata(gql, vod_id)
    say(f"VOD: {meta.display_name} - {meta.title or '(başlıksız)'}")
    work = _downloads_dir(cfg.paths.data_dir, "twitch", vod_id)

    chat_path = work / CHAT_FILE
    if not chat_path.is_file():
        say("Chat indiriliyor...")
        messages = [
            m async for m in iter_vod_comments(gql, vod_id, length_seconds=meta.length_seconds)
        ]
        chatlog.write_jsonl(chat_path, messages)
        say(f"Chat: {len(messages)} mesaj")
    else:
        messages = list(chatlog.read_jsonl(chat_path))

    video = work / "video.mp4"
    if not video.is_file():
        say("Video indiriliyor...")
        last = [-1]

        def progress(done: int, total: int) -> None:
            pct = done * 100 // total
            if pct >= last[0] + 5:
                last[0] = pct
                say(f"  segment {done}/{total} (%{pct})")

        video = await twitch_vod.download_vod(
            client,
            cfg.twitch,
            vod_id,
            work,
            cfg.recorder.quality_order,
            cfg.paths.ffmpeg,
            progress,
        )
    result = await ingest_local(
        conn,
        cfg.paths.data_dir,
        video,
        None,
        streamer=meta.display_name,
        platform="twitch",
        channel=meta.login,
        title=meta.title,
        move=True,
        ffprobe=cfg.paths.ffprobe,
        chat_messages=messages,
    )
    chat_path.unlink(missing_ok=True)
    if meta.category:
        conn.execute(
            "UPDATE sessions SET category = ? WHERE id = ?", (meta.category, result.session_id)
        )
    if cfg.viewer_clips.enabled:
        await _store_vod_clips(conn, gql, meta, result.session_id, say)
    return result


async def _store_vod_clips(
    conn: sqlite3.Connection,
    gql: TwitchGQL,
    meta: twitch_vod.VodMetadata,
    session_id: int,
    say: Callable[[str], None],
) -> None:
    """Best effort: viewer clips are a bonus signal, a GQL hiccup must not fail ingest."""
    created = datetime.fromtimestamp(meta.created_at, UTC) if meta.created_at else None
    try:
        clips = await twitch_clips.vod_clips(
            gql, meta.login, meta.vod_id, vod_created=created, say=say
        )
    except (TwitchGQLError, httpx.HTTPError) as exc:
        log.warning("vod clips failed", extra={"vod_id": meta.vod_id, "error": str(exc)})
        say(f"İzleyici klipleri alınamadı ({exc}); chat ve ses ile devam ediliyor.")
        return
    write_events(conn, session_id, clips)


def make_kick_api(cfg: Config, client: httpx.AsyncClient) -> kick_api.KickAPI:
    getter = (
        kick_api.curl_cffi_getter(cfg.kick)
        if cfg.kick.http_backend == "curl_cffi"
        else kick_api.httpx_getter(client, cfg.kick)
    )
    return kick_api.KickAPI(getter, cfg.kick)


async def ingest_kick_vod(
    conn: sqlite3.Connection,
    cfg: Config,
    client: httpx.AsyncClient,
    uuid: str,
    say: Callable[[str], None] = lambda _m: None,
) -> IngestResult:
    api = make_kick_api(cfg, client)
    meta = kick_vod.parse_video(await api.video(uuid))
    if meta.is_live or meta.duration <= 0:
        raise IngestError(f"Kick VOD {uuid} hâlâ canlı ya da süresi yok; yayın bitince dene.")
    say(f"VOD: {meta.display_name} - {meta.title or '(başlıksız)'}")
    work = _downloads_dir(cfg.paths.data_dir, "kick", uuid)

    # Chat times are aligned to the first video segment's wall-clock time.
    t0 = await first_segment_pdt(client, meta.source, cfg.recorder.quality_order)
    t0 = t0 if t0 is not None else meta.start_time

    chat_path = work / CHAT_FILE
    if not chat_path.is_file():
        say("Chat indiriliyor (her istek ~5 sn'lik pencere)...")
        last = [-1]

        def chat_progress(pos: float) -> None:
            pct = int(pos * 100 / meta.duration) if meta.duration else 100
            if pct >= last[0] + 10:
                last[0] = pct
                say(f"  chat %{pct}")

        messages = [m async for m in kick_vod.iter_vod_chat(api, meta, t0, chat_progress)]
        chatlog.write_jsonl(chat_path, messages)
        say(f"Chat: {len(messages)} mesaj")
    else:
        messages = list(chatlog.read_jsonl(chat_path))

    video = work / "video.mp4"
    if not video.is_file():
        say("Video indiriliyor...")
        last_v = [-1]

        def progress(done: int, total: int) -> None:
            pct = done * 100 // total
            if pct >= last_v[0] + 5:
                last_v[0] = pct
                say(f"  segment {done}/{total} (%{pct})")

        res = await download_hls_vod(
            client,
            meta.source,
            work,
            cfg.recorder.quality_order,
            concurrency=cfg.kick.download_concurrency,
            ffmpeg=cfg.paths.ffmpeg,
            progress=progress,
            forbidden_message=f"Kick VOD {uuid} erişimi reddedildi (abonelere özel olabilir).",
        )
        video = res.video
    result = await ingest_local(
        conn,
        cfg.paths.data_dir,
        video,
        None,
        streamer=meta.display_name,
        platform="kick",
        channel=meta.slug or meta.display_name,
        title=meta.title,
        move=True,
        ffprobe=cfg.paths.ffprobe,
        chat_messages=messages,
    )
    if meta.category:
        conn.execute(
            "UPDATE sessions SET category = ? WHERE id = ?", (meta.category, result.session_id)
        )
    chat_path.unlink(missing_ok=True)
    return result


async def ingest_youtube_vod(
    conn: sqlite3.Connection,
    cfg: Config,
    client: httpx.AsyncClient,
    video_id: str,
    say: Callable[[str], None] = lambda _m: None,
) -> IngestResult:
    page = await innertube.fetch_watch_page(client, cfg.youtube, video_id)
    say(f"Video: {page.author or '?'} - {page.title or '(başlıksız)'}")
    work = _downloads_dir(cfg.paths.data_dir, "youtube", video_id)

    chat_path = work / CHAT_FILE
    if not chat_path.is_file():
        say("Chat tekrarı indiriliyor...")
        messages = [m async for m in innertube.iter_chat_replay(client, cfg.youtube, page)]
        chatlog.write_jsonl(chat_path, messages)
        say(f"Chat: {len(messages)} mesaj")
    else:
        messages = list(chatlog.read_jsonl(chat_path))

    video = work / "video.mp4"
    if not video.is_file():
        say("Video yt-dlp ile indiriliyor...")
        video = await youtube_vod.download_video(cfg.youtube, video_id, work, cfg.paths.ffmpeg)
    result = await ingest_local(
        conn,
        cfg.paths.data_dir,
        video,
        None,
        streamer=page.author or video_id,
        platform="youtube",
        channel=page.channel_id or video_id,
        title=page.title,
        move=True,
        ffprobe=cfg.paths.ffprobe,
        chat_messages=messages,
    )
    chat_path.unlink(missing_ok=True)
    return result
