from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from clipper import chatlog, db, ingest
from clipper.__main__ import main
from clipper.models import ChatMessage
from tests.fixtures.make_fixtures import BASE_TS, DEFAULT_SPIKES


def test_resolve_offset() -> None:
    rel = [ChatMessage(5.0, "u", "a")]
    absolute = [ChatMessage(BASE_TS + 5, "u", "a")]
    assert ingest.resolve_chat_offset(rel, None, None) == 0.0
    assert ingest.resolve_chat_offset(absolute, BASE_TS, 1.0) == BASE_TS
    assert ingest.resolve_chat_offset(absolute, None, BASE_TS - 1) == BASE_TS - 1
    assert ingest.resolve_chat_offset(absolute, None, None) == BASE_TS + 5


def test_ingest_local_fixture(fixture_dir: Path, tmp_path: Path, conn: sqlite3.Connection) -> None:
    data_dir = tmp_path / "data"
    res = asyncio.run(
        ingest.ingest_local(
            conn,
            data_dir,
            fixture_dir / "video.mp4",
            fixture_dir / "chat.jsonl",
            "Test Yayıncı",
            video_start=BASE_TS,
        )
    )
    assert abs(res.duration - 180) < 0.5
    assert res.chat_offset == BASE_TS
    session = conn.execute("SELECT * FROM sessions WHERE id = ?", (res.session_id,)).fetchone()
    assert session["source"] == "vod" and session["status"] == "ended"
    segs = conn.execute("SELECT * FROM segments WHERE session_id = ?", (res.session_id,)).fetchall()
    assert len(segs) == 1 and segs[0]["pdt_start"] == 0.0 and segs[0]["seq"] == 0
    assert Path(segs[0]["path"]).is_file()
    assert (fixture_dir / "video.mp4").is_file()  # source kept (hardlink/copy)

    msgs = list(chatlog.read_jsonl(res.session_dir / "chat.jsonl.gz"))
    assert len(msgs) == res.chat_count > 300
    assert min(m.ts for m in msgs) >= 0 and max(m.ts for m in msgs) <= 180
    spike = DEFAULT_SPIKES[0]
    in_spike = [m for m in msgs if spike.at <= m.ts < spike.at + spike.duration]
    assert len(in_spike) > 50
    info = json.loads((res.session_dir / "session.json").read_text(encoding="utf-8"))
    assert info["time_base"] == "pdt0"


def test_same_streamer_reused(fixture_dir: Path, tmp_path: Path, conn: sqlite3.Connection) -> None:
    for _ in range(2):
        asyncio.run(ingest.ingest_local(conn, tmp_path, fixture_dir / "video.mp4", None, "A"))
    assert conn.execute("SELECT COUNT(*) FROM streamers").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_missing_files(tmp_path: Path, conn: sqlite3.Connection) -> None:
    with pytest.raises(ingest.IngestError):
        asyncio.run(ingest.ingest_local(conn, tmp_path, tmp_path / "nope.mp4", None, "A"))


def test_cli_vod_local(
    fixture_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"paths: {{ data_dir: {tmp_path.as_posix()}/data }}\n", encoding="utf-8")
    code = main(
        [
            "--config",
            str(cfg),
            "--env",
            str(tmp_path / ".env"),
            "vod",
            "--video",
            str(fixture_dir / "video.mp4"),
            "--chat",
            str(fixture_dir / "chat.jsonl"),
            "--streamer",
            "deneme",
            "--video-start",
            str(BASE_TS),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Session #1 hazır: 3:00 video" in out
    conn = db.connect(tmp_path / "data" / "clipper.db")
    assert conn.execute("SELECT channel FROM streamers").fetchone()[0] == "deneme"
    conn.close()


def test_cli_vod_requires_streamer(
    fixture_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"paths: {{ data_dir: {tmp_path.as_posix()}/data }}\n", encoding="utf-8")
    assert main(["--config", str(cfg), "vod", "--video", str(fixture_dir / "video.mp4")]) == 2
    assert "--streamer" in capsys.readouterr().err


def test_cli_chat_import(tmp_path: Path) -> None:
    from tests.test_chatlog import TD_EXPORT

    src = tmp_path / "td.json"
    src.write_text(json.dumps(TD_EXPORT), encoding="utf-8")
    out = tmp_path / "out.jsonl.gz"
    assert main(["chat-import", str(src), str(out)]) == 0
    assert len(list(chatlog.read_jsonl(out))) == 2
