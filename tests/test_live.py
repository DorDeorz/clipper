"""Faz 6: live pipeline — chat clients against fake WS servers, recorder against a fake
HLS server, streaming detector session, candidate merging, watcher, calibration."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

from clipper import db, jobs
from clipper.config import (
    Config,
    DetectorConfig,
    KickConfig,
    MonitoringConfig,
    PathsConfig,
    TwitchConfig,
    YouTubeConfig,
)
from clipper.detect.events import LiveEvent, read_events
from clipper.detect.merge import Draft
from clipper.live import candidates
from clipper.live.calibrate import add_mark, calibrate
from clipper.live.chat import reconnecting
from clipper.live.content import dedupe
from clipper.live.session import LiveSession, StreamSource
from clipper.live.watcher import LiveWatcher
from clipper.models import ChatMessage
from clipper.platforms.kick.chat_pusher import KickPusherChat, frame_items
from clipper.platforms.kick.live import parse_channel
from clipper.platforms.twitch import live as twitch_live
from clipper.platforms.twitch.chat_irc import TwitchIrcChat, emote_names, parse_line, to_items
from clipper.platforms.twitch.gql import TwitchGQL
from clipper.platforms.youtube.innertube import parse_live_response, parse_live_status
from clipper.recorder.hls_live import LiveRecorder, StreamOffline, prune_segments


class Sink:
    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []
        self.events: list[LiveEvent] = []
        self.gaps: list[tuple[float, float]] = []

    def message(self, msg: ChatMessage) -> None:
        self.messages.append(msg)

    def event(self, ev: LiveEvent) -> None:
        self.events.append(ev)

    def gap(self, start: float, end: float) -> None:
        self.gaps.append((start, end))


# -- Twitch IRC ---------------------------------------------------------------------------

PRIV = (
    "@badge-info=;color=#FF0000;display-name=Ali;emotes=25:0-4,6-10;"
    "tmi-sent-ts=1700000000123;user-id=42;bits=100 "
    ":ali!ali@ali.tmi.twitch.tv PRIVMSG #kanal :Kappa Kappa oha"
)


def test_irc_parse_message_emotes_bits() -> None:
    line = parse_line(PRIV)
    assert line.command == "PRIVMSG" and line.trailing == "Kappa Kappa oha"
    assert emote_names(line.trailing, line.tags["emotes"]) == ["Kappa", "Kappa"]
    msg, bits = to_items(line, now=1.0)
    assert isinstance(msg, ChatMessage) and msg.ts == 1700000000.123
    assert msg.platform == "twitch" and msg.user_id != "42" and len(msg.user_id) == 40
    assert isinstance(bits, LiveEvent) and bits.kind == "bits" and bits.amount == 100
    assert emote_names("çok güzel LUL", "1:10-12") == ["LUL"]  # code-point positions


def test_irc_usernotice_events() -> None:
    def ev(tags: str) -> list[Any]:
        return to_items(parse_line(f"@{tags};tmi-sent-ts=2000 :tmi USERNOTICE #k :hi"), 0)

    assert ev("msg-id=resub")[0].kind == "sub"
    assert ev("msg-id=submysterygift;msg-param-mass-gift-count=10")[0].amount == 10
    assert ev("msg-id=subgift;msg-param-community-gift-id=123") == []  # counted in bundle
    assert ev("msg-id=subgift")[0].kind == "gift_sub"
    raid = ev(r"msg-id=raid;msg-param-viewerCount=350;system-msg=a\sraid")[0]
    assert raid.kind == "raid" and raid.amount == 350 and raid.ts == 2.0
    assert ev("msg-id=announcement") == []


def test_twitch_irc_client_with_fake_server() -> None:
    received: list[str] = []

    async def handler(ws: Any) -> None:
        for _ in range(4):
            received.append(await ws.recv())
        await ws.send("PING :tmi.twitch.tv\r\n" + PRIV + "\r\n")
        received.append(await ws.recv())  # PONG
        await ws.close()

    async def go() -> Sink:
        sink = Sink()
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = TwitchIrcChat("#Kanal", url=f"ws://127.0.0.1:{port}")
            stop = asyncio.Event()
            with pytest.raises(websockets.ConnectionClosed):
                await client.connect_once(sink, stop)
        return sink

    sink = asyncio.run(go())
    assert received[0].startswith("CAP REQ") and received[2].startswith("NICK justinfan")
    assert received[3] == "JOIN #kanal" and received[4] == "PONG :tmi.twitch.tv"
    assert [m.text for m in sink.messages] == ["Kappa Kappa oha"]
    assert [e.kind for e in sink.events] == ["bits"]


def test_reconnecting_reports_gap_and_backs_off() -> None:
    calls: list[float] = []
    now = [100.0]

    async def flaky(sink: Any, stop: asyncio.Event) -> None:
        calls.append(now[0])
        now[0] += 5
        if len(calls) == 3:
            stop.set()
            return
        raise OSError("bağlantı koptu")

    async def go() -> Sink:
        sink = Sink()
        await asyncio.wait_for(
            reconnecting("t", flaky, sink, asyncio.Event(), max_backoff=0.01, clock=lambda: now[0]),
            5,
        )
        return sink

    sink = asyncio.run(go())
    assert len(calls) == 3 and len(sink.gaps) == 2
    assert all(b >= a for a, b in sink.gaps) and sink.gaps[0][0] == 105


# -- Kick Pusher --------------------------------------------------------------------------

KICK_MSG = {
    "event": "App\\Events\\ChatMessageEvent",
    "channel": "chatrooms.7.v2",
    "data": json.dumps(
        {
            "id": "x",
            "content": "oha [emote:37226:KEKW]",
            "created_at": "2026-11-18T20:00:01+00:00",
            "sender": {"id": 99, "username": "veli"},
        }
    ),
}


def test_kick_frames() -> None:
    events = KickConfig().pusher_events
    (msg,) = frame_items(KICK_MSG, events, now=0)
    assert isinstance(msg, ChatMessage) and msg.text == "oha KEKW" and msg.emotes == ["KEKW"]
    gift = {"event": "App\\Events\\GiftedSubscriptionsEvent",
            "data": json.dumps({"gifted_usernames": ["a", "b", "c"]})}  # fmt: skip
    (g,) = frame_items(gift, events, now=5)
    assert g.kind == "gift_sub" and g.amount == 3 and g.ts == 5
    host = {"event": "App\\Events\\StreamHostEvent", "data": {"number_viewers": 80}}
    assert frame_items(host, events, now=1)[0].kind == "raid"
    assert frame_items({"event": "App\\Events\\PinnedMessage"}, events, now=1) == []


def test_kick_pusher_client_with_fake_server() -> None:
    subs: list[str] = []

    async def handler(ws: Any) -> None:
        await ws.send(json.dumps({"event": "pusher:connection_established", "data": "{}"}))
        for _ in range(2):
            subs.append(json.loads(await ws.recv())["data"]["channel"])
        await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
        assert json.loads(await ws.recv())["event"] == "pusher:pong"
        await ws.send(json.dumps(KICK_MSG))
        await ws.send(json.dumps({"event": "App\\Events\\StopStreamBroadcast", "data": "{}"}))
        await asyncio.sleep(0.2)

    async def go() -> tuple[Sink, list[bool]]:
        sink = Sink()
        offline: list[bool] = []
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = KickConfig(pusher_url=f"ws://127.0.0.1:{port}")
            client = KickPusherChat(cfg, 7, 11)
            stop = asyncio.Event()
            client.on_offline = lambda: (offline.append(True), stop.set())  # type: ignore[func-returns-value]
            await asyncio.wait_for(client.connect_once(sink, stop), 5)
        return sink, offline

    sink, offline = asyncio.run(go())
    assert subs == ["chatrooms.7.v2", "channel.11"]
    assert [m.text for m in sink.messages] == ["oha KEKW"] and offline == [True]


def test_kick_channel_parse() -> None:
    ch = {
        "id": 11, "chatroom": {"id": 7}, "playback_url": "https://x/master.m3u8",
        "livestream": {"is_live": True, "start_time": "2026-11-18 19:00:00",
                       "viewer_count": 500, "session_title": "selam",
                       "categories": [{"name": "Just Chatting"}]},
    }  # fmt: skip
    info = parse_channel("veli", ch)
    assert info.live and info.chatroom_id == 7 and info.viewers == 500
    assert info.category == "Just Chatting" and info.started_at is not None
    assert not parse_channel("veli", {"id": 11, "livestream": None}).live


# -- YouTube ------------------------------------------------------------------------------


def test_youtube_live_parse() -> None:
    data = {
        "continuationContents": {
            "liveChatContinuation": {
                "continuations": [
                    {"timedContinuationData": {"continuation": "NEXT", "timeoutMs": 4000}}
                ],
                "actions": [
                    {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                        "timestampUsec": "1700000000000000", "authorExternalChannelId": "UCa",
                        "message": {"runs": [{"text": "oha "},
                                             {"emoji": {"emojiId": "😂", "shortcuts": [":j:"]}}]},
                    }}}},
                    {"addChatItemAction": {"item": {"liveChatMembershipItemRenderer": {
                        "timestampUsec": "1700000001000000", "authorExternalChannelId": "UCb",
                        "headerSubtext": {"runs": [{"text": "Hoş geldin"}]},
                    }}}},
                ],
            }
        }
    }  # fmt: skip
    page = parse_live_response(data)
    assert page.continuation == "NEXT" and page.timeout_ms == 4000
    assert [m.text for m in page.messages] == ["oha 😂", "Hoş geldin"]
    assert page.messages[1].weight == 1.5 and page.events[0].kind == "membership"
    assert parse_live_response({}).continuation is None
    html = (
        'var ytInitialPlayerResponse = {"videoDetails": {"videoId": "abc", "isLive": true,'
        ' "title": "canlı", "viewCount": "321"}};'
    )
    st = parse_live_status(html)
    assert st.video_id == "abc" and st.is_live and st.viewers == 321
    off = parse_live_status('var ytInitialPlayerResponse = {"videoDetails": {"videoId": "x"}};')
    assert off.video_id is None and not off.is_live


# -- Twitch GQL live ----------------------------------------------------------------------


def test_twitch_stream_status_and_clips() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        op = json.loads(request.content)[0]
        if op["operationName"] == "StreamStatus":
            users = [
                {"login": "ali", "id": "1", "stream": {
                    "id": "9", "type": "live", "createdAt": "2026-11-18T18:00:00Z",
                    "viewersCount": 1200, "title": "t", "game": {"name": "Valorant"}}},
                {"login": "veli", "id": "2", "stream": None},
                None,
            ]  # fmt: skip
            return httpx.Response(200, json=[{"data": {"users": users}}])
        edges = [
            {"node": {"slug": "new", "createdAt": "2026-11-18T18:30:30Z", "durationSeconds": 30}},
            {"node": {"slug": "old", "createdAt": "2026-11-18T17:00:00Z", "durationSeconds": 30}},
        ]
        return httpx.Response(200, json=[{"data": {"user": {"clips": {"edges": edges}}}}])

    async def go() -> tuple[Any, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            gql = TwitchGQL(c, TwitchConfig())
            st = await twitch_live.stream_status(gql, ["Ali", "veli", "nobody"])
            from clipper.util.timeutil import parse_iso

            clips = await twitch_live.recent_clips(gql, "ali", parse_iso("2026-11-18T18:00:00Z"))
            return st, clips

    st, clips = asyncio.run(go())
    assert st["ali"].live and st["ali"].viewers == 1200 and st["ali"].category == "Valorant"
    assert not st["veli"].live and "nobody" not in st
    (clip,) = clips
    assert clip.data["slug"] == "new" and clip.kind == "viewer_clip"


# -- recorder -----------------------------------------------------------------------------


def _playlist(first: int, n: int, *, t0: str = "2026-11-18T20:00:", ended: bool = False,
              ad: int | None = None) -> str:  # fmt: skip
    out = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
    for seq in range(first, first + n):
        out.append(f"#EXT-X-PROGRAM-DATE-TIME:{t0}{seq * 2:02d}.000Z")
        out.append(f"#EXTINF:2.000,{'Amazon' if seq == ad else 'live'}")
        out.append(f"seg{seq}.ts")
    if ended:
        out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Config]]:
    cfg = Config(
        paths=PathsConfig(data_dir=tmp_path / "data"),
        detector=DetectorConfig(warmup_seconds=20, lag_seconds=5, pre_roll=5, post_roll=5,
                                cooldown=10),
        monitoring=MonitoringConfig(check_every_seconds=0.05, offline_grace_seconds=0),
    )  # fmt: skip
    conn = db.open_db(cfg.db_path, check_same_thread=False)
    conn.execute(
        "INSERT INTO streamers (display_name, platform, channel, created_at)"
        " VALUES ('Ali', 'twitch', 'ali', 'x')"
    )
    yield conn, cfg
    conn.close()


def _session(conn: sqlite3.Connection, tmp_path: Path) -> int:
    conn.execute(
        "INSERT INTO sessions (streamer_id, source, started_at, dir, status)"
        " VALUES (1, 'live', 'x', ?, 'recording')",
        (str(tmp_path),),
    )
    return 1


def test_recorder_takes_new_segments_skips_ads_and_refreshes(world: Any, tmp_path: Path) -> None:
    conn, _ = world
    sid = _session(conn, tmp_path)
    from clipper.util.timeutil import parse_iso

    t0 = parse_iso("2026-11-18T20:00:00Z")
    plays = [_playlist(0, 3, ad=1), "403", _playlist(2, 3), _playlist(3, 3, ended=True)]
    resolves: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("master.m3u8"):
            body = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nmedia.m3u8\n"
            return httpx.Response(200, text=body)
        if path.endswith("media.m3u8"):
            body = plays.pop(0)
            return httpx.Response(403) if body == "403" else httpx.Response(200, text=body)
        return httpx.Response(200, content=f"TS{path}".encode())

    async def resolve() -> str:
        resolves.append(1)
        return "https://cdn.test/master.m3u8"

    async def go() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            seen: list[int] = []
            rec = LiveRecorder(
                c, conn, sid, tmp_path / "seg", resolve, ["best"], t0,
                on_segment=lambda s: seen.append(s.seq), sleep=_nosleep,
            )  # fmt: skip
            stats = await asyncio.wait_for(rec.run(asyncio.Event()), 5)
            return stats, seen

    stats, seen = asyncio.run(go())
    assert seen == [0, 2, 3, 4, 5] and stats.ads == 1 and stats.refreshes == 1
    assert len(resolves) == 2
    rows = conn.execute(
        "SELECT seq, pdt_start, is_ad, deleted FROM segments ORDER BY seq"
    ).fetchall()
    assert [(r[0], r[1]) for r in rows][:3] == [(0, 0.0), (1, 2.0), (2, 4.0)]
    assert rows[1]["is_ad"] == 1 and rows[1]["deleted"] == 1
    assert (tmp_path / "seg" / "5.ts").read_bytes() == b"TS/seg5.ts"


async def _nosleep(_s: float) -> None:
    await asyncio.sleep(0)


def test_recorder_gives_up_when_offline(world: Any, tmp_path: Path) -> None:
    conn, _ = world
    sid = _session(conn, tmp_path)
    now = [0.0]

    async def resolve() -> str:
        raise StreamOffline("kapalı")

    async def sleep(s: float) -> None:
        now[0] += s

    async def go() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ) as c:
            rec = LiveRecorder(c, conn, sid, tmp_path / "seg", resolve, ["best"], 0.0,
                               offline_after=30, clock=lambda: now[0], sleep=sleep)  # fmt: skip
            return await asyncio.wait_for(rec.run(asyncio.Event()), 5)

    stats = asyncio.run(go())
    assert stats.segments == 0 and stats.errors and now[0] > 30


def test_prune_keeps_segments_pending_cuts_need(world: Any, tmp_path: Path) -> None:
    conn, _ = world
    sid = _session(conn, tmp_path)
    for seq in range(10):
        f = tmp_path / f"{seq}.ts"
        f.write_bytes(b"x")
        conn.execute(
            "INSERT INTO segments (session_id, seq, path, pdt_start, duration) VALUES (?,?,?,?,2)",
            (sid, seq, str(f), seq * 100.0),
        )
    cur = conn.execute(
        "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score, features,"
        " status, created_at) VALUES (?, 310, 290, 320, 4, '{}', 'new', 'x')",
        (sid,),
    )
    jobs.enqueue(conn, "cut", {"candidate_id": cur.lastrowid}, "cpu")
    n = prune_segments(conn, sid, now=1000.0, buffer_minutes=5, margin=0)
    left = [r[0] for r in conn.execute("SELECT seq FROM segments WHERE deleted = 0 ORDER BY seq")]
    assert left == [3, 7, 8, 9] and n == 6
    assert not (tmp_path / "0.ts").exists() and (tmp_path / "3.ts").exists()


# -- candidates ---------------------------------------------------------------------------


def test_upsert_merges_across_sources_and_delays_cut(world: Any, tmp_path: Path) -> None:
    conn, _ = world
    sid = _session(conn, tmp_path)
    kw: dict[str, Any] = {"t0": 1000.0, "radius": 20, "max_seconds": 180, "now": 1100.0}
    a, merged = candidates.upsert(
        conn, sid, Draft(100, 60, 125, 4.0, ["reactive"], {"z": {"rate": 3}}), **kw
    )
    assert not merged
    job = conn.execute("SELECT run_after, payload FROM jobs").fetchone()
    assert job["run_after"] == 1000 + 125 + candidates.CUT_DELAY
    b, merged = candidates.upsert(
        conn, sid,
        Draft(150, 130, 170, 3.5, ["content"], {"content": {"kind": "story"}},
              content_score=85, content_reason="hikaye"),
        **kw,
    )  # fmt: skip
    assert merged and a == b
    row = conn.execute("SELECT * FROM candidates").fetchone()
    assert json.loads(row["sources"]) == ["reactive", "content"]
    assert (row["win_start"], row["win_end"], row["peak_ts"]) == (60, 170, 100)
    assert row["content_score"] == 85
    job = conn.execute("SELECT run_after, payload FROM jobs").fetchone()
    assert json.loads(job["payload"])["t1"] == 170 and job["run_after"] == 1000 + 170 + 20
    # a second reactive draft nearby stays separate
    _, merged = candidates.upsert(conn, sid, Draft(200, 180, 220, 4, ["reactive"]), **kw)
    assert not merged
    # once cut has started, nothing merges into it
    conn.execute("UPDATE candidates SET status = 'cut'")
    _, merged = candidates.upsert(conn, sid, Draft(100, 60, 125, 4, ["viewer_clip"]), **kw)
    assert not merged


# -- live session (streaming detector) ------------------------------------------------------


def _source(**kw: Any) -> StreamSource:
    async def resolve() -> str:
        raise StreamOffline("test")

    return StreamSource("twitch", "ali", resolve, None, "başlık", "Just Chatting", **kw)


def test_live_session_detects_spike_and_writes_everything(world: Any) -> None:
    conn, cfg = world
    streamer = conn.execute("SELECT * FROM streamers").fetchone()
    now = [10_000.0]
    sess = LiveSession(conn, cfg, streamer, _source(viewers=900), httpx.AsyncClient(),
                       clock=lambda: now[0], feed_delay=3, analyse_audio=False)  # fmt: skip
    sid = sess.open()
    import random

    rnd = random.Random(1)
    for sec in range(0, 120):
        n = 30 if 80 <= sec < 86 else rnd.randint(0, 2)
        for i in range(n):
            text = "KEKW KEKW" if n > 5 else "selam"
            sess.message(ChatMessage(10_000 + sec + i / 40, f"u{sec}-{i}", text, ["KEKW"]))
        now[0] = 10_000 + sec + 1
        sess.step(now[0])
    sess.event(LiveEvent(10_000 + 50, "sub"))
    sess.gap(10_000 + 100, 10_000 + 105)
    sess._finish("ended")
    rows = conn.execute("SELECT * FROM candidates WHERE session_id = ?", (sid,)).fetchall()
    assert len(rows) == 1
    c = rows[0]
    assert 80 <= c["peak_ts"] <= 92 and json.loads(c["sources"]) == ["reactive"]
    feats = json.loads(c["features"])
    assert feats["top_messages"][0]["text"] == "KEKW KEKW" and feats["viewers"] == 900
    assert feats["category"] == "Just Chatting"
    job = conn.execute("SELECT type, run_after FROM jobs").fetchone()
    assert job["type"] == "cut" and job["run_after"] >= 10_000 + c["win_end"]
    n_sig = conn.execute("SELECT COUNT(*) FROM signal_seconds WHERE session_id = ?", (sid,))
    assert n_sig.fetchone()[0] >= 110
    assert [e.kind for e in read_events(conn, sid)] == ["sub"]
    sdir = Path(conn.execute("SELECT dir FROM sessions WHERE id = ?", (sid,)).fetchone()[0])
    assert (sdir / "chat.jsonl.gz").is_file() and not (sdir / "chat.live.jsonl").exists()
    info = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    assert info["time_base"] == "wall" and info["t0"] == 10_000
    status = conn.execute("SELECT status FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
    assert status == "ended"


def test_live_viewer_clip_becomes_candidate(world: Any) -> None:
    conn, cfg = world
    streamer = conn.execute("SELECT * FROM streamers").fetchone()
    sess = LiveSession(conn, cfg, streamer, _source(), httpx.AsyncClient(),
                       clock=lambda: 5_000.0, analyse_audio=False)  # fmt: skip
    sid = sess.open()
    sess.event(LiveEvent(4_900, "viewer_clip", data={"slug": "s", "duration": 30}))
    c = conn.execute("SELECT sources, win_start FROM candidates WHERE session_id = ?", (sid,))
    row = c.fetchone()
    assert json.loads(row["sources"]) == ["viewer_clip"]
    assert row["win_start"] == 0.0  # clipped to session start


def test_live_session_run_until_recorder_gives_up(world: Any) -> None:
    conn, cfg = world
    streamer = conn.execute("SELECT * FROM streamers").fetchone()
    chat_calls: list[int] = []

    async def chat(sink: Any, stop: asyncio.Event) -> None:
        chat_calls.append(1)
        await stop.wait()

    src = _source()
    src.chat = chat

    async def go() -> Any:
        async with httpx.AsyncClient() as c:
            sess = LiveSession(conn, cfg, streamer, src, c, analyse_audio=False,
                               recorder_offline_after=0.5, tick=0.05)  # fmt: skip
            return await asyncio.wait_for(sess.run(), 10), sess.session_id

    _stats, sid = asyncio.run(go())
    assert chat_calls == [1]
    assert conn.execute("SELECT status FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == "ended"


# -- watcher --------------------------------------------------------------------------------


def test_watcher_starts_and_closes_sessions(world: Any) -> None:
    conn, cfg = world
    conn.execute(
        "INSERT INTO streamers (display_name, platform, channel, enabled, created_at)"
        " VALUES ('Kapalı', 'kick', 'k', 0, 'x')"
    )
    live = {"on": True}
    probes: list[list[int]] = []

    async def probe(rows: list[sqlite3.Row]) -> dict[int, StreamSource | None]:
        probes.append([r["id"] for r in rows])
        return {1: _never_ending_source() if live["on"] else None}

    async def go() -> LiveWatcher:
        async with httpx.AsyncClient() as c:
            w = LiveWatcher(
                conn, cfg, c, probe, session_kwargs={"analyse_audio": False, "stop_grace": 0.2}
            )
            stop = asyncio.Event()
            task = asyncio.create_task(w.run(stop))
            await asyncio.sleep(0.2)
            assert len(w.running) == 1
            live["on"] = False
            await asyncio.sleep(0.3)
            stop.set()
            await asyncio.wait_for(task, 10)
            return w

    w = asyncio.run(go())
    assert probes[0] == [1]  # disabled streamer is not probed
    rows = conn.execute("SELECT status FROM sessions").fetchall()
    assert [r[0] for r in rows] == ["ended"]  # one session, closed after going offline
    assert w.state.live[1] is False


def _never_ending_source() -> StreamSource:
    async def resolve() -> str:
        await asyncio.sleep(3600)
        return ""

    return StreamSource("twitch", "ali", resolve, None)


# -- calibration / content helpers ----------------------------------------------------------


def test_calibrate_from_marks(world: Any) -> None:
    conn, cfg = world
    streamer = conn.execute("SELECT * FROM streamers").fetchone()
    sess = LiveSession(conn, cfg, streamer, _source(), httpx.AsyncClient(), clock=lambda: 1000.0)
    sid = sess.open()
    for i, mark in enumerate((100, 300, 500)):
        add_mark(conn, 1, now=1000.0 + mark)
        delay = 12 + i  # chat peaks 12, 13, 14 s later
        conn.execute(
            "INSERT INTO signal_seconds (session_id, t, msgs, score) VALUES (?, ?, 50, 5)",
            (sid, mark + delay),
        )
    cal = calibrate(conn, 1)
    assert cal.marks == 3 and cal.delays == [12, 13, 14] and cal.suggestion == 13
    conn.execute("UPDATE sessions SET status = 'ended'")
    from clipper.live.calibrate import CalibrateError

    with pytest.raises(CalibrateError):
        add_mark(conn, 1)


def test_dedupe_overlap_words() -> None:
    from clipper.content.transcript import TLine, TWord

    lines = [
        TLine(0, 3, "bir iki üç", (TWord("bir", 0, 1, 1), TWord("iki", 1, 2, 1),
                                    TWord("üç", 2, 3, 1))),
        TLine(3, 4, "dört", (TWord("dört", 3, 4, 1),)),
    ]  # fmt: skip
    out = dedupe(lines, after=2.0)
    assert [ln.text for ln in out] == ["üç", "dört"] and out[0].t0 == 2


def test_youtube_config_unchanged() -> None:
    assert YouTubeConfig().base_url == "https://www.youtube.com"


def test_recorded_live_segments_cut_and_audio(
    world: Any, tmp_path: Path, ts_segments: list[Path]
) -> None:
    from clipper.detect.audio import segment_db
    from clipper.media import probe
    from clipper.recorder.cutter import cut
    from clipper.util.timeutil import parse_iso

    conn, _ = world
    sid = _session(conn, tmp_path)
    t0 = parse_iso("2026-11-18T19:59:59Z")  # session opened 1 s before the first segment
    body = _playlist(0, len(ts_segments), ended=True)

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name.endswith(".m3u8"):
            return httpx.Response(200, text=body)
        return httpx.Response(200, content=ts_segments[int(name[3:-3])].read_bytes())

    async def go() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:

            async def resolve() -> str:
                return "https://cdn.test/media.m3u8"

            rec = LiveRecorder(c, conn, sid, tmp_path / "seg", resolve, ["best"], t0,
                               sleep=_nosleep)  # fmt: skip
            await rec.run(asyncio.Event())
            first = conn.execute("SELECT path, pdt_start FROM segments WHERE seq = 0").fetchone()
            dbs = await segment_db(Path(first[0]), first[1])
            res = await cut(conn, sid, 2.0, 5.0, tmp_path / "clip")
            return dbs, res, await probe(res.raw_mp4)

    dbs, res, info = asyncio.run(go())
    assert min(dbs) == 1 and all(v > -40 for v in dbs.values())  # session time, tone audible
    assert res.t0 == pytest.approx(2.0) and info.has_audio
    assert abs(info.duration - 3.0) < 1.2
