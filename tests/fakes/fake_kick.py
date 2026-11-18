# ruff: noqa: E501
"""Fake Kick: channel API (with Cloudflare 403 mode) and Pusher WebSocket (events whose
`data` is a JSON string inside JSON, like the real one)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

CF_PAGE = "<!DOCTYPE html><title>Just a moment...</title>cf-chl-bypass"


@dataclass
class FakeKickApi:
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    cloudflare: bool = False
    calls: int = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.cloudflare:
            return httpx.Response(403, text=CF_PAGE, headers={"cf-mitigated": "challenge"})
        slug = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        if slug in self.channels:
            return httpx.Response(200, json=self.channels[slug])
        return httpx.Response(404, json={"message": "Not found"})


def chat_event(content: str, user: str, uid: int, created: str) -> dict[str, Any]:
    return {
        "event": "App\\Events\\ChatMessageEvent",
        "channel": "chatrooms.7.v2",
        "data": json.dumps({"id": f"m{uid}", "content": content, "created_at": created,
                            "sender": {"id": uid, "username": user}}),
    }  # fmt: skip


@dataclass
class FakePusher:
    frames: list[dict[str, Any]]
    bad_key: bool = False
    subscribed: list[str] = field(default_factory=list)

    async def handler(self, ws: Any) -> None:
        if self.bad_key:
            await ws.send(json.dumps({"event": "pusher:error",
                                      "data": {"code": 4001, "message": "App key not in this cluster"}}))  # fmt: skip
            await ws.close(code=4001)
            return
        await ws.send(json.dumps({"event": "pusher:connection_established",
                                  "data": json.dumps({"socket_id": "1.2"})}))  # fmt: skip
        for _ in range(2):
            self.subscribed.append(json.loads(await ws.recv())["data"]["channel"])
        for f in self.frames:
            await ws.send(json.dumps(f))
        await asyncio.sleep(0.1)
        await ws.close()
