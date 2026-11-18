"""Kick VOD metadata and chat replay (unofficial web API)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from clipper.models import ChatMessage, hash_user_id
from clipper.platforms.kick.api import KickAPI, KickError
from clipper.util.timeutil import parse_iso

EMOTE_RE = re.compile(r"\[emote:(\d+):([^\]]+)\]")


@dataclass(frozen=True, slots=True)
class KickVodMeta:
    uuid: str
    channel_id: int
    slug: str
    display_name: str
    title: str | None
    category: str | None
    start_time: float  # unix seconds (livestream start)
    duration: float  # seconds
    source: str  # master m3u8
    is_live: bool


def parse_video(data: dict[str, Any]) -> KickVodMeta:
    try:
        live = data["livestream"]
        channel = live.get("channel") or {}
        cats = live.get("categories") or []
        user = channel.get("user") or {}
        return KickVodMeta(
            uuid=str(data["uuid"]),
            channel_id=int(live.get("channel_id") or channel["id"]),
            slug=str(channel.get("slug") or ""),
            display_name=str(user.get("username") or channel.get("slug") or "kick"),
            title=live.get("session_title"),
            category=(cats[0].get("name") if cats else None),
            start_time=parse_iso(str(live["start_time"])),
            duration=float(live.get("duration") or 0) / 1000.0,
            source=str(data["source"]),
            is_live=bool(live.get("is_live")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KickError(
            f"Kick VOD bilgisi beklenmeyen biçimde (alanlar değişmiş olabilir): {exc}"
        ) from exc


def parse_content(content: str) -> tuple[str, list[str]]:
    """`"hey [emote:37226:KEKW]"` -> (`"hey KEKW"`, `["KEKW"]`)."""
    emotes = [m.group(2) for m in EMOTE_RE.finditer(content)]
    return EMOTE_RE.sub(lambda m: m.group(2), content).strip(), emotes


def message_to_chat(msg: dict[str, Any], t0: float) -> ChatMessage:
    text, emotes = parse_content(str(msg.get("content") or ""))
    sender = msg.get("sender") or {}
    uid = str(sender.get("id") or msg.get("user_id") or sender.get("slug") or "anon")
    return ChatMessage(
        ts=parse_iso(str(msg["created_at"])) - t0,
        user_id=hash_user_id("kick", uid),
        text=text,
        emotes=emotes,
        platform="kick",
    )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def iter_vod_chat(
    api: KickAPI,
    meta: KickVodMeta,
    t0: float,
    progress: Callable[[float], None] | None = None,
) -> AsyncIterator[ChatMessage]:
    """Step through the VOD in `chat_window_seconds` windows (the replay endpoint returns
    ~5 s after `start_time` and has no forward cursor). `ts` is relative to `t0`, the
    wall-clock time of the VOD's first frame. Duplicates across windows are dropped by id."""
    window = api.cfg.chat_window_seconds
    end = t0 + meta.duration
    start = float(int(t0))
    seen: set[str] = set()
    while start < end:
        msgs = await api.messages(meta.channel_id, _iso(start))
        last_ts = start
        for m in msgs:
            mid = str(m.get("id") or "")
            if mid in seen:
                continue
            seen.add(mid)
            try:
                chat = message_to_chat(m, t0)
            except (KeyError, TypeError, ValueError) as exc:
                raise KickError(f"Kick chat mesajı beklenmeyen biçimde: {exc}") from exc
            last_ts = max(last_ts, chat.ts + t0)
            if 0 <= chat.ts <= meta.duration:
                yield chat
        if progress is not None:
            progress(start - t0)
        if len(msgs) >= api.cfg.chat_page_hint and int(last_ts) < start + window - 1:
            start = float(max(int(last_ts), int(start) + 1))  # page may be truncated
        else:
            start += window
