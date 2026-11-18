from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from clipper import db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    db.open_db(path).close()
    return path


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(db_path)
    yield c
    c.close()


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg yok")
    from tests.fixtures.make_fixtures import make_chat, make_video

    out = tmp_path_factory.mktemp("fixtures")
    make_video(out / "video.mp4", 180)
    make_chat(out / "chat.jsonl", 180)
    return out


@pytest.fixture(scope="session")
def ts_segments(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    """Three ~2 s MPEG-TS segments (small, generated with ffmpeg)."""
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg yok")
    out = tmp_path_factory.mktemp("ts")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-shortest",
            "-f",
            "segment",
            "-segment_time",
            "2",
            "-segment_format",
            "mpegts",
            str(out / "seg%d.ts"),
        ],
        check=True,
    )
    return sorted(out.glob("seg*.ts"))
