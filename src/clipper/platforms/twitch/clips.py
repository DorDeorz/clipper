"""Viewer clips of a Twitch VOD via anonymous GQL (r3 viewer-clip path).

Verified 2026-11-18: `user(login).clips(criteria)` answers anonymous raw queries and each
node carries `videoOffsetSeconds` and `video { id }`. `video(id).clips` answers but is
always empty, so we page the channel's clips and keep the ones pointing at our VOD.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from clipper.detect.events import LiveEvent
from clipper.log import get_logger
from clipper.platforms.twitch.gql import GQLOperation, TwitchGQL, TwitchGQLError

log = get_logger("twitch.clips")
OPERATION = "ChannelClips"
PAGE_SIZE = 100
QUERY = """
query ChannelClips($login: String!, $first: Int!, $after: Cursor, $period: ClipsPeriod) {
  user(login: $login) {
    clips(first: $first, after: $after, criteria: {period: $period, sort: VIEWS_DESC}) {
      pageInfo { hasNextPage }
      edges {
        cursor
        node {
          id slug title createdAt durationSeconds viewCount videoOffsetSeconds
          video { id }
        }
      }
    }
  }
}
"""


def clips_period(vod_created: datetime | None, now: datetime | None = None) -> str:
    """Narrowest Twitch clip period that still covers a VOD created at `vod_created`."""
    if vod_created is None:
        return "ALL_TIME"
    age = ((now or datetime.now(UTC)) - vod_created).total_seconds()
    if age <= 23 * 3600:
        return "LAST_DAY"
    if age <= 6.5 * 86400:
        return "LAST_WEEK"
    if age <= 29 * 86400:
        return "LAST_MONTH"
    return "ALL_TIME"


def node_to_event(node: dict[str, Any]) -> LiveEvent:
    return LiveEvent(
        ts=float(node["videoOffsetSeconds"]),
        kind="viewer_clip",
        data={
            "slug": node.get("slug"),
            "title": node.get("title"),
            "duration": float(node.get("durationSeconds") or 0),
            "views": int(node.get("viewCount") or 0),
            "created_at": node.get("createdAt"),
        },
    )


async def vod_clips(
    gql: TwitchGQL,
    login: str,
    vod_id: str,
    *,
    vod_created: datetime | None = None,
    max_pages: int = 10,
    say: Callable[[str], None] = lambda _m: None,
) -> list[LiveEvent]:
    """Viewer clips cut from `vod_id`, as `viewer_clip` events at their VOD offset."""
    period = clips_period(vod_created)
    after: str | None = None
    out: list[LiveEvent] = []
    for _ in range(max_pages):
        variables: dict[str, Any] = {
            "login": login,
            "first": PAGE_SIZE,
            "after": after,
            "period": period,
        }
        data = await gql.run(GQLOperation(OPERATION, variables, query=QUERY))
        user = data.get("user")
        if user is None:
            raise TwitchGQLError(f"Twitch kanalı bulunamadı: {login}")
        clips = user.get("clips") or {}
        edges = clips.get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            video = node.get("video") or {}
            if str(video.get("id")) != str(vod_id) or node.get("videoOffsetSeconds") is None:
                continue
            out.append(node_to_event(node))
        if not edges or not (clips.get("pageInfo") or {}).get("hasNextPage"):
            break
        after = edges[-1].get("cursor")
        if not after:
            break
    out.sort(key=lambda e: e.ts)
    log.info("vod clips", extra={"vod_id": vod_id, "clips": len(out), "period": period})
    say(f"İzleyici klipleri: {len(out)}")
    return out
