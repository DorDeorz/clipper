"""r3 detection additions: event filter, viewer clips, candidate merging."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from clipper.config import (
    Config,
    DetectorConfig,
    EventsConfig,
    MergeConfig,
    PathsConfig,
    TwitchConfig,
)
from clipper.detect.events import EventFilter, LiveEvent, events_near, read_events, write_events
from clipper.detect.merge import Draft, merge_drafts
from clipper.detect.pipeline import _thanks_dampening, analyze_session
from clipper.detect.signals import bin_messages
from clipper.ingest import ingest_local
from clipper.models import ChatMessage
from clipper.platforms.twitch.clips import clips_period, vod_clips
from clipper.platforms.twitch.gql import TwitchGQL
from tests.fixtures.make_fixtures import BASE_TS, DEFAULT_SPIKES

# -- event filter -------------------------------------------------------------------------


def test_sub_train_raises_threshold() -> None:
    f = EventFilter(EventsConfig(), [LiveEvent(100 + i * 0.5, "sub") for i in range(5)])
    assert f.state(99).threshold_mult == 1.0
    assert f.state(101).threshold_mult == 1.5 and f.state(101).reason == "sub_train"
    assert f.state(121).threshold_mult == 1.5  # last sub at 102 + 20 s
    assert f.state(123).threshold_mult == 1.0


def test_scattered_subs_do_nothing() -> None:
    f = EventFilter(EventsConfig(), [LiveEvent(i * 10.0, "sub") for i in range(10)])
    assert all(f.state(t).threshold_mult == 1.0 for t in range(100))


def test_gift_bundle_counts_as_train() -> None:
    f = EventFilter(EventsConfig(), [LiveEvent(50, "gift_sub", amount=10)])
    assert f.state(60).threshold_mult == 1.5


def test_raid_suppresses_for_a_minute() -> None:
    f = EventFilter(EventsConfig(), [LiveEvent(10, "raid", amount=300)])
    assert f.state(10).suppressed and f.state(69).suppressed
    assert not f.state(70).suppressed and not f.state(5).suppressed


def test_events_near_summary() -> None:
    ev = [
        LiveEvent(1, "bits", 100),
        LiveEvent(2, "bits", 50),
        LiveEvent(3, "gift_sub", 5),
        LiveEvent(4, "sub"),
        LiveEvent(50, "raid"),
    ]
    assert events_near(ev, 0, 10) == {"bits": 150, "gift_sub": 5, "sub": 1}


def test_events_roundtrip(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO streamers (display_name, platform, channel, created_at)"
        " VALUES ('a', 'twitch', 'a', 'x')"
    )
    conn.execute(
        "INSERT INTO sessions (streamer_id, source, started_at, dir, status)"
        " VALUES (1, 'vod', 'x', '.', 'ended')"
    )
    write_events(conn, 1, [LiveEvent(5, "viewer_clip", data={"slug": "s"}), LiveEvent(1, "sub")])
    got = read_events(conn, 1)
    assert [e.kind for e in got] == ["sub", "viewer_clip"] and got[1].data == {"slug": "s"}
    assert [e.kind for e in read_events(conn, 1, ["sub"])] == ["sub"]


def test_thanks_chat_is_dampened() -> None:
    msgs = [ChatMessage(10 + i * 0.1, f"u{i}", "hoş geldin abi") for i in range(20)]
    msgs += [ChatMessage(30 + i * 0.1, f"v{i}", "KEKW") for i in range(20)]
    bins = bin_messages(msgs, thanks_words=EventsConfig().thanks_words)
    damp = _thanks_dampening(bins, 0, 40, 5, 0.5)
    assert 11 in damp and 31 not in damp


# -- merging ------------------------------------------------------------------------------


def _d(src: str, a: float, b: float, score: float = 4.0, **kw: Any) -> Draft:
    return Draft((a + b) / 2, a, b, score, [src], **kw)


def test_merge_cross_source_within_radius() -> None:
    out = merge_drafts(
        [_d("reactive", 100, 185), _d("viewer_clip", 200, 230), _d("content", 400, 500)],
        radius=20,
    )
    assert len(out) == 2
    assert out[0].sources == ["reactive", "viewer_clip"]
    assert out[0].peak_ts == 142.5  # reactive peak wins
    assert (out[0].win_start, out[0].win_end) == (100, 230)
    assert out[0].features["sources"] == ["reactive", "viewer_clip"]
    assert out[1].sources == ["content"]


def test_merge_keeps_same_source_apart() -> None:
    out = merge_drafts([_d("reactive", 0, 85), _d("reactive", 90, 175)], radius=20)
    assert len(out) == 2


def test_merge_caps_length_and_keeps_content_score() -> None:
    out = merge_drafts(
        [
            _d("reactive", 100, 185),
            _d("content", 0, 180, content_score=88, content_reason="hikaye"),
        ],
        radius=20,
        max_seconds=180,
    )
    (m,) = out
    assert m.win_end - m.win_start <= 180 and m.win_start <= m.peak_ts <= m.win_end
    assert m.content_score == 88 and m.content_reason == "hikaye"


# -- pipeline -----------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    return Config(
        paths=PathsConfig(data_dir=tmp_path / "data"),
        detector=DetectorConfig(warmup_seconds=30, lag_seconds=5, pre_roll=10, post_roll=10),
        events=EventsConfig(raid_suppress_seconds=20),
        merge=MergeConfig(radius_seconds=5),
    )


@pytest.fixture
def session(fixture_dir: Path, tmp_path: Path, conn: sqlite3.Connection) -> int:
    res = asyncio.run(
        ingest_local(
            conn,
            tmp_path / "data",
            fixture_dir / "video.mp4",
            fixture_dir / "chat.jsonl",
            "Test",
            video_start=BASE_TS,
        )
    )
    return res.session_id


def test_raid_hides_spike(session: int, tmp_path: Path, conn: sqlite3.Connection) -> None:
    first = DEFAULT_SPIKES[0]
    write_events(conn, session, [LiveEvent(first.at - 5, "raid", 500)])
    res = asyncio.run(analyze_session(conn, _cfg(tmp_path), session, with_audio=False))
    peaks = [r[0] for r in conn.execute("SELECT peak_ts FROM candidates ORDER BY peak_ts")]
    assert len(res.candidate_ids) == 2
    assert all(p > first.at + first.duration for p in peaks)


def test_viewer_clips_merge_and_open(
    session: int, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    sp = DEFAULT_SPIKES[1]
    write_events(
        conn,
        session,
        [
            LiveEvent(sp.at - 10, "viewer_clip", data={"slug": "near", "duration": 20}),
            LiveEvent(5, "viewer_clip", data={"slug": "alone", "duration": 10}),
        ],
    )
    asyncio.run(analyze_session(conn, _cfg(tmp_path), session, with_audio=False))
    rows = conn.execute(
        "SELECT peak_ts, sources, features, det_score FROM candidates ORDER BY peak_ts"
    ).fetchall()
    sources = [json.loads(r["sources"]) for r in rows]
    assert sources[0] == ["viewer_clip"] and rows[0]["peak_ts"] == 10.0
    assert ["reactive", "viewer_clip"] in sources
    merged = next(r for r in rows if json.loads(r["sources"]) == ["reactive", "viewer_clip"])
    feats = json.loads(merged["features"])
    assert feats["viewer_clips"][0]["slug"] == "near" and feats["viewer_clips_near"] == 1
    assert sp.at <= merged["peak_ts"] < sp.at + sp.duration + 4
    assert sum(1 for s in sources if s == ["reactive"]) == 2


# -- Twitch viewer clips ------------------------------------------------------------------


def test_clips_period() -> None:
    now = datetime(2026, 11, 18, tzinfo=UTC)
    assert clips_period(now - timedelta(hours=3), now) == "LAST_DAY"
    assert clips_period(now - timedelta(days=3), now) == "LAST_WEEK"
    assert clips_period(now - timedelta(days=20), now) == "LAST_MONTH"
    assert clips_period(now - timedelta(days=90), now) == "ALL_TIME"
    assert clips_period(None, now) == "ALL_TIME"


def _clip(i: int, vod: str | None, off: int | None) -> dict[str, Any]:
    return {
        "cursor": f"c{i}",
        "node": {
            "id": str(i),
            "slug": f"s{i}",
            "title": f"t{i}",
            "createdAt": "2026-11-12T03:09:19Z",
            "durationSeconds": 14,
            "viewCount": 100 - i,
            "videoOffsetSeconds": off,
            "video": {"id": vod} if vod else None,
        },
    }


def test_vod_clips_filters_and_pages() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        op = json.loads(request.content)[0]
        assert "query" in op and "extensions" not in op
        seen.append(op["variables"])
        if op["variables"]["after"] is None:
            edges = [_clip(1, "42", 500), _clip(2, "99", 10), _clip(3, None, None)]
            page = {"pageInfo": {"hasNextPage": True}, "edges": edges}
        else:
            page = {"pageInfo": {"hasNextPage": False}, "edges": [_clip(4, "42", 120)]}
        return httpx.Response(200, json=[{"data": {"user": {"clips": page}}}])

    async def go() -> list[LiveEvent]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gql = TwitchGQL(client, TwitchConfig())
            return await vod_clips(gql, "xqc", "42")

    ev = asyncio.run(go())
    assert [(e.ts, e.data["slug"]) for e in ev] == [(120.0, "s4"), (500.0, "s1")]
    assert ev[0].kind == "viewer_clip" and ev[0].data["duration"] == 14
    assert [v["after"] for v in seen] == [None, "c3"] and seen[0]["period"] == "ALL_TIME"
