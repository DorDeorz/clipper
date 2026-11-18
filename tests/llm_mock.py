"""Fake Anthropic Messages API for tests (httpx2.MockTransport through the SDK)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx2

Reply = Callable[[dict[str, Any]], dict[str, Any] | str]


def message(
    model: str,
    text: str,
    *,
    stop: str = "end_turn",
    inp: int = 1000,
    out: int = 200,
    cache_read: int = 0,
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 0,
        },
    }


@dataclass
class FakeAnthropic:
    """`replies` is consumed in order; each item gets the request body and returns either a
    full message dict or the text of the assistant reply."""

    replies: list[Reply] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    betas: list[str] = field(default_factory=list)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.urls.append(str(request.url))
        self.betas.append(request.headers.get("anthropic-beta", ""))
        if not self.replies:
            return httpx2.Response(
                500, json={"type": "error", "error": {"type": "api_error", "message": "no reply"}}
            )
        out = self.replies.pop(0)(body)
        if isinstance(out, str):
            out = message(body["model"], out)
        return httpx2.Response(200, json=out)

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key="test-key",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(self.handler)
            ),
        )


def judge_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "keep": True,
        "score": 82,
        "reason": "Net bir tepki anı.",
        "start": 9.0,
        "end": 40.0,
        "title": "Bunu kimse beklemiyordu 😱",
        "hook": "Ve o an geldi",
        "caption_fixes": [{"from": "valoran", "to": "Valorant"}],
        "hashtags": ["#valorant", "#twitch"],
        "texts": {
            "youtube": {"title": "Bunu kimse beklemiyordu", "description": "Kısa açıklama."},
            "instagram": {"caption": "Inanılmaz an #twitch"},
            "tiktok": {"caption": "Yok artık #fyp"},
            "x": {"text": "Bunu kimse beklemiyordu"},
        },
        "flags": {
            "music_risk": False,
            "nsfw": False,
            "sensitive_topic": False,
            "needs_context": False,
        },
    }
    base.update(over)
    return base
