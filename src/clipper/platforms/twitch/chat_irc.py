"""Twitch chat, read-only, over IRC-on-WebSocket as an anonymous `justinfan` guest
(ROADMAP 4.1). Twitch documents anonymous read-only connections; no account needed.

Also turns USERNOTICE (sub, resub, subgift, submysterygift, raid) and `bits` tags into
platform events for the event filter (ROADMAP 4.3 item 8b).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from clipper.detect.events import LiveEvent
from clipper.live.chat import WS, ChatClientError, ChatSink, Connector, default_connect
from clipper.log import get_logger
from clipper.models import ChatMessage, hash_user_id

log = get_logger("twitch.irc")
IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
SUB_IDS = {"sub", "resub", "primepaidupgrade", "giftpaidupgrade", "anongiftpaidupgrade"}


# ---- IRC line parsing -------------------------------------------------------------------

_TAG_ESCAPES = {r"\:": ";", r"\s": " ", r"\\": "\\", r"\r": "\r", r"\n": "\n"}


def _unescape(v: str) -> str:
    if "\\" not in v:
        return v
    out, i = [], 0
    while i < len(v):
        two = v[i : i + 2]
        if two in _TAG_ESCAPES:
            out.append(_TAG_ESCAPES[two])
            i += 2
        else:
            out.append(v[i])
            i += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class IrcLine:
    tags: dict[str, str] = field(default_factory=dict)
    prefix: str = ""
    command: str = ""
    params: list[str] = field(default_factory=list)

    @property
    def trailing(self) -> str:
        return self.params[-1] if self.params else ""


def parse_line(line: str) -> IrcLine:
    tags: dict[str, str] = {}
    rest = line
    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        for item in raw_tags.split(";"):
            k, _, v = item.partition("=")
            tags[k] = _unescape(v)
    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    trailing = None
    if " :" in rest:
        rest, _, trailing = rest.partition(" :")
    parts = rest.split()
    command = parts[0] if parts else ""
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return IrcLine(tags, prefix, command, params)


def emote_names(text: str, emotes_tag: str) -> list[str]:
    """`emotes=25:0-4,12-16/1902:6-10` → the emote texts, one per occurrence."""
    if not emotes_tag:
        return []
    out: list[str] = []
    chars = list(text)  # positions are in code points
    for group in emotes_tag.split("/"):
        _eid, _, spans = group.partition(":")
        for span in spans.split(","):
            a, _, b = span.partition("-")
            try:
                out.append("".join(chars[int(a) : int(b) + 1]))
            except ValueError:
                continue
    return out


def _ts(tags: dict[str, str], now: float) -> float:
    raw = tags.get("tmi-sent-ts")
    try:
        return int(raw) / 1000 if raw else now
    except ValueError:
        return now


def to_items(line: IrcLine, now: float) -> list[ChatMessage | LiveEvent]:
    """IRC line → chat message and/or platform events (empty for anything else)."""
    out: list[ChatMessage | LiveEvent] = []
    tags = line.tags
    if line.command == "PRIVMSG":
        ts = _ts(tags, now)
        text = line.trailing
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            text = text[8:-1]
        uid = tags.get("user-id") or line.prefix.partition("!")[0]
        out.append(
            ChatMessage(
                ts=ts,
                user_id=hash_user_id("twitch", uid),
                text=text,
                emotes=emote_names(text, tags.get("emotes", "")),
                platform="twitch",
            )
        )
        bits = tags.get("bits")
        if bits and bits.isdigit():
            out.append(LiveEvent(ts, "bits", float(bits)))
    elif line.command == "USERNOTICE":
        ts = _ts(tags, now)
        mid = tags.get("msg-id", "")
        data: dict[str, Any] = {"msg_id": mid}
        if mid in SUB_IDS:
            out.append(LiveEvent(ts, "sub", None, data))
        elif mid == "subgift":
            # Gifts from a mystery bundle are announced one by one after the bundle;
            # the bundle event already counted them.
            if not tags.get("msg-param-community-gift-id"):
                out.append(LiveEvent(ts, "gift_sub", 1.0, data))
        elif mid == "submysterygift":
            n = tags.get("msg-param-mass-gift-count", "1")
            out.append(LiveEvent(ts, "gift_sub", float(n) if n.isdigit() else 1.0, data))
        elif mid == "raid":
            n = tags.get("msg-param-viewerCount", "0")
            out.append(LiveEvent(ts, "raid", float(n) if n.isdigit() else None, data))
    return out


# ---- client -----------------------------------------------------------------------------


class TwitchIrcChat:
    def __init__(
        self,
        login: str,
        *,
        url: str = IRC_URL,
        connect: Connector = default_connect,
        clock: Callable[[], float] | None = None,
        idle_timeout: float = 360.0,
    ) -> None:
        import time

        self.login = login.lower().lstrip("#")
        self.url = url
        self.connect = connect
        self.clock = clock or time.time
        self.idle_timeout = idle_timeout  # Twitch PINGs every ~5 min

    async def connect_once(self, sink: ChatSink, stop: asyncio.Event) -> None:
        ws = await self.connect(self.url)
        try:
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send("PASS SCHMOOPIIE")
            await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
            await ws.send(f"JOIN #{self.login}")
            stopper = asyncio.ensure_future(stop.wait())
            try:
                while not stop.is_set():
                    recv = asyncio.ensure_future(ws.recv())
                    done, _ = await asyncio.wait(
                        {recv, stopper},
                        timeout=self.idle_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stopper in done:
                        recv.cancel()
                        return
                    if not done:
                        recv.cancel()
                        raise ChatClientError("Twitch IRC sessiz kaldı (PING gelmedi)")
                    raw = recv.result()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    await self._handle(ws, raw, sink)
            finally:
                stopper.cancel()
        finally:
            await ws.close()

    async def _handle(self, ws: WS, raw: str, sink: ChatSink) -> None:
        now = self.clock()
        for text in raw.split("\r\n"):
            if not text:
                continue
            line = parse_line(text)
            if line.command == "PING":
                await ws.send(f"PONG :{line.trailing or 'tmi.twitch.tv'}")
            elif line.command == "RECONNECT":
                raise ChatClientError("Twitch IRC yeniden bağlan istedi")
            elif line.command == "NOTICE" and "Login" in line.trailing:
                raise ChatClientError(f"Twitch IRC girişi reddedildi: {line.trailing}")
            else:
                for item in to_items(line, now):
                    if isinstance(item, ChatMessage):
                        sink.message(item)
                    else:
                        sink.event(item)
