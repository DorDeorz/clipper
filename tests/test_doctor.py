from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from clipper.config import Config, PathsConfig, Secrets
from clipper.doctor import Check, Status, format_report, run_checks
from clipper.ffmpeg_probe import parse_encoders, parse_filters, parse_version
from clipper.util.crypto import generate_key

ENCODERS = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
"""
FILTERS = """Filters:
  T.. = Timeline support
 ... ass               V->V       Render ASS subtitles onto input video using the libass library.
 ... subtitles         V->V       Render text subtitles onto input video using the libass library.
 T.C loudnorm          A->A       EBU R128 loudness normalization
"""


def test_parsers() -> None:
    assert parse_encoders(ENCODERS) == {"libx264", "h264_nvenc", "aac"}
    assert parse_filters(FILTERS) == {"ass", "subtitles", "loudnorm"}
    assert parse_version("ffmpeg version 7.1-full_build-www.gyan.dev Copyright") == (
        "7.1-full_build-www.gyan.dev"
    )
    assert parse_version("") is None


def _by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_missing_ffmpeg_is_red(tmp_path: Path) -> None:
    cfg = Config(
        paths=PathsConfig(
            data_dir=tmp_path, ffmpeg="no-such-ffmpeg-xyz", ffprobe="no-such-ffprobe-xyz"
        )
    )
    checks = _by_name(asyncio.run(run_checks(cfg, Secrets())))
    assert checks["ffmpeg"].status is Status.FAIL
    assert checks["ffprobe"].status is Status.FAIL
    assert checks["ANTHROPIC_API_KEY"].status is Status.WARN
    assert checks["Veritabanı"].status is Status.OK


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")
def test_real_ffmpeg_and_secrets(tmp_path: Path) -> None:
    secrets = Secrets(anthropic_api_key="sk-ant-test", clipper_secret_key=generate_key())
    checks = _by_name(
        asyncio.run(run_checks(Config(paths=PathsConfig(data_dir=tmp_path)), secrets))
    )
    assert checks["ffmpeg"].status is Status.OK
    assert checks["ANTHROPIC_API_KEY"].status is Status.OK
    assert checks["CLIPPER_SECRET_KEY"].status is Status.OK
    assert "NVENC" in checks


def test_bad_secret_key_is_red(tmp_path: Path) -> None:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path, ffmpeg="no-such-ffmpeg-xyz"))
    checks = _by_name(asyncio.run(run_checks(cfg, Secrets(clipper_secret_key="bad"))))
    assert checks["CLIPPER_SECRET_KEY"].status is Status.FAIL


def test_report_format() -> None:
    text = format_report(
        [Check("A", Status.OK, "iyi"), Check("Uzun ad", Status.FAIL, "kötü")], color=False
    )
    assert " ✔ A        iyi" in text
    assert "1 kritik sorun" in text
