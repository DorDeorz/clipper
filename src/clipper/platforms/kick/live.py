"""Kick live: channel API says whether a channel is live and gives the HLS master URL and
the chatroom id for Pusher (unofficial; ROADMAP 4.1/4.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clipper.platforms.kick.api import KickAPI
from clipper.util.timeutil import parse_iso


@dataclass(frozen=True, slots=True)
class KickLive:
    slug: str
    live: bool
    channel_id: int | None
    chatroom_id: int | None
    playback_url: str | None
    started_at: float | None = None
    viewers: int | None = None
    title: str | None = None
    category: str | None = None


def parse_channel(slug: str, ch: dict[str, Any]) -> KickLive:
    ls = ch.get("livestream") or None
    started = None
    if ls and ls.get("start_time"):
        raw = str(ls["start_time"])
        try:
            started = parse_iso(raw if "T" in raw else raw.replace(" ", "T") + "Z")
        except ValueError:
            started = None
    cats = (ls or {}).get("categories") or []
    return KickLive(
        slug=slug,
        live=bool(ls) and bool(ls.get("is_live", True)),
        channel_id=ch.get("id"),
        chatroom_id=(ch.get("chatroom") or {}).get("id"),
        playback_url=ch.get("playback_url") or None,
        started_at=started,
        viewers=(ls or {}).get("viewer_count"),
        title=(ls or {}).get("session_title"),
        category=(cats[0] or {}).get("name") if cats else None,
    )


async def channel_live(api: KickAPI, slug: str) -> KickLive:
    return parse_channel(slug, await api.channel(slug))
