# ruff: noqa: E501
"""Fake Twitch: anonymous IRC over WebSocket, GQL and usher/CDN HLS (all local)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

T0 = datetime(2026, 11, 18, 20, 0, 0, tzinfo=UTC).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def privmsg(user: str, text: str, ts_ms: int, uid: int = 1, emotes: str = "") -> str:
    return (
        f"@badge-info=;display-name={user};emotes={emotes};id=x;tmi-sent-ts={ts_ms};"
        f"user-id={uid} :{user}!{user}@{user}.tmi.twitch.tv PRIVMSG #kanal :{text}"
    )


def usernotice(kind: str, ts_ms: int, **tags: str) -> str:
    extra = "".join(f";{k}={v}" for k, v in tags.items())
    return f"@msg-id={kind};tmi-sent-ts={ts_ms}{extra} :tmi.twitch.tv USERNOTICE #kanal"


@dataclass
class FakeIrc:
    """Each connection: handshake, PING, then the next batch of lines; the server drops the
    connection after each batch until the batches run out."""

    batches: list[list[str]]
    connections: int = 0
    pongs: int = 0

    async def handler(self, ws: Any) -> None:
        self.connections += 1
        for _ in range(4):  # CAP, PASS, NICK, JOIN
            await ws.recv()
        await ws.send("PING :tmi.twitch.tv\r\n")
        if (await ws.recv()).startswith("PONG"):
            self.pongs += 1
        if self.batches:
            await ws.send("\r\n".join(self.batches.pop(0)) + "\r\n")
        await asyncio.sleep(0.05)
        await ws.close()


@dataclass
class FakeHls:
    """A live stream of `total` 2 s segments. Each media playlist fetch shows a window of
    `window` segments and moves the live edge by `step`. Segments in `ads` (seq numbers)
    are inside a stitched-ad DATERANGE. `expire_on` fetch numbers answer 403 (token
    expired) until the master playlist is fetched again."""

    total: int
    window: int = 6
    step: int = 3
    ads: set[int] = field(default_factory=set)
    expire_on: set[int] = field(default_factory=set)
    seg_seconds: float = 2.0
    fetches: int = 0
    master_fetches: int = 0
    edge: int = 0
    expired: bool = False
    served: list[int] = field(default_factory=list)

    def playlist(self) -> str:
        self.edge = min(self.total, self.edge + self.step) if self.edge else self.window
        first = max(0, self.edge - self.window)
        out = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(self.seg_seconds)}",
               f"#EXT-X-MEDIA-SEQUENCE:{first}"]  # fmt: skip
        for seq in range(first, self.edge):
            start = T0 + seq * self.seg_seconds
            if seq in self.ads and (seq - 1 not in self.ads or seq == first):
                # Like Twitch: the ad DATERANGE stays in the playlist while any of its
                # segments are in the window.
                a = seq
                while a - 1 in self.ads:
                    a -= 1
                n = 0
                while a + n in self.ads:
                    n += 1
                out.append(
                    f'#EXT-X-DATERANGE:ID="stitched-ad-{a}",CLASS="twitch-stitched-ad",'
                    f'START-DATE="{iso(T0 + a * self.seg_seconds)}",'
                    f"DURATION={n * self.seg_seconds:.3f}"
                )
            out.append(f"#EXT-X-PROGRAM-DATE-TIME:{iso(start)}")
            out.append(f"#EXTINF:{self.seg_seconds:.3f},live")
            out.append(f"seg{seq}.ts")
        if self.edge >= self.total:
            out.append("#EXT-X-ENDLIST")
        return "\n".join(out) + "\n"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("master.m3u8"):
            self.master_fetches += 1
            self.expired = False
            body = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080\nmedia.m3u8\n"
            return httpx.Response(200, text=body)
        if path.endswith("media.m3u8"):
            self.fetches += 1
            if self.fetches in self.expire_on:
                self.expired = True
            if self.expired:
                return httpx.Response(403, text="token expired")
            return httpx.Response(200, text=self.playlist())
        if path.endswith(".ts"):
            seq = int(path.rsplit("seg", 1)[1][:-3])
            self.served.append(seq)
            return httpx.Response(200, content=f"TS{seq}".encode())
        return httpx.Response(404)


@dataclass
class FakeGql:
    """GQL: StreamStatus, clips, PlaybackAccessToken. `reject` = operation names that come
    back with a GraphQL error (simulates Twitch changing the anonymous API)."""

    live: dict[str, dict[str, Any]] = field(default_factory=dict)
    clips: list[dict[str, Any]] = field(default_factory=list)
    reject: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ops = body if isinstance(body, list) else [body]
        out = []
        for op in ops:
            name = op.get("operationName", "")
            self.calls.append(name)
            if name in self.reject:
                out.append({"errors": [{"message": "PersistedQueryNotFound"}]})
            elif name == "StreamStatus":
                logins = op.get("variables", {}).get("logins", [])
                users = [self.live.get(lg.lower(), {"login": lg.lower(), "id": "0",
                                                    "stream": None}) for lg in logins]  # fmt: skip
                out.append({"data": {"users": users}})
            elif name == "PlaybackAccessToken":
                out.append({"data": {"streamPlaybackAccessToken": {"value": "{}",
                                                                   "signature": "sig"}}})  # fmt: skip
            else:
                out.append(
                    {"data": {"user": {"clips": {"edges": [{"node": c} for c in self.clips]}}}}
                )
        return httpx.Response(200, json=out if isinstance(body, list) else out[0])
