"""YouTube innertube (the website's internal API): anonymous, no quota. Unofficial and
isolated here; when JSON paths change, a `YouTubeParseError` with a clear message is raised.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from clipper.config import YouTubeConfig
from clipper.log import get_logger
from clipper.models import ChatMessage, hash_user_id
from clipper.util.timeutil import parse_iso

log = get_logger("youtube.innertube")

PAID_WEIGHT = 1.5
_RENDERERS = {
    "liveChatTextMessageRenderer": 1.0,
    "liveChatPaidMessageRenderer": PAID_WEIGHT,
    "liveChatPaidStickerRenderer": PAID_WEIGHT,
    "liveChatMembershipItemRenderer": PAID_WEIGHT,
}
_KEY_RE = re.compile(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"')
_VER_RE = re.compile(r'"INNERTUBE_CONTEXT_CLIENT_VERSION"\s*:\s*"([^"]+)"')
MAX_PAGES = 100_000


class YouTubeParseError(Exception):
    """User-facing (Turkish): innertube response did not have the expected shape."""


@dataclass(frozen=True, slots=True)
class WatchPage:
    video_id: str
    api_key: str
    client_version: str
    title: str | None
    channel_id: str | None
    author: str | None
    is_live: bool
    start_ts: float | None
    chat_continuation: str | None


def headers(cfg: YouTubeConfig) -> dict[str, str]:
    return {
        "User-Agent": cfg.user_agent,
        "Accept-Language": cfg.accept_language,
        "Cookie": cfg.consent_cookie,
    }


def extract_json_var(html: str, name: str) -> dict[str, Any] | None:
    """Extract `var name = {...};` / `window["name"] = {...};` JSON from page HTML."""
    for marker in (f"var {name} = ", f'window["{name}"] = ', f"{name} = "):
        idx = html.find(marker)
        if idx < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(html, idx + len(marker))
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def find_key(obj: Any, key: str) -> Iterator[Any]:
    """Depth-first search for all values under `key` in nested JSON."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k == key:
                    yield v
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def chat_continuation(initial_data: dict[str, Any]) -> str | None:
    """Prefer the "all messages" (last) view of the chat selector; fall back to default."""
    for renderer in find_key(initial_data, "liveChatRenderer"):
        items = list(find_key(renderer, "subMenuItems"))
        if items and isinstance(items[0], list) and items[0]:
            last = items[0][-1]
            for tok in find_key(last, "continuation"):
                if isinstance(tok, str):
                    return tok
        for cont in renderer.get("continuations") or []:
            for tok in find_key(cont, "continuation"):
                if isinstance(tok, str):
                    return tok
    return None


