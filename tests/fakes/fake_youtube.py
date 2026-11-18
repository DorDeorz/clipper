# ruff: noqa: E501
"""Fake YouTube: `/@kanal/live`, `/live_chat` page and the `get_live_chat` continuation
chain. `rename` swaps renderer field names to simulate a YouTube layout change."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


def text_item(ts_us: int, author: str, text: str, key: str = "liveChatTextMessageRenderer"):  # type: ignore[no-untyped-def]
    return {"addChatItemAction": {"item": {key: {
        "timestampUsec": str(ts_us), "authorExternalChannelId": author,
        "message": {"runs": [{"text": text}]}}}}}  # fmt: skip


@dataclass
class FakeYouTube:
    video_id: str = "dQw4w9WgXcQ"
    pages: list[list[dict[str, Any]]] = field(default_factory=list)
    timeout_ms: int = 1500
    rename: bool = False
    live: bool = True
    tokens: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/live"):
            player = {"videoDetails": {"videoId": self.video_id, "isLive": self.live,
                                       "title": "canlı", "viewCount": "321"}}  # fmt: skip
            return httpx.Response(
                200, text=f"<script>var ytInitialPlayerResponse = {json.dumps(player)};</script>"
            )
        if path == "/live_chat":
            initial = {
                "contents": {
                    "liveChatRenderer": {
                        "continuations": [
                            {
                                "invalidationContinuationData": {
                                    "continuation": "C0",
                                    "timeoutMs": 1000,
                                }
                            }
                        ]
                    }
                }
            }
            html = ('<script>ytcfg.set({"INNERTUBE_API_KEY": "KEY", '
                    '"INNERTUBE_CONTEXT_CLIENT_VERSION": "2.2026"});'
                    f"var ytInitialData = {json.dumps(initial)};</script>")  # fmt: skip
            return httpx.Response(200, text=html)
        if path.endswith("/get_live_chat"):
            token = json.loads(request.content)["continuation"]
            self.tokens.append(token)
            i = int(token[1:])
            actions = self.pages[i] if i < len(self.pages) else []
            if self.rename:
                actions = json.loads(json.dumps(actions).replace("liveChatTextMessageRenderer",
                                                                 "liveChatTextMessageViewModel"))  # fmt: skip
            conts = [] if i + 1 > len(self.pages) else [
                {"timedContinuationData": {"continuation": f"C{i + 1}", "timeoutMs": self.timeout_ms}}]  # fmt: skip
            return httpx.Response(200, json={"continuationContents": {"liveChatContinuation": {
                "continuations": conts, "actions": actions}}})  # fmt: skip
        return httpx.Response(404)
