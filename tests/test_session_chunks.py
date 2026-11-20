"""Long VOD sessions are transcribed in pieces (an 11 h stream ran faster-whisper out of
memory: 9.4 GB feature array). Times must come back relative to the full WAV."""

from __future__ import annotations

import asyncio
import shutil
import wave
from pathlib import Path
from typing import Any

import pytest

from clipper.config import Config, TranscribeConfig
from clipper.content.batch import _shift_segments, _transcribe_in_chunks


class _Stub:
    def __init__(self) -> None:
        self.calls: list[tuple[float, float | None]] = []

    async def transcribe(
        self, wav: Path, out_json: Path, *, origin: float | None = None
    ) -> list[dict[str, Any]]:
        with wave.open(str(wav), "rb") as w:
            dur = w.getnframes() / w.getframerate()
        self.calls.append((round(dur, 1), origin))
        return [
            {
                "start": 1.0,
                "end": 2.0,
                "text": "x",
                "words": [{"w": "x", "s": 1.0, "e": 1.5, "p": 1}],
            }
        ]


def _silence(path: Path, seconds: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 16000 * seconds)


def test_shift_segments_moves_words_too() -> None:
    seg = {"start": 1.0, "end": 2.0, "text": "a", "words": [{"w": "a", "s": 1.2, "e": 1.8, "p": 1}]}
    (out,) = _shift_segments([seg], 60.0)
    assert (out["start"], out["end"]) == (61.0, 62.0)
    assert (out["words"][0]["s"], out["words"][0]["e"]) == (61.2, 61.8)
    assert seg["start"] == 1.0  # input untouched


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")
def test_long_wav_is_transcribed_in_chunks(tmp_path: Path) -> None:
    wav = tmp_path / "session.wav"
    _silence(wav, 150)
    cfg = Config(transcribe=TranscribeConfig(session_chunk_minutes=1))
    stub = _Stub()
    segs = asyncio.run(_transcribe_in_chunks(cfg, stub, wav, tmp_path / "raw.json", 1000.0))  # type: ignore[arg-type]
    assert [d for d, _ in stub.calls] == [60.0, 60.0, 30.0]
    assert [o for _, o in stub.calls] == [1000.0, 1060.0, 1120.0]
    assert [s["start"] for s in segs] == [1.0, 61.0, 121.0]
    assert not (tmp_path / "session_part.wav").exists()
    assert (tmp_path / "raw.json").is_file()


def test_short_wav_is_one_call(tmp_path: Path) -> None:
    wav = tmp_path / "session.wav"
    _silence(wav, 5)
    stub = _Stub()
    segs = asyncio.run(_transcribe_in_chunks(Config(), stub, wav, tmp_path / "raw.json", 0.0))  # type: ignore[arg-type]
    assert len(stub.calls) == 1 and segs[0]["start"] == 1.0
