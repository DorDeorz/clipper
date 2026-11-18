"""Twitch VOD downloader (own code): GQL access token -> usher m3u8 -> segments -> mp4."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from clipper.config import TwitchConfig
from clipper.log import get_logger
from clipper.media import concat_copy
from clipper.platforms.twitch.gql import GQLOperation, TwitchGQL, TwitchGQLError
from clipper.recorder.m3u8 import parse_master, parse_media, pick_variant
from clipper.util.timeutil import parse_iso

log = get_logger("twitch.vod")

PLAYBACK_TOKEN_QUERY = """query PlaybackAccessToken($login: String!, $isLive: Boolean!, \
$vodID: ID!, $isVod: Boolean!, $playerType: String!) {
  streamPlaybackAccessToken(channelName: $login, params: {platform: "web", \
playerBackend: "mediaplayer", playerType: $playerType}) @include(if: $isLive) {
    value
    signature
  }
  videoPlaybackAccessToken(id: $vodID, params: {platform: "web", \
playerBackend: "mediaplayer", playerType: $playerType}) @include(if: $isVod) {
    value
    signature
  }
}"""

VIDEO_METADATA_QUERY = """query VideoMetadata($id: ID!) {
  video(id: $id) {
    id
    title
    createdAt
    lengthSeconds
    owner { id login displayName }
    game { name }
  }
}"""

Progress = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class VodMetadata:
    vod_id: str
    title: str | None
    created_at: float | None
    length_seconds: int | None
    login: str
    display_name: str
    category: str | None


async def get_metadata(gql: TwitchGQL, vod_id: str) -> VodMetadata:
    data = await gql.run(GQLOperation("VideoMetadata", {"id": vod_id}, VIDEO_METADATA_QUERY))
    video = data.get("video")
    if not video:
        raise TwitchGQLError(f"Twitch VOD {vod_id} bulunamadı (silinmiş ya da sub-only olabilir).")
    owner = video.get("owner") or {}
    created = video.get("createdAt")
    return VodMetadata(
        vod_id=vod_id,
        title=video.get("title"),
        created_at=parse_iso(created) if created else None,
        length_seconds=video.get("lengthSeconds"),
        login=str(owner.get("login") or "unknown"),
        display_name=str(owner.get("displayName") or owner.get("login") or "unknown"),
        category=(video.get("game") or {}).get("name"),
    )


async def get_vod_token(gql: TwitchGQL, vod_id: str) -> tuple[str, str]:
    data = await gql.run(
        GQLOperation(
            "PlaybackAccessToken",
            {"login": "", "isLive": False, "vodID": vod_id, "isVod": True, "playerType": "embed"},
            PLAYBACK_TOKEN_QUERY,
        )
    )
    tok = data.get("videoPlaybackAccessToken")
    if not tok or not tok.get("value") or not tok.get("signature"):
        raise TwitchGQLError(f"Twitch VOD {vod_id} için erişim token'ı alınamadı.")
    return str(tok["value"]), str(tok["signature"])


def usher_vod_url(cfg: TwitchConfig, vod_id: str, token: str, sig: str) -> httpx.URL:
    return httpx.URL(
        cfg.usher_vod_url.format(vod_id=vod_id),
        params={
            "nauth": token,
            "nauthsig": sig,
            "allow_source": "true",
            "allow_audio_only": "true",
            "player": "twitchweb",
            "p": str(random.randint(100000, 999999)),
        },
    )


async def download_vod(
    client: httpx.AsyncClient,
    cfg: TwitchConfig,
    vod_id: str,
    out_dir: Path,
    quality: list[str],
    ffmpeg: str = "ffmpeg",
    progress: Progress | None = None,
) -> Path:
    """Download a VOD to `out_dir/video.mp4`. Segments are kept in `out_dir/parts/`
    until the concat succeeds, so an interrupted download resumes."""
    gql = TwitchGQL(client, cfg)
    token, sig = await get_vod_token(gql, vod_id)
    master_resp = await client.get(usher_vod_url(cfg, vod_id, token, sig), timeout=30)
    if master_resp.status_code == 403:
        raise TwitchGQLError(f"Twitch VOD {vod_id} erişimi reddedildi (sub-only olabilir).")
    master_resp.raise_for_status()
    variant = pick_variant(parse_master(master_resp.text, str(master_resp.url)), quality)
    log.info("vod variant", extra={"vod_id": vod_id, "variant": variant.name})
    media_resp = await client.get(variant.url, timeout=30)
    media_resp.raise_for_status()
    playlist = parse_media(media_resp.text, str(media_resp.url))
    if not playlist.segments:
        raise TwitchGQLError(f"Twitch VOD {vod_id} playlist'i boş.")

    parts_dir = out_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, cfg.download_concurrency))
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

    tasks = []
    for seg in playlist.segments:
        dest = parts_dir / f"{seg.seq:06d}.ts"
        paths.append(dest)
        tasks.append(fetch(seg.url, dest))
    await asyncio.gather(*tasks)

    video = out_dir / "video.mp4"
    await concat_copy(paths, video, ffmpeg)
    for p in paths:
        p.unlink(missing_ok=True)
    parts_dir.rmdir()
    return video


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
