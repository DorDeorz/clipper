"""Labeling mode (TESTPLAN §4, PNL-05)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper.evals import labels as L
from clipper.panel.label_app import create_label_app


def _session(conn: sqlite3.Connection, segs: list[tuple[str, float, float]]) -> int:
    conn.execute(
        "INSERT INTO streamers(display_name, platform, channel, created_at)"
        " VALUES('Ayşe', 'twitch', 'ayse', '2026-11-18T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO sessions(streamer_id, source, title, started_at, dir, status)"
        " VALUES(1, 'vod', 'Gece yayını', '2026-11-18T00:00:00Z', 'x', 'done')"
    )
    for i, (path, t0, dur) in enumerate(segs):
        conn.execute(
            "INSERT INTO segments(session_id, seq, path, pdt_start, duration) VALUES(1,?,?,?,?)",
            (i, path, t0, dur),
        )
    # A candidate the labeler must never see.
    conn.execute(
        "INSERT INTO candidates(session_id, peak_ts, win_start, win_end, det_score, features,"
        " status, created_at) VALUES(1, 1234.5, 1200, 1260, 9.87, '{}', 'new', 'x')"
    )
    conn.commit()
    return 1


def test_video_to_session_maps_across_gaps() -> None:
    ps = [L.Piece(0, 1000, 10), L.Piece(10, 1030, 10)]  # 20 s gap between segments
    assert L.video_to_session(5, ps) == 1005
    assert L.video_to_session(12, ps) == 1032
    assert L.video_to_session(99, ps) == 1040


def test_validate_rejects_bad_labels() -> None:
    ps = [L.Piece(0, 0, 100)]
    ok = {"video_start": 1, "video_end": 5, "type": "komik", "quality": 3}
    assert L.validate([ok], ps, 100)[0]["start"] == 1
    for bad in (
        {**ok, "video_end": 0.5},
        {**ok, "type": "eğlenceli"},
        {**ok, "quality": 4},
        {**ok, "video_end": 500},
        {"type": "komik", "quality": 1},
    ):
        with pytest.raises(L.LabelError):
            L.validate([bad], ps, 100)


def test_label_path_and_agreement(tmp_path: Path) -> None:
    assert L.label_path(tmp_path, 7).name == "7.json"
    assert L.label_path(tmp_path, 7, "Zeynep Ö.").name == "7_Zeynep_Ö.json"
    a = [{"start": 0, "end": 10}, {"start": 100, "end": 110}]
    b = [{"start": 4, "end": 12}]  # overlaps a[0] by 6/8 of the shorter span
    assert L.agreement(a, b) == pytest.approx(2 / 3, abs=1e-3)
    assert L.agreement([], []) == 1.0


def test_pnl05_label_api_hides_candidates(conn: sqlite3.Connection, tmp_path: Path) -> None:
    sid = _session(conn, [("a.ts", 1000, 60), ("b.ts", 1100, 60)])
    proxy = tmp_path / "p.mp4"
    proxy.write_bytes(b"\0" * 16)
    out = tmp_path / "labels" / "1.json"
    client = TestClient(create_label_app(conn, sid, proxy, out, "zeynep"))

    got = client.get("/api/label")
    assert got.status_code == 200
    text = got.text
    for leak in ("1234.5", "9.87", "peak", "candidate", "det_score", "score"):
        assert leak not in text
    assert got.json()["duration"] == 120
    for path in ("/api/clips", "/api/sessions", "/api/sessions/1/signals", "/api/live",
                 "/api/status", "/static/app.js", "/media/x"):  # fmt: skip
        assert client.get(path).status_code == 404, path

    r = client.put(
        "/api/label",
        json={"labels": [{"video_start": 70, "video_end": 80, "type": "şok", "quality": 2}]},
    )
    assert r.status_code == 200, r.text
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["labeler"] == "zeynep" and doc["session_id"] == 1
    assert doc["labels"][0]["start"] == 1110 and doc["labels"][0]["end"] == 1120
    assert client.get("/api/label").json()["labels"][0]["type"] == "şok"

    bad = client.put("/api/label", json={"labels": [{"video_start": 1}]})
    assert bad.status_code == 400 and "etiket" in bad.json()["error"]
    assert client.get("/").status_code == 200
    assert client.get("/static/label.js").status_code == 200


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")
def test_build_proxy(conn: sqlite3.Connection, ts_segments: list[Path], tmp_path: Path) -> None:
    import asyncio

    _session(conn, [(str(p), 2.0 * i, 2.0) for i, p in enumerate(ts_segments)])
    out = asyncio.run(L.build_proxy(conn, 1, tmp_path / "proxy" / "1.mp4"))
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,height", "-of", "json",
         str(out)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    streams = json.loads(probe.stdout)["streams"]
    assert any(s.get("codec_name") == "h264" and s.get("height") == 540 for s in streams)
    mtime = out.stat().st_mtime
    asyncio.run(L.build_proxy(conn, 1, out))  # cached
    assert out.stat().st_mtime == mtime
