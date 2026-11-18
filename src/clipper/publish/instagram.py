"""Instagram Reels via the resumable path (developers.facebook.com/docs/instagram-platform/
content-publishing). The resumable upload is only for apps on Facebook Login for
Business, so the host is graph.facebook.com and the token is the Page token.

1. POST /<ig_id>/media media_type=REELS upload_type=resumable caption share_to_feed
2. POST https://rupload.facebook.com/ig-api-upload/<v>/<container> (Authorization:
   OAuth <token>, offset, file_size; body = bytes)
3. GET /<container>?fields=status_code until FINISHED (ERROR / EXPIRED → failed)
4. POST /<ig_id>/media_publish creation_id=<container> → media id; permalink lookup.
Limit: GET /<ig_id>/content_publishing_limit before creating (100 posts / 24 h).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

from clipper.config import PublishConfig
from clipper.publish.base import (
    ClipForPublish,
    PublishError,
    PublishResult,
    SaveState,
    Sleep,
    TokenGetter,
    send,
)


class InstagramPublisher:
    platform = "instagram"

    def __init__(
        self,
        cfg: PublishConfig,
        client: httpx.AsyncClient,
        tokens: TokenGetter,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.tokens = tokens
        self.sleep = sleep
        self.clock = clock
        v = cfg.graph_api_version
        self.graph = f"https://graph.facebook.com/{v}"
        self.rupload = f"https://rupload.facebook.com/ig-api-upload/{v}"

    async def _call(self, method: str, path: str, token: str, **params: Any) -> dict[str, Any]:
        resp = await send(
            self.client,
            method,
            f"{self.graph}/{path}",
            platform=self.platform,
            sleep=self.sleep,
            params={**params, "access_token": token},
        )
        data: dict[str, Any] = resp.json()
        return data

    async def _ids(self, account: str) -> tuple[str, str]:
        tok = await self.tokens(self.platform, account)
        ig = tok.extra.get("ig_user_id")
        if not ig:
            raise PublishError(
                "instagram: hesap kaydında IG kullanıcı kimliği yok; yeniden bağla", retry=False
            )
        return tok.token, str(ig)

    async def _check_limit(self, token: str, ig: str) -> None:
        data = await self._call(
            "GET", f"{ig}/content_publishing_limit", token, fields="quota_usage,config"
        )
        rows = data.get("data") or []
        if not rows:
            return
        used = rows[0].get("quota_usage")
        total = (rows[0].get("config") or {}).get("quota_total")
        if isinstance(used, int) and isinstance(total, int) and used >= total:
            raise PublishError(f"instagram: 24 saatlik yayın limiti dolu ({used}/{total})")

    async def upload(
        self, clip: ClipForPublish, account: str, state: dict[str, Any], save: SaveState
    ) -> PublishResult:
        token, ig = await self._ids(account)
        container = state.get("container_id")
        if not container or state.get("container_error"):
            await self._check_limit(token, ig)
            data = await self._call(
                "POST",
                f"{ig}/media",
                token,
                media_type="REELS",
                upload_type="resumable",
                caption=clip.caption[:2200],
                share_to_feed="true",
            )
            container = str(data["id"])
            state.clear()
            state.update(container_id=container, uri=data.get("uri"), uploaded=False)
            save(state)
        if not state.get("uploaded"):
            uri = state.get("uri") or f"{self.rupload}/{container}"
            body = await asyncio.to_thread(clip.path.read_bytes)
            resp = await send(
                self.client,
                "POST",
                uri,
                platform=self.platform,
                sleep=self.sleep,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(len(body)),
                },
                content=body,
            )
            if not resp.json().get("success", True):
                raise PublishError("instagram: video yüklenemedi")
            state["uploaded"] = True
            save(state)
        return await self._finish(token, ig, container, state, save, wait=True)

    async def _finish(
        self,
        token: str,
        ig: str,
        container: str,
        state: dict[str, Any],
        save: SaveState,
        *,
        wait: bool,
    ) -> PublishResult:
        deadline = self.clock() + (self.cfg.instagram_poll_timeout_seconds if wait else 0)
        while True:
            data = await self._call("GET", container, token, fields="status_code,status")
            code = data.get("status_code")
            if code == "FINISHED":
                break
            if code == "PUBLISHED":
                return PublishResult("published", state.get("media_id"), state.get("url"))
            if code in ("ERROR", "EXPIRED"):
                state["container_error"] = True
                save(state)
                raise PublishError(
                    f"instagram: video işlenemedi ({code}: {data.get('status') or '-'})",
                    retry=code == "EXPIRED",
                )
            if self.clock() >= deadline:
                return PublishResult("processing", container)
            await self.sleep(self.cfg.instagram_poll_seconds)
        pub = await self._call("POST", f"{ig}/media_publish", token, creation_id=container)
        media_id = str(pub["id"])
        url = None
        try:
            info = await self._call("GET", media_id, token, fields="permalink")
            url = info.get("permalink")
        except PublishError:
            pass
        state.update(media_id=media_id, url=url)
        save(state)
        return PublishResult("published", media_id, url)

    async def check_status(
        self, account: str, remote_id: str, state: dict[str, Any]
    ) -> PublishResult:
        token, ig = await self._ids(account)
        container = state.get("container_id") or remote_id
        return await self._finish(token, ig, container, state, lambda s: None, wait=False)
