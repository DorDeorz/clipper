"""Real-network checks for unofficial endpoints. Run explicitly: `uv run pytest -m online`.

Set CLIPPER_TEST_TWITCH_VOD / CLIPPER_TEST_YOUTUBE_VIDEO to pick the videos.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from clipper.config import TwitchConfig, YouTubeConfig
from clipper.platforms.twitch import vod as twitch_vod
from clipper.platforms.twitch.gql import TwitchGQL
from clipper.platforms.twitch.vod_chat import iter_vod_comments
from clipper.platforms.youtube import innertube

pytestmark = pytest.mark.online


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} tanımlı değil")
    return value


def test_twitch_vod_endpoints() -> None:
    vod_id = _env("CLIPPER_TEST_TWITCH_VOD")

    async def go() -> None:
        async with httpx.AsyncClient() as client:
            gql = TwitchGQL(client, TwitchConfig())
            meta = await twitch_vod.get_metadata(gql, vod_id)
            assert meta.login
            token, sig = await twitch_vod.get_vod_token(gql, vod_id)
            resp = await client.get(twitch_vod.usher_vod_url(TwitchConfig(), vod_id, token, sig))
            assert resp.status_code == 200 and "#EXTM3U" in resp.text
            n = 0
            async for _ in iter_vod_comments(gql, vod_id):
                n += 1
                if n >= 50:
                    break
            assert n > 0

    asyncio.run(go())


def test_youtube_replay_endpoints() -> None:
    video_id = _env("CLIPPER_TEST_YOUTUBE_VIDEO")

    async def go() -> None:
        async with httpx.AsyncClient() as client:
            page = await innertube.fetch_watch_page(client, YouTubeConfig(), video_id)
            n = 0
            async for _ in innertube.iter_chat_replay(client, YouTubeConfig(), page):
                n += 1
                if n >= 50:
                    break
            assert n > 0

    asyncio.run(go())
