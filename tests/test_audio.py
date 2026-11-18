from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from clipper.detect.audio import SILENCE_DB, rms_dbfs, segment_db, segments_db

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")


def test_rms_dbfs() -> None:
    full = np.full(16000, 32767, dtype=np.int16)
    assert rms_dbfs(full) == pytest.approx(0.0, abs=0.01)
    assert rms_dbfs(np.zeros(100, dtype=np.int16)) == SILENCE_DB
    assert rms_dbfs(np.array([], dtype=np.int16)) == SILENCE_DB


@pytest.fixture(scope="module")
def quiet_loud(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """3 s silence followed by 3 s of a 0.5-amplitude sine."""
    out = tmp_path_factory.mktemp("aud") / "a.wav"
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
            "anullsrc=r=16000:cl=mono:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:r=16000:d=3",
            "-filter_complex",
            "[1]volume=4[s];[0][s]concat=n=2:v=0:a=1",
            str(out),
        ],
        check=True,
    )
    return out


def test_segment_db_per_second(quiet_loud: Path) -> None:
    db = asyncio.run(segment_db(quiet_loud, 100.0))
    assert sorted(db) == [100, 101, 102, 103, 104, 105]
    assert all(db[t] == SILENCE_DB for t in (100, 101, 102))
    assert all(-12 < db[t] < -3 for t in (103, 104, 105))


def test_fractional_pdt_alignment(quiet_loud: Path) -> None:
    db = asyncio.run(segment_db(quiet_loud, 10.5))
    assert min(db) == 10 and max(db) == 16
    assert db[10] == SILENCE_DB and db[14] > -12


def test_segments_merge(quiet_loud: Path) -> None:
    db = asyncio.run(segments_db([(quiet_loud, 0.0), (quiet_loud, 6.0)]))
    assert sorted(db) == list(range(12))
