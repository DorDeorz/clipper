# ruff: noqa: E501
"""TESTPLAN seviye 2: components against the local fake platform servers (tests/fakes)."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

from clipper import db
from clipper.config import Config, KickConfig, PathsConfig, YouTubeConfig
from clipper.live.chat import reconnecting
from clipper.platforms.kick.api import KickAPI, KickError, httpx_getter
from clipper.platforms.kick.chat_pusher import KickPusherChat
from clipper.platforms.twitch.chat_irc import TwitchIrcChat
from clipper.platforms.youtube.innertube import YouTubeChatEnded, YouTubeLiveChat, live_status
from clipper.recorder.hls_live import LiveRecorder
from tests.fakes.fake_kick import FakeKickApi, FakePusher, chat_event
from tests.fakes.fake_twitch import T0, FakeHls, FakeIrc, privmsg, usernotice
from tests.fakes.fake_youtube import FakeYouTube, text_item
from tests.test_live import Sink

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))
    c = db.open_db(cfg.db_path, check_same_thread=False)
    c.execute("INSERT INTO streamers (display_name, platform, channel, created_at)"
              " VALUES ('Ali', 'twitch', 'ali', 'x')")  # fmt: skip
    c.execute("INSERT INTO sessions (streamer_id, source, started_at, dir, status)"
              " VALUES (1, 'live', 'x', ?, 'recording')", (str(tmp_path),))  # fmt: skip
    yield c
    c.close()


async def _nosleep(_s: float) -> None:
    await asyncio.sleep(0)


def _record(conn: sqlite3.Connection, tmp_path: Path, fake: FakeHls) -> Any:
    async def resolve() -> str:
        return "https://usher.test/master.m3u8"

    async def go() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as c:
            rec = LiveRecorder(c, conn, 1, tmp_path / "seg", resolve, ["best"], T0,
                               sleep=_nosleep)  # fmt: skip
            return await asyncio.wait_for(rec.run(asyncio.Event()), 30)

    return asyncio.run(go())


def test_rec01_thirty_minutes_no_segment_loss(conn: sqlite3.Connection, tmp_path: Path) -> None:
    fake = FakeHls(total=900)  # 30 min of 2 s segments
    stats = _record(conn, tmp_path, fake)
    rows = conn.execute("SELECT seq, pdt_start FROM segments ORDER BY seq").fetchall()
    assert [r[0] for r in rows] == list(range(900)) and stats.segments == 900
    assert all(abs(r[1] - r[0] * 2.0) < 1e-6 for r in rows)  # PDT-based session time


def test_rec02_daterange_ads_marked(conn: sqlite3.Connection, tmp_path: Path) -> None:
    fake = FakeHls(total=40, ads=set(range(10, 16)))
    _record(conn, tmp_path, fake)
    ads = [r[0] for r in conn.execute("SELECT seq FROM segments WHERE is_ad = 1 ORDER BY seq")]
    assert ads == list(range(10, 16))
    assert not set(fake.served) & set(range(10, 16))  # ad segments are not even downloaded


def test_rec03_token_expiry_refresh_without_gap(conn: sqlite3.Connection, tmp_path: Path) -> None:
    fake = FakeHls(total=120, expire_on={8, 20})
    stats = _record(conn, tmp_path, fake)
    seqs = [r[0] for r in conn.execute("SELECT seq FROM segments ORDER BY seq")]
    assert seqs == list(range(120))  # gap ≤ 1 segment: here none
    assert stats.refreshes == 2 and fake.master_fetches == 3


def test_chat01_chat04_irc_drops_three_times(tmp_path: Path) -> None:
    ts = int(T0 * 1000)
    batches = [
        [privmsg("ali", "selam", ts), usernotice("subgift", ts + 1, **{"msg-param-mass-gift-count": "5"})],
        [privmsg("veli", "oha\\sbu", ts + 2000)],
        [privmsg("ayse", "KEKW", ts + 4000, emotes="1:0-3"),
         usernotice("raid", ts + 4001, **{"msg-param-viewerCount": "400"})],
        [privmsg("can", "son", ts + 6000)],
    ]  # fmt: skip
    fake = FakeIrc(batches)

    async def go() -> Sink:
        sink = Sink()
        stop = asyncio.Event()
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = TwitchIrcChat("#kanal", url=f"ws://127.0.0.1:{port}")

            async def stopper() -> None:
                while len(sink.messages) < 4:
                    await asyncio.sleep(0.02)
                stop.set()

            await asyncio.wait_for(
                asyncio.gather(reconnecting("twitch", client.connect_once, sink, stop,
                                            max_backoff=0.01), stopper()), 10)  # fmt: skip
        return sink

    sink = asyncio.run(go())
    assert [m.text for m in sink.messages] == ["selam", "oha\\sbu", "KEKW", "son"]
    assert fake.connections >= 4 and fake.pongs >= 4
    assert len(sink.gaps) >= 3  # each drop is reported as a chat gap
    kinds = sorted(e.kind for e in sink.events)
    assert "raid" in kinds and any(k in kinds for k in ("gift_sub", "sub"))


def test_chat02_kick_pusher_double_encoded(tmp_path: Path) -> None:
    frames = [chat_event("oha [emote:37226:KEKW]", "veli", 9, "2026-11-18T20:00:01+00:00"),
              {"event": "App\\Events\\UnknownNewEvent", "data": "{\"x\": 1}"},
              chat_event("ikinci", "ali", 10, "2026-11-18T20:00:02+00:00")]  # fmt: skip
    fake = FakePusher(frames)

    async def go() -> Sink:
        sink = Sink()
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = KickPusherChat(KickConfig(pusher_url=f"ws://127.0.0.1:{port}"), 7, 11)
            with pytest.raises(websockets.ConnectionClosed):
                await asyncio.wait_for(client.connect_once(sink, asyncio.Event()), 5)
        return sink

    sink = asyncio.run(go())
    assert [m.text for m in sink.messages] == ["oha KEKW", "ikinci"]  # unknown event ignored
    assert fake.subscribed == ["chatrooms.7.v2", "channel.11"]


def test_chaos08_kick_cloudflare_403_is_clear(tmp_path: Path) -> None:
    fake = FakeKickApi(cloudflare=True)

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as c:
            api = KickAPI(httpx_getter(c, KickConfig()), KickConfig(), sleep=_nosleep)
            await api.channel("veli")

    with pytest.raises(KickError) as e:
        asyncio.run(go())
    assert "403" in str(e.value) and "Cloudflare" in str(e.value) and fake.calls == 1


def _yt_chat(fake: FakeYouTube) -> tuple[Sink, list[float], BaseException | None]:
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    async def go() -> tuple[Sink, BaseException | None]:
        sink = Sink()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as c:
            st = await live_status(c, YouTubeConfig(), "kanal")
            assert st.is_live and st.video_id == fake.video_id
            chat = YouTubeLiveChat(c, YouTubeConfig(), st.video_id, sleep=sleep)
            try:
                await chat.connect_once(sink, asyncio.Event())
            except Exception as exc:
                return sink, exc
        return sink, None

    sink, err = asyncio.run(go())
    return sink, waits, err


def test_chat03_youtube_continuation_chain() -> None:
    us = int(T0 * 1_000_000)
    pages = [[text_item(us, "UCa", "bir")], [text_item(us + 1, "UCb", "iki"),
             text_item(us + 1, "UCb", "iki")], [text_item(us + 2, "UCc", "üç")]]  # fmt: skip
    fake = FakeYouTube(pages=pages, timeout_ms=1500)
    sink, waits, err = _yt_chat(fake)
    assert [m.text for m in sink.messages] == ["bir", "iki", "üç"]  # duplicate dropped
    assert fake.tokens == ["C0", "C1", "C2", "C3"] and waits == [1.5, 1.5, 1.5]
    assert isinstance(err, YouTubeChatEnded)


def test_chaos09_youtube_layout_change_is_detected() -> None:
    us = int(T0 * 1_000_000)
    fake = FakeYouTube(pages=[[text_item(us, "UCa", "bir")]] * 5, rename=True)
    sink, _waits, err = _yt_chat(fake)
    # Renamed renderers: no garbage messages, and a clear parse alarm instead of silence.
    assert sink.messages == []
    assert err is not None and not isinstance(err, YouTubeChatEnded)
    assert "yapısı değişmiş" in str(err)


def test_publishers_survive_429_5xx_and_drops(tmp_path: Path) -> None:
    from tests.fakes.faults import Faulty
    from tests.test_publish import FakeYouTube, _yt, run, video

    clip = video(tmp_path, 700_000)
    fake = FakeYouTube(clip.size)
    faulty = Faulty(fake, plan={2: "429", 3: "500", 5: "drop"}, retry_after="2")
    res = run(_yt(faulty).upload(clip, "main", {}, lambda s: None))
    assert res.status == "published" and bytes(fake.data) == clip.path.read_bytes()
    assert faulty.injected == ["429", "500", "drop"]


def test_publisher_expired_token_asks_to_reconnect(tmp_path: Path) -> None:
    from clipper.publish.base import AuthError
    from tests.fakes.faults import Faulty
    from tests.test_publish import FakeYouTube, _yt, run, video

    clip = video(tmp_path, 1000)
    with pytest.raises(AuthError, match="yeniden bağla"):
        run(_yt(Faulty(FakeYouTube(clip.size), plan={1: "401"})).upload(clip, "main", {},
                                                                          lambda s: None))  # fmt: skip
