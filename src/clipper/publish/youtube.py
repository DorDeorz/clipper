"""YouTube resumable upload (YouTube Data API v3 guide "Resumable Uploads").

1. POST .../upload/youtube/v3/videos?uploadType=resumable&part=snippet,status with the
   video resource; the session URI comes back in `Location`.
2. PUT chunks (multiple of 256 KiB) with `Content-Range: bytes a-b/total`; 308 means
   "continue", its `Range` header says what arrived. 201/200 carries the video resource.
3. After a drop: empty PUT with `Content-Range: bytes */total` asks where to resume.
   404 → session expired, start over.
Quota (determine_quota_cost, 2026-11): videos.insert has its own bucket, 100 calls/day.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from clipper.config import PublishConfig
from clipper.publish.base import (
    AuthError,
    ClipForPublish,
    PublishError,
    PublishResult,
    SaveState,
    Sleep,
    TokenGetter,
    backoff,
    error_text,
    read_chunk,
    send,
)

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK_UNIT = 256 * 1024
RETRY_STATUS = {500, 502, 503, 504}
MAX_RESUMES = 6


def video_resource(cfg: PublishConfig, clip: ClipForPublish) -> dict[str, Any]:
    return {
        "snippet": {
            "title": clip.title[:100],
            "description": clip.description[:5000],
            "tags": clip.tags[:30],
            "categoryId": cfg.youtube_category_id,
        },
        "status": {
            "privacyStatus": cfg.youtube_privacy,
            "selfDeclaredMadeForKids": False,
        },
    }


def _next_offset(resp: httpx.Response) -> int:
    rng = resp.headers.get("range")  # "bytes=0-999999"
    if not rng:
        return 0
    return int(rng.rsplit("-", 1)[1]) + 1


def _done(resp: httpx.Response) -> PublishResult:
    vid = resp.json().get("id")
    if not vid:
        raise PublishError("youtube: yanıt video kimliği içermiyor", retry=False)
    return PublishResult("published", vid, f"https://www.youtube.com/shorts/{vid}")


class YouTubePublisher:
    platform = "youtube"

    def __init__(
        self,
        cfg: PublishConfig,
        client: httpx.AsyncClient,
        tokens: TokenGetter,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.tokens = tokens
        self.sleep = sleep
        self.chunk = max(CHUNK_UNIT, cfg.youtube_chunk_mb * 1024 * 1024 // CHUNK_UNIT * CHUNK_UNIT)

    async def _auth(self, account: str) -> dict[str, str]:
        tok = await self.tokens(self.platform, account)
        return {"Authorization": f"Bearer {tok.token}"}

    async def _start(self, clip: ClipForPublish, account: str) -> str:
        resp = await send(
            self.client,
            "POST",
            UPLOAD_URL,
            platform=self.platform,
            sleep=self.sleep,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                **await self._auth(account),
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(clip.size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json=video_resource(self.cfg, clip),
        )
        loc = resp.headers.get("location")
        if not loc:
            raise PublishError("youtube: upload oturumu adresi gelmedi")
        return loc

    async def _query(self, uri: str, total: int, account: str) -> httpx.Response | int | None:
        """Where to resume: an offset, a finished response, or None (session gone)."""
        resp = await send(
            self.client,
            "PUT",
            uri,
            platform=self.platform,
            sleep=self.sleep,
            ok=(200, 201, 308, 404),
            headers={
                **await self._auth(account),
                "Content-Length": "0",
                "Content-Range": f"bytes */{total}",
            },
        )
        if resp.status_code == 404:
            return None
        if resp.status_code == 308:
            return _next_offset(resp)
        return resp

    async def upload(
        self, clip: ClipForPublish, account: str, state: dict[str, Any], save: SaveState
    ) -> PublishResult:
        total = clip.size
        uri = state.get("session_uri")
        offset = 0
        if uri:
            q = await self._query(uri, total, account)
            if isinstance(q, httpx.Response):
                return _done(q)
            if q is None:
                uri = None
            else:
                offset = q
        if not uri:
            uri = await self._start(clip, account)
            state["session_uri"] = uri
            save(state)
        resumes = 0
        while True:
            size = min(self.chunk, total - offset)
            body = await asyncio.to_thread(read_chunk, clip.path, offset, size)
            try:
                resp = await self.client.put(
                    uri,
                    content=body,
                    headers={
                        **await self._auth(account),
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{offset + size - 1}/{total}",
                    },
                )
            except httpx.TransportError:
                resp = None
            if resp is not None and resp.status_code in (200, 201):
                return _done(resp)
            if resp is not None and resp.status_code == 308:
                offset = _next_offset(resp)
                resumes = 0
                continue
            if resp is not None and resp.status_code == 404:
                state.pop("session_uri", None)
                save(state)
                raise PublishError("youtube: upload oturumu düştü, baştan denenecek")
            if resp is not None and resp.status_code not in RETRY_STATUS:
                if resp.status_code == 401:
                    raise AuthError("youtube: yetki reddedildi; hesabı yeniden bağla")
                raise PublishError(
                    f"youtube: HTTP {resp.status_code} {error_text(resp)}",
                    retry=False,
                    status=resp.status_code,
                )
            resumes += 1
            if resumes > MAX_RESUMES:
                raise PublishError("youtube: yükleme sürekli kopuyor, sonra devam edilecek")
            await self.sleep(backoff(resumes - 1))
            q = await self._query(uri, total, account)
            if isinstance(q, httpx.Response):
                return _done(q)
            if q is None:
                state.pop("session_uri", None)
                save(state)
                raise PublishError("youtube: upload oturumu düştü, baştan denenecek")
            offset = q

    async def check_status(
        self, account: str, remote_id: str, state: dict[str, Any]
    ) -> PublishResult:
        return PublishResult("published", remote_id, f"https://www.youtube.com/shorts/{remote_id}")
