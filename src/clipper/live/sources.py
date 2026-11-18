"""Platform glue for the watcher: which streamers are live, and a StreamSource for each.

Twitch channels are checked in one batched GQL request; Kick and YouTube one by one
(Kick requests are paced by KickAPI)."""

from __future__ import annotations

import sqlite3

import httpx

from clipper.config import Config
from clipper.detect.events import LiveEvent
from clipper.live.session import StreamSource
from clipper.live.watcher import Prober
from clipper.log import get_logger
from clipper.platforms.kick.chat_pusher import KickPusherChat
from clipper.platforms.kick.live import channel_live
from clipper.platforms.twitch import live as twitch_live
from clipper.platforms.twitch.chat_irc import TwitchIrcChat
from clipper.platforms.twitch.gql import TwitchGQL
from clipper.platforms.youtube import innertube
from clipper.platforms.youtube.vod import live_hls_url
from clipper.recorder.hls_live import StreamOffline

log = get_logger("live.sources")


def make_prober(cfg: Config, client: httpx.AsyncClient) -> Prober:
    from clipper.ingest import make_kick_api

    gql = TwitchGQL(client, cfg.twitch)
    kick = make_kick_api(cfg, client)

    def twitch_source(login: str, info: twitch_live.StreamInfo) -> StreamSource:
        async def resolve() -> str:
            return await twitch_live.live_master_url(gql, cfg.twitch, login)

        async def clips(since: float) -> list[LiveEvent]:
            return await twitch_live.recent_clips(gql, login, since)

        return StreamSource(
            "twitch", login, resolve, TwitchIrcChat(login).connect_once,
            info.title, info.category, info.started_at, info.viewers, clips,
        )  # fmt: skip

    async def kick_source(slug: str) -> StreamSource | None:
        info = await channel_live(kick, slug)
        if not info.live or not info.playback_url or info.chatroom_id is None:
            return None

        async def resolve() -> str:
            fresh = await channel_live(kick, slug)
            if not fresh.playback_url:
                raise StreamOffline("Kick yayını kapandı")
            return fresh.playback_url

        chat = KickPusherChat(cfg.kick, info.chatroom_id, info.channel_id)
        return StreamSource(
            "kick", slug, resolve, chat.connect_once, info.title, info.category,
            info.started_at, info.viewers,
            headers={"User-Agent": cfg.kick.user_agent, "Referer": cfg.kick.base_url + "/"},
        )  # fmt: skip

    async def youtube_source(channel: str) -> StreamSource | None:
        st = await innertube.live_status(client, cfg.youtube, channel)
        if not st.is_live or not st.video_id:
            return None
        vid = st.video_id

        async def resolve() -> str:
            return await live_hls_url(cfg.youtube, vid)

        chat = innertube.YouTubeLiveChat(client, cfg.youtube, vid)
        return StreamSource(
            "youtube", channel, resolve, chat.connect_once, st.title, None, None, st.viewers
        )

    async def probe(rows: list[sqlite3.Row]) -> dict[int, StreamSource | None]:
        out: dict[int, StreamSource | None] = {}
        twitch_rows = [r for r in rows if r["platform"] == "twitch"]
        if twitch_rows:
            try:
                status = await twitch_live.stream_status(gql, [r["channel"] for r in twitch_rows])
            except Exception as exc:
                log.warning("twitch status failed", extra={"error": str(exc)})
            else:
                for r in twitch_rows:
                    info = status.get(str(r["channel"]).lower())
                    if info is None:
                        continue  # unknown login: leave unprobed (no false "offline")
                    out[int(r["id"])] = twitch_source(info.login, info) if info.live else None
        for r in rows:
            try:
                if r["platform"] == "kick":
                    out[int(r["id"])] = await kick_source(str(r["channel"]))
                elif r["platform"] == "youtube":
                    out[int(r["id"])] = await youtube_source(str(r["channel"]))
            except Exception as exc:
                log.warning(
                    "live check failed", extra={"streamer": r["display_name"], "error": str(exc)}
                )
        return out

    return probe
