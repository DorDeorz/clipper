from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from clipper import jobs
from clipper.config import Config, DetectorConfig, LLMConfig, PathsConfig, TranscribeConfig
from clipper.detect.pipeline import analyze_session
from clipper.ingest import ingest_local
from clipper.llm.client import LLMClient
from clipper.recorder.cutter import cut
from clipper.stages import Stages
from clipper.transcribe.whisper import Transcriber
from clipper.worker import run_until_idle
from tests.fixtures.make_fixtures import BASE_TS
from tests.llm_mock import FakeAnthropic, judge_payload


@dataclass
class _W:
    word: str
    start: float
    end: float
    probability: float = 0.9


@dataclass
class _Seg:
    start: float
    end: float
    text: str
    words: list[_W]


class FakeWhisper:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def transcribe(self, audio: str, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append(audio)
        assert kwargs["word_timestamps"] is True and kwargs["language"] == "tr"
        segs = [
            _Seg(
                1.0,
                4.0,
                " valoran oynuyoruz",
                [_W(" valoran", 1.0, 1.8), _W("oynuyoruz", 1.9, 3.9)],
            ),
            _Seg(
                8.0,
                12.0,
                " oha ne oldu",
                [_W("oha", 8.0, 8.5), _W("ne", 9, 9.4), _W("oldu", 9.5, 12)],
            ),
            _Seg(30.0, 33.0, " inanılmaz", [_W("inanılmaz", 30.0, 33.0)]),
        ]
        return iter(segs), None


def _cfg(tmp_path: Path, **llm: Any) -> Config:
    return Config(
        paths=PathsConfig(data_dir=tmp_path / "data"),
        detector=DetectorConfig(warmup_seconds=30, lag_seconds=5, pre_roll=10, post_roll=25),
        llm=LLMConfig(**llm),
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


def _llm_reply(body: dict[str, Any]) -> str:
    user = body["messages"][0]["content"]
    if body["model"] == "claude-haiku-5-5":
        drop = "clip" in user.split("Transkript:")[0]  # third spike's chat says "clip"
        return json.dumps({"keep": not drop, "rough_score": 20 if drop else 75, "reason": "test"})
    score = 90 if "yok artık" in user else 50  # second spike passes, first is below threshold
    return json.dumps(judge_payload(score=score, start=7.7, end=31.0))


def _run(conn: sqlite3.Connection, cfg: Config, fake: FakeAnthropic, whisper: FakeWhisper) -> Any:
    transcriber = Transcriber(TranscribeConfig(), cfg.model_dir, loader=lambda c, d: whisper)
    stages = Stages(conn, cfg, transcriber=transcriber, llm=LLMClient(conn, cfg.llm, fake.client()))
    try:
        return asyncio.run(run_until_idle(conn, stages.handle, poll=0.01))
    finally:
        transcriber.close()


def test_full_pipeline(session: int, tmp_path: Path, conn: sqlite3.Connection) -> None:
    cfg = _cfg(tmp_path)
    det = asyncio.run(analyze_session(conn, cfg, session, with_audio=True))
    assert len(det.candidate_ids) == 3
    fake = FakeAnthropic([_llm_reply] * 20)
    whisper = FakeWhisper()
    stats = _run(conn, cfg, fake, whisper)

    assert stats.dead == 0 and stats.failed == 0
    assert stats.by_type == {"cut": 3, "transcribe": 3, "prefilter": 3, "judge": 2, "render": 1}
    assert len(whisper.calls) == 3 and all(c.endswith("raw.wav") for c in whisper.calls)
    statuses = dict(conn.execute("SELECT id, status FROM candidates ORDER BY peak_ts").fetchall())
    first, second, third = sorted(statuses, key=lambda i: i)
    assert statuses[second] == "rendered"
    assert statuses[first] == "rejected" and statuses[third] == "rejected"

    clip = conn.execute("SELECT * FROM clips WHERE candidate_id = ?", (second,)).fetchone()
    assert Path(clip["raw_path"]).is_file()
    assert Path(clip["transcript_path"]).is_file()
    assert clip["llm_score"] == 90 and clip["title"]
    # Snapped to word boundaries: start 8.0 - 0.15, end 31 -> nearest word end 33 + 0.3.
    assert clip["start_rel"] == pytest.approx(7.85) and clip["end_rel"] == pytest.approx(33.3)
    data = json.loads(clip["llm_json"])
    assert data["prefilter"]["keep"] and data["judge"]["model"] == "claude-sonnet-5-5"
    assert data["judge"]["texts"]["x"]["text"]
    assert data["judge"]["caption_fixes"] == [{"from": "valoran", "to": "Valorant"}]

    judge_req = next(b for b in fake.requests if b["model"] == "claude-sonnet-5-5")
    user = judge_req["messages"][0]["content"]
    assert "[8.0] oha ne oldu" in user and "## Chat" in user and "## Ses enerjisi" in user
    usage = conn.execute("SELECT purpose, COUNT(*) FROM llm_usage GROUP BY purpose").fetchall()
    assert dict(usage) == {"prefilter": 3, "judge": 2}
    assert jobs.counts(conn) == {"done": 12}
    assert Path(clip["final_path"]).is_file() and Path(clip["thumb_path"]).is_file()
    ass = (Path(clip["raw_path"]).parent / "clip.ass").read_text(encoding="utf-8")
    assert "OHA" in ass and "İNANILMAZ" in ass  # words in [start, end], tr_upper
    assert "VE O AN GELDİ" in ass  # hook


def test_hourly_cap(session: int, tmp_path: Path, conn: sqlite3.Connection) -> None:
    cfg = _cfg(tmp_path, max_candidates_per_hour=1)
    asyncio.run(analyze_session(conn, cfg, session, with_audio=False))

    def reply(b: dict[str, Any]) -> str:
        if b["model"] == "claude-haiku-5-5":
            return json.dumps({"keep": True, "rough_score": 80, "reason": "x"})
        return json.dumps(judge_payload(score=95))

    fake = FakeAnthropic([reply] * 6)
    _run(conn, cfg, fake, FakeWhisper())
    judged = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE status IN ('judged', 'rendered')"
    ).fetchone()[0]
    assert judged == 1
    reasons = [
        json.loads(r[0])["judge"]["reason"]
        for r in conn.execute("SELECT llm_json FROM clips").fetchall()
        if "model" not in json.loads(r[0])["judge"]
    ]
    assert reasons == ["saatlik aday sınırı doldu"] * 2
    assert sum(b["model"] == "claude-sonnet-5-5" for b in fake.requests) == 1


def test_budget_stop_rejects_without_calls(
    session: int, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    cfg = _cfg(tmp_path, daily_budget_usd=0.01)
    asyncio.run(analyze_session(conn, cfg, session, with_audio=False))
    conn.execute(
        "INSERT INTO llm_usage (ts, model, purpose, input_tokens, output_tokens, cache_read, usd)"
        " VALUES (?, 'claude-sonnet-5-5', 'judge', 0, 0, 0, 0.02)",
        (time.time(),),
    )
    fake = FakeAnthropic([_llm_reply] * 6)
    stats = _run(conn, cfg, fake, FakeWhisper())
    assert fake.requests == []
    assert stats.dead == 0
    statuses = {r[0] for r in conn.execute("SELECT status FROM candidates").fetchall()}
    assert statuses == {"rejected"}
    reasons = {json.loads(r[0])["prefilter"]["reason"] for r in conn.execute(
        "SELECT llm_json FROM clips").fetchall()}  # fmt: skip
    assert all("Günlük LLM bütçesi doldu" in r for r in reasons)


def test_no_api_key_leaves_llm_jobs(session: int, tmp_path: Path, conn: sqlite3.Connection) -> None:
    cfg = _cfg(tmp_path)
    asyncio.run(analyze_session(conn, cfg, session, with_audio=False))
    transcriber = Transcriber(TranscribeConfig(), cfg.model_dir, loader=lambda c, d: FakeWhisper())
    stages = Stages(conn, cfg, transcriber=transcriber, llm=None)
    try:
        asyncio.run(run_until_idle(conn, stages.handle, pools=("cpu", "gpu"), poll=0.01))
    finally:
        transcriber.close()
    assert jobs.counts(conn) == {"done": 6, "queued": 3}
    assert {r[0] for r in conn.execute("SELECT status FROM candidates")} == {"transcribed"}


def test_cut_produces_mp4_and_wav(session: int, tmp_path: Path, conn: sqlite3.Connection) -> None:
    res = asyncio.run(cut(conn, session, 20, 35, tmp_path / "c"))
    assert res.raw_mp4.stat().st_size > 0 and res.raw_wav.stat().st_size > 0
    assert res.duration == pytest.approx(15)
    assert res.t0 == pytest.approx(20)
    assert not (tmp_path / "c" / "segments.txt").exists()


def test_failed_job_is_retried_then_dead(conn: sqlite3.Connection) -> None:
    jobs.enqueue(conn, "cut", {"candidate_id": 999}, "cpu", max_attempts=1)

    async def boom(job: jobs.Job) -> None:
        raise RuntimeError("patladı")

    stats = asyncio.run(run_until_idle(conn, boom, poll=0.01))
    assert stats.dead == 1
    assert jobs.counts(conn) == {"dead": 1}


def test_transcriber_output_format(tmp_path: Path) -> None:
    loads: list[int] = []

    def loader(c: TranscribeConfig, d: Path) -> FakeWhisper:
        loads.append(1)
        return FakeWhisper()

    t = Transcriber(TranscribeConfig(), tmp_path, loader=loader)
    try:
        out = asyncio.run(t.transcribe(tmp_path / "a.wav", tmp_path / "t.json"))
        asyncio.run(t.transcribe(tmp_path / "b.wav", tmp_path / "t2.json"))
    finally:
        t.close()
    assert loads == [1]  # model loads once
    assert out[0]["text"] == "valoran oynuyoruz"
    assert out[0]["words"][0] == {"w": "valoran", "s": 1.0, "e": 1.8, "p": 0.9}
    assert json.loads((tmp_path / "t.json").read_text(encoding="utf-8")) == out


def test_work_cli_without_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from clipper.__main__ import main

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["work", "--pool", "cpu"]) == 0
