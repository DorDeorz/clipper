from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from clipper import chatlog, ingest
from clipper.config import Config, KickConfig, PathsConfig
from clipper.platforms.kick.api import KickAPI, KickError, httpx_getter
from clipper.platforms.kick.vod import iter_vod_chat, parse_content, parse_video
from clipper.util.timeutil import parse_iso

UUID = "d54ab01e-bf42-4937-b8be-6e6c8798b810"
CHANNEL_ID = 1001317
PDT0 = "2026-11-17T13:57:58.000Z"  # first segment; livestream start_time is ~5 s later
T0 = parse_iso(PDT0)

# Trimmed from a live response (2026-11-18).
VIDEO = {
    "id": 134062116,
    "uuid": UUID,
    "source": "https://stream.kick.com/abc/media/hls/master.m3u8",
    "livestream": {
        "id": 138364122,
        "channel_id": CHANNEL_ID,
        "session_title": "baksana | !pindirim",
        "is_live": False,
        "start_time": "2026-11-17T13:58:03+00:00",
        "duration": 6000,  # ms
        "channel": {"id": CHANNEL_ID, "slug": "wtcn", "user": {"username": "wtcN"}},
        "categories": [{"name": "Just Chatting"}],
    },
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg(mid: str, ts: float, text: str, uid: int = 1) -> dict[str, Any]:
    return {
        "id": mid,
        "chat_id": CHANNEL_ID,
        "user_id": uid,
        "content": text,
        "type": "message",
        "created_at": _iso(ts),
        "sender": {"id": uid, "slug": f"u{uid}", "username": f"U{uid}"},
    }


class FakeKick:
    def __init__(
        self, segments: list[Path] | None = None, chat: list[dict[str, Any]] | None = None
    ):
        self.segments = segments or []
        self.chat = chat or []
        self.chat_starts: list[float] = []
        self.flaky_chat = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "kick.com":
            assert request.headers["Accept"] == "application/json"
            if url.path == f"/api/v1/video/{UUID}":
                return httpx.Response(200, json=VIDEO)
            if url.path == f"/api/v2/channels/{CHANNEL_ID}/messages":
                if self.flaky_chat:
                    self.flaky_chat = False
                    return httpx.Response(503)
                start = parse_iso(url.params["start_time"])
                self.chat_starts.append(start - T0)
                window = [m for m in self.chat if start <= parse_iso(m["created_at"]) < start + 5]
                return httpx.Response(
                    200, json={"status": {"error": False}, "data": {"messages": window[:3]}}
                )
            if url.path == "/api/v2/channels/wtcn/videos":
                return httpx.Response(200, json=[{"is_live": True, "duration": 0}])
            return httpx.Response(404)
        if url.host == "stream.kick.com":
            if url.path.endswith("master.m3u8"):
                return httpx.Response(
                    200,
                    text=(
                        "#EXTM3U\n"
                        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="tr",URI="subtitles/tr/playlist.m3u8"\n'
                        '#EXT-X-STREAM-INF:BANDWIDTH=5150000,RESOLUTION=1920x1080,VIDEO="1080p60"\n'
                        "1080p60/playlist.m3u8\n"
                        '#EXT-X-STREAM-INF:BANDWIDTH=3480000,RESOLUTION=1280x720,VIDEO="720p60"\n'
                        "720p60/playlist.m3u8\n"
                    ),
                )
            if url.path.endswith("playlist.m3u8"):
                lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-PLAYLIST-TYPE:EVENT"]
                for i, _ in enumerate(self.segments or [None]):
                    if i == 0:
                        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{PDT0}")
                    lines += ["#EXTINF:2.000,", f"{i}.ts"]
                lines.append("#EXT-X-ENDLIST")
                return httpx.Response(200, text="\n".join(lines) + "\n")
            idx = int(url.path.rsplit("/", 1)[1].split(".")[0])
            return httpx.Response(200, content=self.segments[idx].read_bytes())
        return httpx.Response(404)


def _cfg(tmp_path: Path) -> Config:
    return Config(
        paths=PathsConfig(data_dir=tmp_path / "data"),
        kick=KickConfig(request_interval_seconds=0, chat_page_hint=3),
    )


async def _no_sleep(_s: float) -> None:
    return None


def test_parse_content_emotes() -> None:
    text, emotes = parse_content("oha [emote:37226:KEKW] [emote:6406274:collectiblesNumberPlate]")
    assert text == "oha KEKW collectiblesNumberPlate"
    assert emotes == ["KEKW", "collectiblesNumberPlate"]


def test_parse_video() -> None:
    meta = parse_video(VIDEO)
    assert (meta.slug, meta.display_name, meta.channel_id) == ("wtcn", "wtcN", CHANNEL_ID)
    assert meta.duration == 6.0 and meta.category == "Just Chatting" and not meta.is_live
    assert meta.start_time == pytest.approx(T0 + 5)
    with pytest.raises(KickError, match="beklenmeyen"):
        parse_video({"uuid": "x", "livestream": {}})


def test_chat_windows_dedupe_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clipper.platforms.kick.api.asyncio.sleep", _no_sleep)
    # 4 messages in the first 3 s, window capped at 3 -> the next request starts at the
    # last returned second instead of skipping ahead and losing the 4th.
    chat = [_msg(f"m{i}", T0 + [0, 0, 1, 2][i], f"KEKW {i}", uid=i) for i in range(4)] + [
        _msg("late", T0 + 5, "[emote:1:LUL] sonra"),
        _msg("after", T0 + 7, "VOD sonrası"),  # beyond duration (6 s)
    ]
    fake = FakeKick(chat=chat)

    async def go() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            api = KickAPI(
                httpx_getter(client, KickConfig()),
                KickConfig(request_interval_seconds=0, chat_page_hint=3),
            )
            return [m async for m in iter_vod_chat(api, parse_video(VIDEO), T0)]

    msgs = asyncio.run(go())
    assert [m.text for m in msgs] == ["KEKW 0", "KEKW 1", "KEKW 2", "KEKW 3", "LUL sonra"]
    assert msgs[-1].emotes == ["LUL"] and msgs[-1].ts == pytest.approx(5)
    assert all(m.platform == "kick" and len(m.user_id) == 40 for m in msgs)
    assert fake.chat_starts[:2] == [0, 1]


def test_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clipper.platforms.kick.api.asyncio.sleep", _no_sleep)

    async def go(status: int) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(status, text="<html>"))
        async with httpx.AsyncClient(transport=transport) as client:
            cfg = KickConfig(request_interval_seconds=0)
            await KickAPI(httpx_getter(client, cfg), cfg).channel("x")

    with pytest.raises(KickError, match="Cloudflare"):
        asyncio.run(go(403))
    with pytest.raises(KickError, match="geçici"):
        asyncio.run(go(502))
    with pytest.raises(KickError, match="bulunamadı"):
        asyncio.run(go(404))


