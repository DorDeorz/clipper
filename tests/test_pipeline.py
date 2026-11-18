from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from clipper import jobs
from clipper.__main__ import main
from clipper.config import Config, DetectorConfig, PathsConfig
from clipper.detect.pipeline import DetectError, analyze_session, top_messages
from clipper.detect.report import render_report, write_report
from clipper.ingest import ingest_local
from clipper.models import ChatMessage
from tests.fixtures.make_fixtures import BASE_TS, DEFAULT_SPIKES


def _cfg(tmp_path: Path) -> Config:
    return Config(
        paths=PathsConfig(data_dir=tmp_path / "data"),
        detector=DetectorConfig(warmup_seconds=30, lag_seconds=5, pre_roll=10, post_roll=10),
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


def test_analyze_fixture_session(session: int, tmp_path: Path, conn: sqlite3.Connection) -> None:
    res = asyncio.run(analyze_session(conn, _cfg(tmp_path), session))
    assert len(res.candidate_ids) == 3
    assert res.seconds == 180
    rows = conn.execute(
        "SELECT COUNT(*), COUNT(audio_db) FROM signal_seconds WHERE session_id = ?", (session,)
    ).fetchone()
    assert rows[0] == 180 and rows[1] >= 175  # audio present for (almost) every second
    cands = conn.execute("SELECT * FROM candidates ORDER BY peak_ts").fetchall()
    for c, sp in zip(cands, DEFAULT_SPIKES, strict=True):
        assert sp.at <= c["peak_ts"] < sp.at + sp.duration + 4
        assert c["status"] == "new"
        feats = json.loads(c["features"])
        assert feats["top_messages"] and feats["top_messages"][0]["text"] in sp.texts
        assert set(feats["z"]) == {"rate", "uniq", "emote", "audio"}
    cut_jobs = conn.execute("SELECT payload FROM jobs WHERE type = 'cut'").fetchall()
    assert sorted(json.loads(j[0])["candidate_id"] for j in cut_jobs) == res.candidate_ids


def test_rerun_needs_force(session: int, tmp_path: Path, conn: sqlite3.Connection) -> None:
    cfg = _cfg(tmp_path)
    asyncio.run(analyze_session(conn, cfg, session, with_audio=False))
    with pytest.raises(DetectError, match="--force"):
        asyncio.run(analyze_session(conn, cfg, session, with_audio=False))
    res = asyncio.run(analyze_session(conn, cfg, session, force=True, with_audio=False))
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 3
    assert jobs.counts(conn) == {"queued": 3}
    assert len(res.candidate_ids) == 3


def test_streamer_overrides(session: int, tmp_path: Path, conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE streamers SET detector_overrides = ?", (json.dumps({"warmup_seconds": 60}),)
    )
    res = asyncio.run(analyze_session(conn, _cfg(tmp_path), session, with_audio=False))
    assert len(res.candidate_ids) == 2  # first spike (45 s) is inside warmup now


def test_report(session: int, tmp_path: Path, conn: sqlite3.Connection) -> None:
    asyncio.run(analyze_session(conn, _cfg(tmp_path), session, with_audio=False))
    page = render_report(conn, session, 3.0)
    assert page.count('class="card"') == 3
    assert "<svg" in page and "<polyline" in page and 'lang="tr"' in page
    path = write_report(conn, session, 3.0)
    assert path.name == "report.html" and path.read_text(encoding="utf-8") == page


def test_top_messages_counts_case_insensitive() -> None:
    msgs = [
        ChatMessage(1, "a", "KLİPLE"),
        ChatMessage(2, "b", "kliple"),
        ChatMessage(3, "c", "oha"),
        ChatMessage(99, "d", "dışarıda"),
    ]
    top = top_messages(msgs, 0, 10)
    assert top[0] == {"text": "KLİPLE", "count": 2} and len(top) == 2


def test_cli_detect_and_report(
    fixture_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"paths: {{ data_dir: {tmp_path.as_posix()}/data }}\n"
        "detector: { warmup_seconds: 30, lag_seconds: 5, pre_roll: 10, post_roll: 10 }\n",
        encoding="utf-8",
    )
    base = ["--config", str(cfg), "--env", str(tmp_path / ".env")]
    assert (
        main(
            [
                *base,
                "vod",
                "--video",
                str(fixture_dir / "video.mp4"),
                "--chat",
                str(fixture_dir / "chat.jsonl"),
                "--streamer",
                "x",
                "--video-start",
                str(BASE_TS),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Dedektör: 3 aday" in out and "report.html" in out
    assert main([*base, "detect", "1"]) == 1
    assert "--force" in capsys.readouterr().err
    assert main([*base, "detect", "1", "--force", "--no-audio"]) == 0
    assert main([*base, "report", "1", "--out", str(tmp_path / "r.html")]) == 0
    assert (tmp_path / "r.html").is_file()
    assert main([*base, "report", "99"]) == 1
    assert "bulunamadı" in capsys.readouterr().err
