"""Faz 5: panel API (FastAPI TestClient), feedback records, media safety, auth."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from clipper import db
from clipper.config import Config, PanelConfig, PathsConfig, Secrets
from clipper.panel.app import PanelConfigError, PanelDeps, check_bind, create_app, is_loopback
from tests.panel_client import panel_client

JUDGE = {
    "keep": True, "score": 80, "reason": "çok komik", "start": 2.0, "end": 30.0,
    "title": "Oha anı", "hook": "bak şuna", "caption_fixes": [], "hashtags": ["#oha"],
    "texts": {"youtube": {"title": "t", "description": "d"}, "instagram": {"caption": "c"},
              "tiktok": {"caption": "c"}, "x": {"text": "x"}},
    "flags": {"music_risk": False, "nsfw": False, "sensitive_topic": False,
              "needs_context": False},
}  # fmt: skip


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Config]]:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))
    conn = db.open_db(cfg.db_path, check_same_thread=False)
    conn.execute(
        "INSERT INTO streamers (display_name, platform, channel, layout, permission_ok,"
        " created_at) VALUES ('Ali', 'twitch', 'ali', 'blur', 1, 'x')"
    )
    conn.execute(
        "INSERT INTO sessions (streamer_id, source, title, started_at, dir, status)"
        " VALUES (1, 'vod', 'yayın', '2026-11-18T10:00:00', '.', 'ended')"
    )
    clip_dir = cfg.paths.data_dir / "sessions" / "1" / "clips" / "1"
    clip_dir.mkdir(parents=True)
    for name in ("raw.mp4", "final.mp4", "thumb.jpg"):
        (clip_dir / name).write_bytes(b"\x00" * 64)

    def add(status: str, judge: dict[str, Any] | None, sources: list[str]) -> int:
        cur = conn.execute(
            "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score,"
            " features, status, created_at, sources) VALUES (1, 100, 40, 125, 4.2, ?, ?, 'x', ?)",
            (
                json.dumps({"z": {"rate": 3.1}, "content": {"kind": "story", "score": 77,
                            "reason": "hikaye"}, "top_messages": ["KEKW"]}),
                status,
                json.dumps(sources),
            ),
        )  # fmt: skip
        cid = cur.lastrowid
        llm: dict[str, Any] = {
            "cut": {"t0": 40, "duration": 85},
            "prefilter": {"keep": True, "rough_score": 70, "reason": "iyi"},
        }
        if judge:
            llm["judge"] = judge
        conn.execute(
            "INSERT INTO clips (candidate_id, raw_path, llm_json, llm_score, start_rel, end_rel,"
            " title, final_path, thumb_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'x')",
            (
                cid,
                str(clip_dir / "raw.mp4"),
                json.dumps(llm),
                judge["score"] if judge else None,
                2.0 if judge else None,
                30.0 if judge else None,
                judge["title"] if judge else None,
                str(clip_dir / "final.mp4") if status == "rendered" else None,
                str(clip_dir / "thumb.jpg") if status == "rendered" else None,
            ),
        )
        return int(cid or 0)

    add("rendered", JUDGE, ["reactive", "content"])  # clip 1
    add("rejected", None, ["viewer_clip"])  # clip 2 (prefilter dropped it)
    conn.execute(
        "INSERT INTO signal_seconds (session_id, t, msgs, score, audio_db)"
        " VALUES (1, 99, 10, 1.5, -20), (1, 100, 30, 4.2, -10)"
    )
    yield conn, cfg
    conn.close()


@pytest.fixture
def client(world: tuple[sqlite3.Connection, Config]) -> TestClient:
    conn, cfg = world
    return panel_client(create_app(PanelDeps(conn, cfg, Secrets())))


def test_index_and_static(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "clipper paneli" in r.text
    assert client.get("/static/app.js").status_code == 200
    s = client.get("/api/status").json()
    assert s["review_count"] == 1 and "not_funny" in s["reject_reasons"]
    assert s["facecam_suggest"] is False


def test_media_is_confined_to_data_dir(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("gizli")
    assert client.get("/media/sessions/1/clips/1/final.mp4").status_code == 200
    for bad in ("../secret.txt", "..%2Fsecret.txt", "sessions/../../secret.txt",
                "%2E%2E/secret.txt", "sessions", "nope.mp4"):  # fmt: skip
        r = client.get(f"/media/{bad}")
        assert r.status_code == 404, bad
        assert "gizli" not in r.text


def test_clip_views_and_card(client: TestClient) -> None:
    (card,) = client.get("/api/clips?view=review").json()
    assert card["sources"] == ["reactive", "content"]
    assert card["content"]["kind"] == "story" and card["content"]["reason"] == "hikaye"
    assert card["video"] == "sessions/1/clips/1/final.mp4"
    assert card["hook"] == "bak şuna" and card["llm_score"] == 80
    (rej,) = client.get("/api/clips?view=rejected_llm").json()
    assert rej["reject_stage"] == "prefilter" and rej["sources"] == ["viewer_clip"]
    assert rej["raw_video"] == "sessions/1/clips/1/raw.mp4"
    assert client.get("/api/clips?view=nope").status_code == 400


def test_edit_title_only_needs_no_render(client: TestClient, world: Any) -> None:
    conn, _ = world
    r = client.put("/api/clips/1", json={"title": "Yeni başlık", "hook": "bak şuna"}).json()
    assert r == {"needs_render": False}
    r = client.post("/api/clips/1/approve", json={"platforms": ["youtube", "x"]}).json()
    assert r["decision"] == "edited"
    fb = conn.execute("SELECT * FROM feedback").fetchone()
    assert fb["decision"] == "edited" and fb["title_edited"] == 1 and fb["start_delta"] is None
    snap = json.loads(fb["features_snapshot"])
    assert snap["candidate_id"] == 1
    row = conn.execute("SELECT review, llm_json FROM clips WHERE id = 1").fetchone()
    assert row["review"] == "approved"
    assert json.loads(row["llm_json"])["publish_targets"] == ["youtube", "x"]
    assert client.get("/api/clips?view=approved").json()[0]["clip_id"] == 1


def test_trim_edit_rerenders_and_keeps_deltas(client: TestClient, world: Any) -> None:
    conn, _ = world
    r = client.put("/api/clips/1", json={"start": 3.5, "end": 28.0}).json()
    assert r == {"needs_render": True, "rendering": True}
    job = conn.execute("SELECT type, priority, payload FROM jobs").fetchone()
    assert job["type"] == "render" and job["priority"] == 5
    assert conn.execute("SELECT status FROM candidates WHERE id = 1").fetchone()[0] == "judged"
    # approve is refused until the new render lands
    assert client.post("/api/clips/1/approve", json={"platforms": []}).status_code == 400
    conn.execute("UPDATE candidates SET status = 'rendered' WHERE id = 1")
    client.post("/api/clips/1/approve", json={"platforms": ["youtube"]})
    fb = conn.execute("SELECT * FROM feedback").fetchone()
    assert (fb["start_delta"], fb["end_delta"]) == (1.5, -2.0)
    assert fb["decision"] == "edited" and fb["title_edited"] == 0


def test_edit_validation(client: TestClient) -> None:
    assert client.put("/api/clips/1", json={"start": 10, "end": 11}).status_code == 400
    assert client.put("/api/clips/1", json={"title": ""}).status_code == 400
    r = client.put("/api/clips/99", json={"title": "x"})
    assert r.status_code == 404 and r.json()["error"] == "klip bulunamadı"
    not_object = client.put(
        "/api/clips/1", content=b"[1]", headers={"Content-Type": "application/json"}
    )
    assert not_object.status_code == 400


def test_reject_with_reason(client: TestClient, world: Any) -> None:
    conn, _ = world
    assert client.post("/api/clips/1/reject", json={"reason": "kötü"}).status_code == 400
    assert client.post("/api/clips/1/reject", json={"reason": "bad_cut", "note": "erken"}).json()
    fb = conn.execute("SELECT decision, reason FROM feedback").fetchone()
    assert tuple(fb) == ("rejected", "bad_cut")
    row = conn.execute("SELECT review, reviewer_note FROM clips WHERE id = 1").fetchone()
    assert tuple(row) == ("rejected", "erken")
    assert client.get("/api/clips?view=rejected_user").json()[0]["clip_id"] == 1


def test_rescue_llm_rejected(client: TestClient, world: Any) -> None:
    conn, _ = world
    assert client.post("/api/clips/1/rescue").status_code == 400  # not LLM-rejected
    assert client.post("/api/clips/2/rescue").json() == {"ok": True}
    fb = conn.execute("SELECT decision, reason FROM feedback WHERE clip_id = 2").fetchone()
    assert tuple(fb) == ("approved", "rescued")
    clip = conn.execute("SELECT start_rel, end_rel, title FROM clips WHERE id = 2").fetchone()
    assert clip["start_rel"] == 0.0 and clip["end_rel"] == 59.0 and clip["title"]
    assert conn.execute("SELECT status FROM candidates WHERE id = 2").fetchone()[0] == "judged"
    assert conn.execute("SELECT type FROM jobs").fetchone()[0] == "render"


def test_streamers_crud(client: TestClient) -> None:
    r = client.post("/api/streamers", json={"display_name": "Veli", "platform": "kick",
                                            "channel": "veli", "layout": "stack",
                                            "facecam_rect": {"x": 0.7, "y": 0.6, "w": 0.3,
                                                             "h": 0.4},
                                            "keywords": ["oha"]})  # fmt: skip
    assert r.status_code == 201
    sid = r.json()["id"]
    got = next(s for s in client.get("/api/streamers").json() if s["id"] == sid)
    assert got["facecam_rect"] == {"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4}
    assert got["keywords"] == ["oha"] and got["permission_ok"] == 0
    bad = client.put(f"/api/streamers/{sid}", json={"facecam_rect": {"x": 0.9, "y": 0, "w": 0.5,
                                                                     "h": 0.1}})  # fmt: skip
    assert bad.status_code == 400 and "facecam" in bad.json()["error"]
    assert client.put(f"/api/streamers/{sid}", json={"layout": "kare"}).status_code == 400
    assert client.put(f"/api/streamers/{sid}", json={"permission_ok": True}).status_code == 200
    got = next(s for s in client.get("/api/streamers").json() if s["id"] == sid)
    assert got["permission_ok"] == 1
    assert client.post("/api/streamers", json={"display_name": "x"}).status_code == 400
    assert client.put("/api/streamers/999", json={"layout": "blur"}).status_code == 404
    r = client.post(f"/api/streamers/{sid}/frame")
    assert r.status_code == 404 and "video" in r.json()["error"]
    assert client.post(f"/api/streamers/{sid}/facecam/suggest").status_code == 503


def test_signals_sessions_costs_logs(client: TestClient) -> None:
    (sess,) = client.get("/api/sessions").json()
    assert sess["streamer"] == "Ali" and sess["candidates"] == 2
    sig = client.get("/api/sessions/1/signals?last=0").json()
    assert sig["t"] == [99, 100] and sig["score"] == [1.5, 4.2]
    assert {tuple(c["sources"]) for c in sig["candidates"]} == {("reactive", "content"),
                                                                 ("viewer_clip",)}  # fmt: skip
    costs = client.get("/api/costs").json()
    assert costs["month_usd"] == 0 and costs["days"] == []
    assert isinstance(client.get("/api/logs").json(), list)
    assert client.get("/api/publications").json() == []
    assert client.get("/api/accounts").json()["accounts"] == []


def test_basic_auth_when_password(world: Any) -> None:
    conn, cfg = world
    app = create_app(PanelDeps(conn, cfg, Secrets(panel_password=SecretStr("gizli"))))
    c = panel_client(app)
    assert c.get("/api/status").status_code == 401
    assert c.get("/media/sessions/1/clips/1/final.mp4").status_code == 401
    good = base64.b64encode(b"panel:gizli").decode()
    bad = base64.b64encode(b"panel:yanlis").decode()
    assert c.get("/api/status", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_bind_rules() -> None:
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0")
    check_bind(Config(), Secrets())
    open_cfg = Config(panel=PanelConfig(host="0.0.0.0"))
    with pytest.raises(PanelConfigError):
        check_bind(open_cfg, Secrets())
    check_bind(open_cfg, Secrets(panel_password=SecretStr("x")))


def test_run_forever_stops(world: Any) -> None:
    import asyncio

    from clipper import jobs
    from clipper.worker import run_forever

    conn, _ = world
    seen: list[str] = []

    async def go() -> None:
        stop = asyncio.Event()

        async def handler(job: jobs.Job) -> None:
            seen.append(job.type)
            if len(seen) == 2:
                stop.set()

        jobs.enqueue(conn, "a", {}, "cpu")
        task = asyncio.create_task(run_forever(conn, handler, stop, poll=0.05))
        await asyncio.sleep(0.2)
        jobs.enqueue(conn, "b", {}, "io")  # arrives while idle
        stats = await asyncio.wait_for(task, 5)
        assert stats.done == 2

    asyncio.run(go())
    assert seen == ["a", "b"]


def test_live_list_and_mark(client: TestClient, world: Any) -> None:
    conn, cfg = world
    assert client.get("/api/live").json() == []
    assert client.post("/api/live/1/mark").status_code == 404
    sdir = cfg.paths.data_dir / "sessions" / "9"
    sdir.mkdir(parents=True)
    (sdir / "session.json").write_text('{"t0": 1000.0}', encoding="utf-8")
    conn.execute(
        "INSERT INTO sessions (id, streamer_id, source, started_at, dir, status)"
        " VALUES (9, 1, 'live', '2026-11-18T20:00:00', ?, 'recording')",
        (str(sdir),),
    )
    (live,) = client.get("/api/live").json()
    assert live["session_id"] == 9 and live["display_name"] == "Ali"
    ts = client.post("/api/live/1/mark").json()["ts"]
    assert ts > 0
    kinds = [r[0] for r in conn.execute("SELECT kind FROM live_events WHERE session_id = 9")]
    assert kinds == ["mark"]
