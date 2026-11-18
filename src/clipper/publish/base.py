"""Shared publishing types and HTTP helpers (ROADMAP 4.7).

Errors: 429 / 5xx / network → retried here with exponential backoff (honouring
`Retry-After`), then surfaced as a retryable `PublishError` so the job queue tries
again later. Other 4xx → `PublishError(retry=False)` with the platform's message.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from clipper.log import get_logger

log = get_logger("publish")

Sleep = Callable[[float], Awaitable[None]]
SaveState = Callable[[dict[str, Any]], None]
TokenGetter = Callable[[str, str], Awaitable["AccessToken"]]
Status = Literal["processing", "published", "inbox", "failed"]

RETRY_STATUS = {429, 500, 502, 503, 504}
HTTP_RETRIES = 4
BACKOFF_BASE = 2.0
BACKOFF_MAX = 120.0


class PublishError(Exception):
    """`retry=True`: the job queue retries later (worker reads the attribute)."""

    def __init__(self, message: str, *, retry: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retry = retry
        self.status = status


class AuthError(PublishError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retry=False, status=401)


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClipForPublish:
    clip_id: int
    path: Path
    title: str
    description: str
    caption: str
    x_text: str
    tags: list[str]

    @property
    def size(self) -> int:
        return self.path.stat().st_size


@dataclass(slots=True)
class PublishResult:
    status: Status
    remote_id: str | None = None
    url: str | None = None
    error: str | None = None


class Publisher(Protocol):
    platform: str

    async def upload(
        self, clip: ClipForPublish, account: str, state: dict[str, Any], save: SaveState
    ) -> PublishResult: ...

    async def check_status(
        self, account: str, remote_id: str, state: dict[str, Any]
    ) -> PublishResult: ...


def retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def backoff(attempt: int) -> float:
    return min(BACKOFF_MAX, BACKOFF_BASE * 2**attempt) * (0.8 + 0.4 * random.random())


def error_text(resp: httpx.Response) -> str:
    """Short platform error message from the body (never echoes headers/tokens)."""
    try:
        data = resp.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return resp.text[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            parts = [str(err.get(k)) for k in ("code", "message") if err.get(k)]
            if parts:
                return ": ".join(parts)
        elif isinstance(err, str):
            return str(data.get("error_description") or err)
        for key in ("detail", "title", "message"):
            if data.get(key):
                return str(data[key])
        errors = data.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or errors[0])
    return json.dumps(data, ensure_ascii=False)[:300]


async def send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    platform: str,
    sleep: Sleep = asyncio.sleep,
    ok: tuple[int, ...] = (200, 201),
    retries: int = HTTP_RETRIES,
    **kwargs: Any,
) -> httpx.Response:
    """One API call with backoff on 429/5xx/network errors; maps failures to
    `PublishError`. Statuses in `ok` are returned as-is."""
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last = f"ağ hatası: {type(exc).__name__}"
        else:
            if resp.status_code in ok:
                return resp
            if resp.status_code == 401:
                raise AuthError(
                    f"{platform}: yetki reddedildi ({error_text(resp)}); hesabı yeniden bağla"
                )
            if resp.status_code not in RETRY_STATUS:
                raise PublishError(
                    f"{platform}: HTTP {resp.status_code} {error_text(resp)}",
                    retry=False,
                    status=resp.status_code,
                )
            last = f"HTTP {resp.status_code} {error_text(resp)}"
            wait = retry_after(resp)
            if wait is not None and attempt < retries:
                log.info("rate limited", extra={"platform": platform, "wait": wait})
                await sleep(min(wait, BACKOFF_MAX))
                continue
        if attempt < retries:
            await sleep(backoff(attempt))
    raise PublishError(f"{platform}: {last} (tekrar denenecek)", retry=True)


def read_chunk(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as f:
        f.seek(offset)
        return f.read(size)