def test_full_kick_vod_ingest(
    ts_segments: list[Path],
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("clipper.platforms.kick.api.asyncio.sleep", _no_sleep)
    chat = [_msg("a", T0 + 1.0, "selam"), _msg("b", T0 + 4.0, "[emote:9:KEKW]", uid=2)]
    fake = FakeKick(ts_segments, chat)
    cfg = _cfg(tmp_path)

    async def go() -> ingest.IngestResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            return await ingest.ingest_kick_vod(conn, cfg, client, UUID)

    res = asyncio.run(go())
    assert 5.5 < res.duration < 6.6 and res.chat_count == 2
    st = conn.execute("SELECT * FROM streamers").fetchone()
    assert (st["platform"], st["channel"], st["display_name"]) == ("kick", "wtcn", "wtcN")
    sess = conn.execute("SELECT * FROM sessions").fetchone()
    assert sess["title"] == "baksana | !pindirim" and sess["category"] == "Just Chatting"
    msgs = list(chatlog.read_jsonl(res.session_dir / "chat.jsonl.gz"))
    assert [m.ts for m in msgs] == [1.0, 4.0]  # relative to the first segment's PDT
    assert msgs[1].emotes == ["KEKW"]


def test_kick_vods_cli_skips_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from clipper import __main__ as cli

    monkeypatch.chdir(tmp_path)
    fake = FakeKick()
    real = httpx.AsyncClient

    def client_factory(*a: Any, **k: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(fake))

    monkeypatch.setattr(cli.httpx, "AsyncClient", client_factory)
    assert cli.main(["kick-vods", "wtcn"]) == 1


# ---- new-style share links (UUIDv7) ----------------------------------------------------

V7_LINK = "01a1f03e-53f8-7112-8fd8-8c495e2de163"  # 2026-11-18 19:59:39 UTC
LIST = [
    {"video": {"uuid": "old-earlier"}, "start_time": "2026-11-17 19:00:00", "duration": 3_600_000},
    {"video": {"uuid": "right-one"}, "start_time": "2026-11-18 19:59:30", "duration": 14_220_000},
    {"video": {"uuid": "live-now"}, "start_time": "2026-11-18 20:00:00", "duration": 0,
     "is_live": True},
]  # fmt: skip


def test_uuid7_time() -> None:
    from datetime import UTC, datetime

    ts = ingest.uuid7_time(V7_LINK)
    assert ts is not None
    assert datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M") == "2026-11-18 19:59"
    assert ingest.uuid7_time(UUID) is None  # API-style v4 uuid passes through
    assert ingest.uuid7_time("nonsense") is None


def test_match_kick_video_by_time() -> None:
    ts = ingest.uuid7_time(V7_LINK)
    assert ts is not None
    assert ingest.match_kick_video(LIST, ts) == "right-one"
    assert ingest.match_kick_video(LIST, ts + 3 * 86400) is None
    assert ingest.match_kick_video(LIST[:1], ts) is None


def test_resolve_kick_uuid() -> None:
    class Api:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def videos(self, slug: str) -> list[dict[str, object]]:
            self.calls.append(slug)
            return LIST

    api = Api()
    said: list[str] = []
    got = asyncio.run(ingest.resolve_kick_uuid(api, V7_LINK, "BerkCan", said.append))  # type: ignore[arg-type]
    assert got == "right-one" and api.calls == ["berkcan"] and said
    assert asyncio.run(ingest.resolve_kick_uuid(api, UUID, None)) == UUID  # type: ignore[arg-type]
    with pytest.raises(ingest.IngestError, match="linkin tamamını"):
        asyncio.run(ingest.resolve_kick_uuid(api, V7_LINK, None))  # type: ignore[arg-type]
    late = "01a2" + V7_LINK[4:]  # far in the future: nothing matches
    with pytest.raises(ingest.IngestError, match="bulunamadı"):
        asyncio.run(ingest.resolve_kick_uuid(api, late, "berkcan"))  # type: ignore[arg-type]
