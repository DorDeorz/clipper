"""Minimal kick.com web API client (unofficial, no account).

Requests are spaced by `request_interval_seconds` and retried on transient errors. Kick is
behind Cloudflare; if plain httpx starts getting 403 challenge pages, set
`kick.http_backend: curl_cffi` (optional dependency) in config.yaml.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from clipper.config import KickConfig
from clipper.log import get_logger

log = get_logger("kick.api")


class KickError(Exception):
    """User-facing (Turkish)."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class _Getter(Protocol):
    async def __call__(self, url: str, params: dict[str, str] | None) -> tuple[int, Any]: ...


def _headers(cfg: KickConfig) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Language": cfg.accept_language,
        "User-Agent": cfg.user_agent,
        "Referer": cfg.base_url + "/",
    }


def httpx_getter(client: httpx.AsyncClient, cfg: KickConfig) -> _Getter:
    async def get(url: str, params: dict[str, str] | None) -> tuple[int, Any]:
        resp = await client.get(url, params=params, headers=_headers(cfg), timeout=30)
        return resp.status_code, _json_or_none(resp.status_code, resp.text)

    return get


def curl_cffi_getter(cfg: KickConfig) -> _Getter:  # pragma: no cover - optional backend
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError as exc:
        raise KickError(
            "kick.http_backend=curl_cffi seçili ama curl_cffi kurulu değil (uv add curl_cffi)."
        ) from exc
    session = AsyncSession(impersonate=cfg.impersonate)

    async def get(url: str, params: dict[str, str] | None) -> tuple[int, Any]:
        resp = await session.get(url, params=params, headers=_headers(cfg), timeout=30)
        return resp.status_code, _json_or_none(resp.status_code, resp.text)

    return get


def _json_or_none(status: int, text: str) -> Any:
    import json

    if status != 200:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class KickAPI:
    def __init__(
        self,
        getter: _Getter,
        cfg: KickConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._get = getter
        self.cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _pace(self) -> None:
        async with self._lock:
            wait = self._last + self.cfg.request_interval_seconds - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._last = self._clock()

    async def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = self.cfg.base_url + path
        attempt = 0
        while True:
            await self._pace()
            try:
                status, data = await self._get(url, params)
            except httpx.HTTPError as exc:
                status, data = -1, None
                err: KickError = KickError(f"Kick'e bağlanılamadı: {exc}", transient=True)
            else:
                err = self._status_error(status, data, path)
            if status == 200 and data is not None:
                return data
            if not err.transient or attempt >= self.cfg.retries:
                raise err
            delay = 2.0**attempt
            attempt += 1
            log.warning("kick transient error", extra={"status": status, "delay": delay})
            await self._sleep(delay)

    @staticmethod
    def _status_error(status: int, data: Any, path: str) -> KickError:
        if status == 200:
            return KickError(
                "Kick geçersiz JSON döndürdü (Cloudflare sayfası olabilir).", transient=True
            )
        if status == 404:
            return KickError(f"Kick'te bulunamadı: {path}")
        if status == 403:
            return KickError(
                "Kick isteği reddetti (HTTP 403, Cloudflare). config.yaml'da"
                " kick.http_backend: curl_cffi deneyebilirsin."
            )
        if status == 429 or status >= 500:
            return KickError(f"Kick geçici hata: HTTP {status}", transient=True)
        return KickError(f"Kick HTTP {status}: {path} (uç nokta değişmiş olabilir, config: kick)")

    async def channel(self, slug: str) -> dict[str, Any]:
        data = await self.get_json(self.cfg.channel_path.format(slug=slug))
        if not isinstance(data, dict) or "id" not in data:
            raise KickError(f"Kick kanal cevabı beklenmeyen biçimde: {slug}")
        return data

    async def videos(self, slug: str) -> list[dict[str, Any]]:
        data = await self.get_json(self.cfg.videos_path.format(slug=slug))
        if not isinstance(data, list):
            raise KickError(f"Kick VOD listesi beklenmeyen biçimde: {slug}")
        return data

    async def video(self, uuid: str) -> dict[str, Any]:
        data = await self.get_json(self.cfg.video_path.format(uuid=uuid))
        if not isinstance(data, dict) or "livestream" not in data:
            raise KickError(f"Kick VOD cevabı beklenmeyen biçimde: {uuid}")
        return data

    async def messages(self, channel_id: int, start_time: str) -> list[dict[str, Any]]:
        data = await self.get_json(
            self.cfg.messages_path.format(channel_id=channel_id), {"start_time": start_time}
        )
        try:
            msgs = data["data"]["messages"]
        except (KeyError, TypeError) as exc:
            raise KickError(
                "Kick chat cevabı beklenmeyen biçimde (alanlar değişmiş olabilir)."
            ) from exc
        return list(msgs or [])