def parse_watch_page(html: str, video_id: str) -> WatchPage:
    key = _KEY_RE.search(html)
    ver = _VER_RE.search(html)
    if not key or not ver:
        raise YouTubeParseError(
            "YouTube sayfasında INNERTUBE anahtarı bulunamadı (consent sayfası ya da sayfa"
            " yapısı değişmiş olabilir)."
        )
    player = extract_json_var(html, "ytInitialPlayerResponse") or {}
    initial = extract_json_var(html, "ytInitialData") or {}
    details = player.get("videoDetails") or {}
    micro = (player.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    live = micro.get("liveBroadcastDetails") or {}
    start = live.get("startTimestamp")
    return WatchPage(
        video_id=video_id,
        api_key=key.group(1),
        client_version=ver.group(1),
        title=details.get("title"),
        channel_id=details.get("channelId"),
        author=details.get("author"),
        is_live=bool(details.get("isLive")),
        start_ts=parse_iso(start) if start else None,
        chat_continuation=chat_continuation(initial),
    )


async def fetch_watch_page(
    client: httpx.AsyncClient, cfg: YouTubeConfig, video_id: str
) -> WatchPage:
    resp = await client.get(
        f"{cfg.base_url}/watch",
        params={"v": video_id},
        headers=headers(cfg),
        timeout=30,
        follow_redirects=True,
    )
    if resp.status_code != 200:
        raise YouTubeParseError(f"YouTube video sayfası açılamadı (HTTP {resp.status_code}).")
    return parse_watch_page(resp.text, video_id)


def runs_to_text(runs: list[dict[str, Any]]) -> tuple[str, list[str]]:
    parts: list[str] = []
    emotes: list[str] = []
    for run in runs:
        if "text" in run:
            parts.append(str(run["text"]))
        elif "emoji" in run:
            emoji = run["emoji"]
            shortcuts = emoji.get("shortcuts") or []
            label = str(shortcuts[0]) if shortcuts else str(emoji.get("emojiId", ""))
            # Standard unicode emoji have the character itself as emojiId.
            if not emoji.get("isCustomEmoji") and emoji.get("emojiId"):
                label = str(emoji["emojiId"])
            parts.append(label)
            emotes.append(label)
    return "".join(parts), emotes


def renderer_to_message(item: dict[str, Any], ts: float | None = None) -> ChatMessage | None:
    """Convert one chat item (`addChatItemAction.item`) to a ChatMessage.

    `ts`: explicit timestamp (replay offset); otherwise `timestampUsec` (unix).
    """
    for name, weight in _RENDERERS.items():
        r = item.get(name)
        if r is None:
            continue
        runs = (r.get("message") or r.get("headerSubtext") or {}).get("runs") or []
        text, emotes = runs_to_text(runs)
        if ts is None:
            ts = int(r["timestampUsec"]) / 1_000_000
        author = str(r.get("authorExternalChannelId") or "anon")
        return ChatMessage(
            ts=ts,
            user_id=hash_user_id("youtube", author),
            text=text,
            emotes=emotes,
            platform="youtube",
            weight=weight,
        )
    return None


@dataclass(slots=True)
class ReplayPage:
    messages: list[ChatMessage]
    continuation: str | None


def parse_replay_response(data: dict[str, Any]) -> ReplayPage:
    try:
        cont = data["continuationContents"]["liveChatContinuation"]
    except (KeyError, TypeError):
        if "continuationContents" not in data:
            return ReplayPage([], None)  # end of replay
        raise YouTubeParseError("YouTube chat replay cevabı beklenmeyen biçimde.") from None
    msgs: list[ChatMessage] = []
    for action in cont.get("actions") or []:
        replay = action.get("replayChatItemAction")
        if not replay:
            continue
        try:
            offset = int(replay.get("videoOffsetTimeMsec", "0")) / 1000
        except ValueError:
            continue
        for inner in replay.get("actions") or []:
            item = (inner.get("addChatItemAction") or {}).get("item")
            if not item:
                continue
            try:
                msg = renderer_to_message(item, ts=offset)
            except (KeyError, TypeError, ValueError) as exc:
                raise YouTubeParseError(f"YouTube chat mesajı okunamadı: {exc}") from exc
            if msg is not None:
                msgs.append(msg)
    next_token: str | None = None
    for c in cont.get("continuations") or []:
        data_c = c.get("liveChatReplayContinuationData")
        if data_c and data_c.get("continuation"):
            next_token = str(data_c["continuation"])
            break
    return ReplayPage(msgs, next_token)


def _client_context(page: WatchPage) -> dict[str, Any]:
    return {"client": {"clientName": "WEB", "clientVersion": page.client_version, "hl": "tr"}}


async def iter_chat_replay(
    client: httpx.AsyncClient,
    cfg: YouTubeConfig,
    page: WatchPage,
    progress: Callable[[float], None] | None = None,
) -> AsyncIterator[ChatMessage]:
    """Yield replay chat of a finished stream (ts = seconds from video start)."""
    token = page.chat_continuation
    if not token:
        raise YouTubeParseError(
            "Bu videoda chat tekrarı yok (canlı yayın değil ya da chat kapalı)."
        )
    url = f"{cfg.base_url}/youtubei/v1/live_chat/get_live_chat_replay"
    offset_ms = 0
    for _ in range(MAX_PAGES):
        body = {
            "context": _client_context(page),
            "continuation": token,
            "currentPlayerState": {"playerOffsetMs": str(offset_ms)},
        }
        resp = await client.post(
            url,
            params={"key": page.api_key, "prettyPrint": "false"},
            json=body,
            headers=headers(cfg),
            timeout=30,
        )
        if resp.status_code != 200:
            raise YouTubeParseError(f"YouTube chat replay HTTP {resp.status_code}.")
        result = parse_replay_response(resp.json())
        for msg in result.messages:
            offset_ms = max(offset_ms, int(msg.ts * 1000))
            if progress is not None:
                progress(msg.ts)
            yield msg
        if not result.continuation or result.continuation == token:
            return
        token = result.continuation
    raise YouTubeParseError("YouTube chat sayfalama sınırı aşıldı.")
