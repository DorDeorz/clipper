"""TikTok Content Posting API (developers.tiktok.com, checked 2026-11).

Default **inbox** mode (scope `video.upload`): POST /v2/post/publish/inbox/video/init/
→ PUT the file to `upload_url` in order (`Content-Range`, 206 per chunk, 201 at the
end) → POST /v2/post/publish/status/fetch/ until SEND_TO_USER_INBOX. The draft lands
in the user's TikTok app; they finish the post there.

**Direct** mode (scope `video.publish`) is coded but only runs when config says
`tiktok_mode: direct` *and* `tiktok_audit_passed: true` (the user confirms the audit).
Unaudited clients may only post SELF_ONLY.

Chunk rules (media transfer guide): chunks 5–64 MB, the last one may hold the
remainder (≤ 128 MB), files under 5 MB go as one chunk, total_chunk_count =
floor(size / chunk_size), 1–1000.
"""

from __future__ import annotations

import asyncio
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
    error_text,
    read_chunk,
    send,
)

API = "https://open.tiktokapis.com/v2"
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
MAX_POLLS = 40


def chunk_plan(size: int, chunk: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) following TikTok's rules."""
    if size < MIN_CHUNK:
        return size, 1
    chunk = max(MIN_CHUNK, min(MAX_CHUNK, chunk))
    return chunk, max(1, size // chunk)


def chunk_ranges(size: int, chunk: int, count: int) -> list[tuple[int, int]]:
    """[(first, last)] byte ranges; the last chunk takes the remainder."""
    out = []
    for i in range(count):
        first = i * chunk
        last = size - 1 if i == count - 1 else first + chunk - 1
        out.append((first, last))
    return out


class TikTokDirectDisabled(PublishError):
    def __init__(self) -> None:
        super().__init__(
            "tiktok: direct post kapalı (audit onayı yok); inbox modunu kullan", retry=False
        )


class TikTokPublisher:
    platform = "tiktok"

    def __init__(
        self,
        cfg: PublishConfig,
        client: httpx.AsyncClient,
        tokens: TokenGetter,
        *,
        sleep: Sleep = asyncio.sleep,
        poll_seconds: float = 5.0,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.tokens = tokens
        self.sleep = sleep
        self.poll_seconds = poll_seconds

    @property
    def direct(self) -> bool:
        return self.cfg.tiktok_mode == "direct" and self.cfg.tiktok_audit_passed

    async def _post(self, path: str, account: str, body: dict[str, Any]) -> dict[str, Any]:
        tok = await self.tokens(self.platform, account)
        resp = await send(
            self.client,
            "POST",
            f"{API}{path}",
            platform=self.platform,
            sleep=self.sleep,
            headers={
                "Authorization": f"Bearer {tok.token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=body,
        )
        data: dict[str, Any] = resp.json()
        err = data.get("error") or {}
        if err.get("code") not in (None, "ok"):
            raise PublishError(f"tiktok: {error_text(resp)}", retry=False)
        return data.get("data") or {}

    async def _init(
        self, clip: ClipForPublish, account: str, chunk: int, count: int
    ) -> dict[str, Any]:
        source = {
            "source": "FILE_UPLOAD",
            "video_size": clip.size,
            "chunk_size": chunk,
            "total_chunk_count": count,
        }
        if self.cfg.tiktok_mode == "direct" and not self.cfg.tiktok_audit_passed:
            raise TikTokDirectDisabled()
        if not self.direct:
            return await self._post(
                "/post/publish/inbox/video/init/", account, {"source_info": source}
            )
        info = await self._post("/post/publish/creator_info/query/", account, {})
        options = info.get("privacy_level_options") or []
        level = self.cfg.tiktok_privacy_level
        if level not in options:
            raise PublishError(
                f"tiktok: gizlilik seviyesi {level} bu hesapta yok (seçenekler: {options})",
                retry=False,
            )
        return await self._post(
            "/post/publish/video/init/",
            account,
            {
                "post_info": {"title": clip.caption[:2200], "privacy_level": level},
                "source_info": source,
            },
        )

    async def upload(
        self, clip: ClipForPublish, account: str, state: dict[str, Any], save: SaveState
    ) -> PublishResult:
        size = clip.size
        chunk, count = chunk_plan(size, self.cfg.tiktok_chunk_mb * 1024 * 1024)
        if not state.get("uploaded"):
            # upload_url lives 1 h and chunks must go in order: a fresh init each attempt.
            data = await self._init(clip, account, chunk, count)
            state.clear()
            state.update(publish_id=data["publish_id"], uploaded=False)
            save(state)
            url = data["upload_url"]
            for first, last in chunk_ranges(size, chunk, count):
                body = await asyncio.to_thread(read_chunk, clip.path, first, last - first + 1)
                await send(
                    self.client,
                    "PUT",
                    url,
                    platform=self.platform,
                    sleep=self.sleep,
                    ok=(200, 201, 206),
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {first}-{last}/{size}",
                    },
                    content=body,
                )
            state["uploaded"] = True
            save(state)
        return await self._poll(account, state["publish_id"], MAX_POLLS)

    async def _poll(self, account: str, publish_id: str, polls: int) -> PublishResult:
        for i in range(max(1, polls)):
            data = await self._post(
                "/post/publish/status/fetch/", account, {"publish_id": publish_id}
            )
            status = data.get("status")
            if status == "SEND_TO_USER_INBOX":
                return PublishResult("inbox", publish_id)
            if status == "PUBLISH_COMPLETE":
                ids = data.get("publicaly_available_post_id") or []
                url = f"https://www.tiktok.com/video/{ids[0]}" if ids else None
                return PublishResult("published", publish_id, url)
            if status == "FAILED":
                raise PublishError(
                    f"tiktok: yayın başarısız ({data.get('fail_reason')})", retry=False
                )
            if i < polls - 1:
                await self.sleep(self.poll_seconds)
        return PublishResult("processing", publish_id)

    async def check_status(
        self, account: str, remote_id: str, state: dict[str, Any]
    ) -> PublishResult:
        return await self._poll(account, remote_id, 1)
