"""Twitch VOD chat downloader via anonymous GQL `VideoCommentsByOffsetOrCursor`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from clipper.models import ChatMessage, hash_user_id
from clipper.platforms.twitch.gql import GQLOperation, TwitchGQL, TwitchGQLError

OPERATION = "VideoCommentsByOffsetOrCursor"
MAX_PAGES = 200_000


def comment_to_message(node: dict[str, Any]) -> ChatMessage:
    commenter = node.get("commenter") or {}
    raw_uid = str(commenter.get("id") or commenter.get("login") or "anon")
    fragments = (node.get("message") or {}).get("fragments") or []
    text = "".join(str(f.get("text") or "") for f in fragments)
    emotes = [str(f.get("text") or "").strip() for f in fragments if f.get("emote")]
    return ChatMessage(
        ts=float(node["contentOffsetSeconds"]),
        user_id=hash_user_id("twitch", raw_uid),
        text=text,
        emotes=[e for e in emotes if e],
        platform="twitch",
    )


async def iter_vod_comments(
    gql: TwitchGQL,
    vod_id: str,
    progress: Callable[[float], None] | None = None,
    *,
    length_seconds: float | None = None,
) -> AsyncIterator[ChatMessage]:
    """Yield comments (ts = seconds from VOD start) by paging on `contentOffsetSeconds`.

    Cursor paging is no longer served to anonymous clients ("failed integrity check",
    2026-11), so each next page starts at the last offset seen; duplicates are dropped
    by comment id. If a whole page shares one second, we step one second forward.

    Comments can run a few seconds past the VOD's `lengthSeconds`, but Twitch answers any
    `contentOffsetSeconds` beyond the length with "service error"; paging stops there.
    """
    offset = 0.0
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        variables: dict[str, Any] = {"videoID": vod_id, "contentOffsetSeconds": int(offset)}
        data = await gql.run(GQLOperation(OPERATION, variables))
        video = data.get("video")
        if video is None:
            raise TwitchGQLError(f"Twitch VOD {vod_id} chat'i bulunamadı.")
        comments = video.get("comments") or {}
        edges = comments.get("edges") or []
        max_offset = offset
        for edge in edges:
            node = edge.get("node") or {}
            cid = str(node.get("id", ""))
            if cid in seen:
                continue
            seen.add(cid)
            try:
                msg = comment_to_message(node)
            except (KeyError, TypeError, ValueError) as exc:
                raise TwitchGQLError(
                    f"Twitch chat yorumu beklenmeyen biçimde (alanlar değişmiş olabilir): {exc}"
                ) from exc
            max_offset = max(max_offset, msg.ts)
            if progress is not None:
                progress(msg.ts)
            yield msg
        has_next = bool((comments.get("pageInfo") or {}).get("hasNextPage"))
        if not has_next or not edges:
            return
        next_offset = float(int(max_offset))
        offset = next_offset if next_offset > int(offset) else float(int(offset) + 1)
        if length_seconds is not None and offset >= length_seconds:
            return
    raise TwitchGQLError("Twitch chat sayfalama sınırı aşıldı.")
