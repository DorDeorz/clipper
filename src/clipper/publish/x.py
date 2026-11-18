"""X: v2 chunked media upload + POST /2/tweets (docs.x.com/x-api/media/quickstart/
media-upload-chunked).

initialize (JSON media_type/total_bytes/media_category) → append (multipart
segment_index + media, ≤ 5 MB) → finalize → poll `GET /2/media/upload?command=STATUS`
while `processing_info.state` is pending/in_progress → POST /2/tweets
`{"text", "media": {"media_ids": [id]}}`. No links in the text (pricier posts).
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
    read_chunk,
    send,
)

API = "https://api.x.com/2"
MAX_POLLS = 60


class XPublisher:
    platform = "x"

    def __init__(
        self,
        cfg: PublishConfig,
        client: httpx.AsyncClient,
        tokens: TokenGetter,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.tokens = tokens
        self.sleep = sleep
        self.clock = clock
        self.chunk = min(5 * 1024 * 1024, max(1, cfg.x_chunk_mb) * 1024 * 1024)

    async def _h(self, account: str) -> dict[str, str]:
        tok = await self.tokens(self.platform, account)
        return {"Authorization": f"Bearer {tok.token}"}

    async def _req(self, method: str, url: str, account: str, **kw: Any) -> httpx.Response:
        return await send(
            self.client,
            method,
            url,
            platform=self.platform,
            sleep=self.sleep,
            headers=await self._h(account),
            **kw,
        )

    async def upload(
        self, clip: ClipForPublish, account: str, state: dict[str, Any], save: SaveState
    ) -> PublishResult:
        total = clip.size
        if state.get("tweet_id"):
            return self._result(state["tweet_id"], state)
        media_id = state.get("media_id")
        if media_id and float(state.get("expires_at") or 0) <= self.clock():
            media_id = None
        if not media_id:
            resp = await self._req(
                "POST",
                f"{API}/media/upload/initialize",
                account,
                json={
                    "media_type": "video/mp4",
                    "total_bytes": total,
                    "media_category": "tweet_video",
                },
            )
            data = resp.json()["data"]
            media_id = str(data["id"])
            state.clear()
            state.update(
                media_id=media_id,
                next_segment=0,
                finalized=False,
                expires_at=self.clock() + float(data.get("expires_after_secs") or 86400),
            )
            save(state)
        if not state.get("finalized"):
            seg = int(state.get("next_segment") or 0)
            while seg * self.chunk < total:
                body = await asyncio.to_thread(read_chunk, clip.path, seg * self.chunk, self.chunk)
                await self._req(
                    "POST",
                    f"{API}/media/upload/{media_id}/append",
                    account,
                    ok=(200, 201, 204),
                    data={"segment_index": str(seg)},
                    files={"media": ("clip.mp4", body, "application/octet-stream")},
                )
                seg += 1
                state["next_segment"] = seg
                save(state)
            resp = await self._req("POST", f"{API}/media/upload/{media_id}/finalize", account)
            state["finalized"] = True
            save(state)
            info = (resp.json().get("data") or {}).get("processing_info")
            await self._wait(media_id, account, info)
        return await self._tweet(clip, account, media_id, state, save)

    async def _wait(self, media_id: str, account: str, info: dict[str, Any] | None) -> None:
        for _ in range(MAX_POLLS):
            if not info or info.get("state") == "succeeded":
                return
            if info.get("state") == "failed":
                err = info.get("error") or {}
                raise PublishError(
                    f"x: video işlenemedi ({err.get('message') or err.get('name') or '-'})",
                    retry=False,
                )
            await self.sleep(float(info.get("check_after_secs") or 5))
            resp = await self._req(
                "GET",
                f"{API}/media/upload",
                account,
                params={"command": "STATUS", "media_id": media_id},
            )
            info = (resp.json().get("data") or {}).get("processing_info")
        raise PublishError("x: video işleme çok uzun sürdü, sonra denenecek")

    async def _tweet(
        self,
        clip: ClipForPublish,
        account: str,
        media_id: str,
        state: dict[str, Any],
        save: SaveState,
    ) -> PublishResult:
        resp = await self._req(
            "POST",
            f"{API}/tweets",
            account,
            json={"text": clip.x_text[:280], "media": {"media_ids": [media_id]}},
        )
        tweet_id = str(resp.json()["data"]["id"])
        state["tweet_id"] = tweet_id
        save(state)
        return self._result(tweet_id, state)

    def _result(self, tweet_id: str, state: dict[str, Any]) -> PublishResult:
        return PublishResult("published", tweet_id, f"https://x.com/i/status/{tweet_id}")

    async def check_status(
        self, account: str, remote_id: str, state: dict[str, Any]
    ) -> PublishResult:
        return self._result(remote_id, state)
