from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from clipper import chatlog, ingest
from clipper.config import Config, PathsConfig, TwitchConfig
from clipper.platforms.twitch.gql import GQLOperation, TwitchGQL, TwitchGQLError
from clipper.platforms.twitch.vod_chat import iter_vod_comments

VOD_ID = "987654"


def _comment(cid: str, offset: float, uid: str, frags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cursor": f"cur-{cid}",
        "node": {
            "id": cid,
            "contentOffsetSeconds": offset,
            "commenter": {"id": uid, "login": f"u{uid}"},
            "message": {"fragments": frags},
        },
    }


COMMENT_PAGES = [
    {
        "edges": [
            _comment("a", 1.0, "1", [{"text": "selam", "emote": None}]),
            _comment(
                "b",
                2.0,
                "2",
                [{"text": "KEKW", "emote": {"emoteID": "9"}}, {"text": " haha", "emote": None}],
            ),
        ],
        "pageInfo": {"hasNextPage": True},
    },
    {
        "edges": [
            _comment("b", 2.0, "2", [{"text": "dup", "emote": None}]),
            _comment("c", 4.0, "3", [{"text": "gg", "emote": None}]),
        ],
        "pageInfo": {"hasNextPage": False},
    },
]


class FakeTwitch:
    """Serves GQL, usher and segment URLs from memory."""

    def __init__(self, segments: list[Path]) -> None:
        self.segments = segments
        self.gql_bodies: list[dict[str, Any]] = []
        self.flaky_once = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "gql.twitch.tv":
            assert request.headers["Client-ID"] == TwitchConfig().client_id
            ops = json.loads(request.content)
            return httpx.Response(200, json=[self._gql(op) for op in ops])
        if url.host == "usher.ttvnw.net":
            assert url.params["nauth"] == "TOKEN" and url.params["nauthsig"] == "SIG"
            master = (
                "#EXTM3U\n"
                '#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,VIDEO="chunked"\n'
                "https://cdn.test/vod/chunked/index-dvr.m3u8\n"
                '#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="720p30",NAME="720p30"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,VIDEO="720p30"\n'
                "https://cdn.test/vod/720p30/index-dvr.m3u8\n"
            )
            return httpx.Response(200, text=master)
        if url.host == "cdn.test":
            if url.path.endswith("index-dvr.m3u8"):
                assert "chunked" in url.path  # 1080p60 preferred
                lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2"]
                for i, _ in enumerate(self.segments):
                    lines += ["#EXTINF:2.000,", f"{i}.ts"]
                lines.append("#EXT-X-ENDLIST")
                return httpx.Response(200, text="\n".join(lines) + "\n")
            idx = int(url.path.rsplit("/", 1)[1].split(".")[0])
            if idx == 1 and self.flaky_once:
                self.flaky_once = False
                return httpx.Response(503)
            return httpx.Response(200, content=self.segments[idx].read_bytes())
        return httpx.Response(404)

    def _gql(self, op: dict[str, Any]) -> dict[str, Any]:
        self.gql_bodies.append(op)
        name = op["operationName"]
        if name == "VideoMetadata":
            return {
                "data": {
                    "video": {
                        "id": VOD_ID,
                        "title": "Efsane yayın",
                        "createdAt": "2026-11-17T18:00:00Z",
                        "lengthSeconds": 6,
                        "owner": {"id": "1", "login": "yayinci", "displayName": "Yayıncı"},
                        "game": {"name": "Just Chatting"},
                    }
                }
            }
        if name == "PlaybackAccessToken":
            assert op["variables"]["isVod"] is True
            return {"data": {"videoPlaybackAccessToken": {"value": "TOKEN", "signature": "SIG"}}}
        if name == "VideoCommentsByOffsetOrCursor":
            assert "extensions" in op  # persisted query
            page = 0 if "contentOffsetSeconds" in op["variables"] else 1
            if page == 1:
                assert op["variables"]["cursor"] == "cur-b"
            return {"data": {"video": {"comments": COMMENT_PAGES[page]}}}
        return {"errors": [{"message": f"unknown op {name}"}]}


def test_vod_comments_paging() -> None:
    fake = FakeTwitch([])

    async def go() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            gql = TwitchGQL(client, TwitchConfig())
            return [m async for m in iter_vod_comments(gql, VOD_ID)]

    msgs = asyncio.run(go())
    assert [m.ts for m in msgs] == [1.0, 2.0, 4.0]
    assert msgs[1].text == "KEKW haha" and msgs[1].emotes == ["KEKW"]


def test_gql_errors_are_clear() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"errors": [{"message": "PersistedQueryNotFound"}]}])

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await TwitchGQL(client, TwitchConfig()).run(
                GQLOperation("VideoCommentsByOffsetOrCursor", {})
            )

    with pytest.raises(TwitchGQLError, match="persisted_queries"):
        asyncio.run(go())

    def http_err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    async def go2() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(http_err)) as client:
            await TwitchGQL(client, TwitchConfig()).run(GQLOperation("X", {}, "query X { a }"))

    with pytest.raises(TwitchGQLError, match="HTTP 400"):
        asyncio.run(go2())


def test_full_twitch_vod_ingest(
    ts_segments: list[Path],
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("clipper.platforms.twitch.vod.asyncio.sleep", _no_sleep)
    fake = FakeTwitch(ts_segments)
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))

    async def go() -> ingest.IngestResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            return await ingest.ingest_twitch_vod(conn, cfg, client, VOD_ID)

    res = asyncio.run(go())
    assert 5.5 < res.duration < 6.6
    assert res.chat_count == 3
    st = conn.execute("SELECT * FROM streamers").fetchone()
    assert (st["platform"], st["channel"], st["display_name"]) == ("twitch", "yayinci", "Yayıncı")
    sess = conn.execute("SELECT * FROM sessions").fetchone()
    assert sess["title"] == "Efsane yayın" and sess["status"] == "ended"
    msgs = list(chatlog.read_jsonl(res.session_dir / "chat.jsonl.gz"))
    assert [m.ts for m in msgs] == [1.0, 2.0, 4.0]
    work = tmp_path / "data" / "downloads" / f"twitch-{VOD_ID}"
    assert not (work / "parts").exists() and not (work / "video.mp4").exists()


async def _no_sleep(_s: float) -> None:
    return None
