"""Twitch live: is a channel live (anonymous GQL, batched), live HLS URL (access token +
usher), and recently created viewer clips. Unofficial, isolated here (ROADMAP 4.1/4.2)."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import httpx

from clipper.config import TwitchConfig
from clipper.detect.events import LiveEvent
from clipper.platforms.twitch.gql import GQLOperation, TwitchGQL, TwitchGQLError
from clipper.platforms.twitch.vod import PLAYBACK_TOKEN_QUERY
from clipper.util.timeutil import parse_iso

STATUS_QUERY = """query StreamStatus($logins: [String!]) {
  users(logins: $logins) {
    login
    id
    stream { id type createdAt viewersCount title game { name } }
  }
}"""

RECENT_CLIPS_QUERY = """query RecentClips($login: String!) {
  user(login: $login) {
    clips(first: 20, criteria: {period: LAST_DAY, sort: CREATED_AT_DESC}) {
      edges { node { id slug title createdAt durationSeconds viewCount } }
    }
  }
}"""


@dataclass(frozen=True, slots=True)
class StreamInfo:
    login: str
    live: bool
    stream_id: str | None = None
    started_at: float | None = None
    viewers: int | None = None
    title: str | None = None
    category: str | None = None


async def stream_status(gql: TwitchGQL, logins: list[str]) -> dict[str, StreamInfo]:
    """One request for all channels (GQL `users(logins:)`). Unknown logins are absent."""
    if not logins:
        return {}
    data = await gql.run(
        GQLOperation("StreamStatus", {"logins": [x.lower() for x in logins]}, STATUS_QUERY)
    )
    out: dict[str, StreamInfo] = {}
    for user in data.get("users") or []:
        if not user:
            continue
        login = str(user.get("login") or "").lower()
        s = user.get("stream")
        if not s or (s.get("type") not in (None, "live")):
            out[login] = StreamInfo(login, False)
            continue
        created = s.get("createdAt")
        out[login] = StreamInfo(
            login,
            True,
            str(s.get("id")),
            parse_iso(created) if created else None,
            s.get("viewersCount"),
            s.get("title"),
            (s.get("game") or {}).get("name"),
        )
    return out


async def live_master_url(gql: TwitchGQL, cfg: TwitchConfig, login: str) -> str:
    data = await gql.run(
        GQLOperation(
            "PlaybackAccessToken",
            {"login": login, "isLive": True, "vodID": "", "isVod": False, "playerType": "site"},
            PLAYBACK_TOKEN_QUERY,
        )
    )
    tok = data.get("streamPlaybackAccessToken")
    if not tok or not tok.get("value") or not tok.get("signature"):
        raise TwitchGQLError(f"Twitch {login} için canlı erişim token'ı alınamadı.")
    url = httpx.URL(
        cfg.usher_live_url.format(login=login),
        params={
            "sig": tok["signature"],
            "token": tok["value"],
            "allow_source": "true",
            "fast_bread": "true",
            "p": str(random.randint(100000, 999999)),
        },
    )
    return str(url)


def clip_event(node: dict[str, Any]) -> LiveEvent | None:
    """A live viewer clip as an event at the clip's start (wall clock): a clip made at
    `createdAt` covers the `durationSeconds` before it."""
    created = node.get("createdAt")
    if not created:
        return None
    dur = float(node.get("durationSeconds") or 30)
    return LiveEvent(
        ts=parse_iso(created) - dur,
        kind="viewer_clip",
        data={
            "slug": node.get("slug"),
            "title": node.get("title"),
            "duration": dur,
            "views": int(node.get("viewCount") or 0),
            "created_at": created,
        },
    )


async def recent_clips(gql: TwitchGQL, login: str, since: float) -> list[LiveEvent]:
    """Clips created after `since` (wall clock), oldest first."""
    data = await gql.run(GQLOperation("RecentClips", {"login": login}, RECENT_CLIPS_QUERY))
    edges = (((data.get("user") or {}).get("clips")) or {}).get("edges") or []
    out = []
    for edge in edges:
        node = edge.get("node") or {}
        created = node.get("createdAt")
        if not created or parse_iso(created) <= since:
            continue
        ev = clip_event(node)
        if ev is not None:
            out.append(ev)
    out.sort(key=lambda e: e.ts)
    return out
