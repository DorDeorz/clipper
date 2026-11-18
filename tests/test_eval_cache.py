"""TESTPLAN Bölüm 1: LLM record/replay, whisper cache, fake transcript backend."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import wave
from pathlib import Path
from typing import Any

import pytest

from clipper.config import LLMConfig, TranscribeConfig
from clipper.llm.client import LLMCacheMiss, LLMClient
from clipper.llm.schemas import PrefilterDecision
from clipper.transcribe.whisper import TranscribeError, Transcriber
from tests.llm_mock import FakeAnthropic


def _ask(llm: LLMClient, user: str = "u") -> Any:
    return asyncio.run(
        llm.structured(purpose="prefilter", model="claude-haiku-5-5", system="s", user=user,
                       schema=PrefilterDecision, effort="low")
    )  # fmt: skip


def test_llm_record_then_replay(conn: sqlite3.Connection, tmp_path: Path) -> None:
    reply = json.dumps({"keep": True, "rough_score": 71, "reason": "iyi"})
    fake = FakeAnthropic([lambda b: reply])
    rec = LLMClient(conn, LLMConfig(cache_mode="record", cache_dir=tmp_path), fake.client())
    assert _ask(rec).value.rough_score == 71
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert _ask(rec).value.rough_score == 71  # second call served from cache
    assert len(fake.requests) == 1

    empty = FakeAnthropic([])
    rep = LLMClient(conn, LLMConfig(cache_mode="replay", cache_dir=tmp_path), empty.client())
    res = _ask(rep)
    assert res.value.rough_score == 71 and res.usd == 0 and empty.requests == []
    with pytest.raises(LLMCacheMiss):
        _ask(rep, user="başka istek")
    assert empty.requests == []  # a miss never reaches the API


def _wav(path: Path, seconds: float, tone: int = 0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes([tone % 256, 0]) * int(16000 * seconds))
    return path


class _Seg:
    def __init__(self, text: str) -> None:
        self.start, self.end, self.text, self.words = 0.0, 1.0, text, []


class _Model:
    calls = 0

    def transcribe(self, audio: str, **kw: Any) -> tuple[Any, Any]:
        _Model.calls += 1
        return [_Seg("merhaba")], None


def test_whisper_cache_by_audio_hash(tmp_path: Path) -> None:
    cfg = TranscribeConfig(cache_dir=tmp_path / "wc")
    tr = Transcriber(cfg, tmp_path, loader=lambda c, d: _Model())
    a = _wav(tmp_path / "a.wav", 1)
    asyncio.run(tr.transcribe(a, tmp_path / "a.json"))
    asyncio.run(tr.transcribe(_wav(tmp_path / "copy.wav", 1), tmp_path / "b.json"))
    assert _Model.calls == 1  # same bytes -> cached
    asyncio.run(tr.transcribe(_wav(tmp_path / "c.wav", 1, tone=7), tmp_path / "c.json"))
    assert _Model.calls == 2
    assert json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))[0]["text"] == "merhaba"
    tr.close()


def test_fake_transcript_backend(tmp_path: Path) -> None:
    fake = tmp_path / "fake_transcript.jsonl"
    fake.write_text(
        json.dumps({"start": 1000.0, "end": 1004.0, "text": "İstanbul'da ılık şişli öğüş"},
                   ensure_ascii=False) + "\n"
        + json.dumps({"start": 2000.0, "end": 2001.0, "text": "uzak"}) + "\n",
        encoding="utf-8",
    )  # fmt: skip
    cfg = TranscribeConfig(backend="fake", fake_path=fake)
    tr = Transcriber(cfg, tmp_path, loader=lambda c, d: pytest.fail("whisper loaded"))
    wav = _wav(tmp_path / "x.wav", 10)
    segs = asyncio.run(tr.transcribe(wav, tmp_path / "x.json", origin=998.0))
    assert len(segs) == 1 and segs[0]["text"] == "İstanbul'da ılık şişli öğüş"
    words = segs[0]["words"]
    assert words[0]["s"] == 2.0 and words[-1]["e"] == 6.0 and len(words) == 4
    with pytest.raises(TranscribeError):
        asyncio.run(tr.transcribe(wav, tmp_path / "y.json"))  # no origin
    tr.close()
