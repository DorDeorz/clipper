"""Twitch VOD downloader (own code): GQL access token -> usher m3u8 -> segments -> mp4."""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import httpx

from clipper.config import TwitchConfig
from clipper.log import get_logger
from clipper.media import MediaError
from clipper.platforms.twitch.gql import GQLOperation, TwitchGQL, TwitchGQLError
from clipper.recorder.hls_vod import Progress, download_hls_vod
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
    try:
        res = await download_hls_vod(
            client,
            usher_vod_url(cfg, vod_id, token, sig),
            out_dir,
            quality,
            concurrency=cfg.download_concurrency,
            ffmpeg=ffmpeg,
            progress=progress,
            forbidden_message=f"Twitch VOD {vod_id} erişimi reddedildi (sub-only olabilir).",
        )
    except MediaError as exc:
        if "erişimi reddedildi" in str(exc) or "boş" in str(exc):
            raise TwitchGQLError(f"Twitch VOD {vod_id}: {exc}") from exc
        raise
    return res.video
