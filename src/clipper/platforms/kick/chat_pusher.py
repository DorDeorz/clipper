"""Kick live chat over kick.com's public Pusher WebSocket (unofficial; ROADMAP 4.1).

Pusher frames are `{"event": ..., "channel": ..., "data": "<JSON string>"}`; the data of
chat events is JSON encoded a second time. Event names and channels come from config.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from clipper.config import KickConfig
from clipper.detect.events import LiveEvent
from clipper.live.chat import WS, ChatClientError, ChatSink, Connector, default_connect
from clipper.log import get_logger
from clipper.models import ChatMessage, hash_user_id
from clipper.platforms.kick.vod import parse_content
from clipper.util.timeutil import parse_iso

log = get_logger("kick.pusher")


def _data(frame: dict[str, Any]) -> dict[str, Any]:
    raw = frame.get("data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _ts(d: dict[str, Any], now: float) -> float:
    raw = d.get("created_at")
    if isinstance(raw, str):
        try:
            return parse_iso(raw)
        except ValueError:
            pass
    return now


def frame_items(
    frame: dict[str, Any], events: dict[str, str], now: float
) -> list[ChatMessage | LiveEvent]:
    kind = events.get(str(frame.get("event")))
    if kind is None or kind == "ignore":
        return []
    d = _data(frame)
    if kind == "message":
        text, emotes = parse_content(str(d.get("content") or ""))
        sender = d.get("sender") or {}
        uid = str(sender.get("id") or sender.get("slug") or "anon")
        return [
            ChatMessage(
                ts=_ts(d, now),
                user_id=hash_user_id("kick", uid),
                text=text,
                emotes=emotes,
                platform="kick",
            )
        ]
    if kind == "sub":
        return [LiveEvent(now, "sub", None, {"months": d.get("months")})]
    if kind == "gift_sub":
        users = d.get("gifted_usernames")
        n = len(users) if isinstance(users, list) and users else 1
        return [LiveEvent(now, "gift_sub", float(n))]
    if kind == "raid":
        n = d.get("number_viewers")
        return [LiveEvent(now, "raid", float(n) if isinstance(n, int | float) else None)]
    if kind in ("live", "offline"):
        return [LiveEvent(now, f"stream_{kind}")]
    return []


class KickPusherChat:
    def __init__(
        self,
        cfg: KickConfig,
        chatroom_id: int,
        channel_id: int | None,
        *,
        connect: Connector = default_connect,
        clock: Callable[[], float] = time.time,
        idle_timeout: float = 150.0,
    ) -> None:
        self.cfg = cfg
        self.chatroom_id = chatroom_id
        self.channel_id = channel_id
        self.connect = connect
        self.clock = clock
        self.idle_timeout = idle_timeout
        self._unknown: set[str] = set()
        self.on_offline: Callable[[], None] | None = None

    def _channels(self) -> list[str]:
        out = []
        for tpl in self.cfg.pusher_channels:
            if "{channel_id}" in tpl and self.channel_id is None:
                continue
            out.append(tpl.format(chatroom_id=self.chatroom_id, channel_id=self.channel_id))
        return out

    async def connect_once(self, sink: ChatSink, stop: asyncio.Event) -> None:
        ws = await self.connect(self.cfg.pusher_url)
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if hello.get("event") != "pusher:connection_established":
                raise ChatClientError(
                    "Kick Pusher beklenmeyen karşılama; anahtar değişmiş olabilir"
                    " (config: kick.pusher_url)",
                    fatal=True,
                )
            for ch in self._channels():
                await ws.send(
                    json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": ch}})
                )
            await self._loop(ws, sink, stop)
        finally:
            await ws.close()

    async def _loop(self, ws: WS, sink: ChatSink, stop: asyncio.Event) -> None:
        stopper = asyncio.ensure_future(stop.wait())
        try:
            while not stop.is_set():
                recv = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait(
                    {recv, stopper}, timeout=self.idle_timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if stopper in done:
                    recv.cancel()
                    return
                if not done:
                    recv.cancel()
                    raise ChatClientError("Kick Pusher sessiz kaldı")
                raw = recv.result()
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                await self._handle(ws, frame, sink)
        finally:
            stopper.cancel()

    async def _handle(self, ws: WS, frame: dict[str, Any], sink: ChatSink) -> None:
        ev = str(frame.get("event"))
        if ev == "pusher:ping":
            await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
            return
        if ev == "pusher:error":
            raise ChatClientError(f"Kick Pusher hatası: {frame.get('data')}")
        if ev.startswith("pusher"):
            return
        if ev not in self.cfg.pusher_events and ev not in self._unknown:
            self._unknown.add(ev)
            log.info("kick unknown event", extra={"event": ev})
        for item in frame_items(frame, self.cfg.pusher_events, self.clock()):
            if isinstance(item, ChatMessage):
                sink.message(item)
            elif item.kind == "stream_offline":
                if self.on_offline is not None:
                    self.on_offline()
            elif item.kind != "stream_live":
                sink.event(item)
