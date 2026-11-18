"""r3 content path (transcript + Haiku scan), speech signals, few-shot feedback."""

from __future__ import annotations

import asyncio
import itertools
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from clipper import jobs
from clipper.config import Config, ContentScanConfig, DetectorConfig, PathsConfig, TranscribeConfig
from clipper.content.batch import scan_session, scan_windows
from clipper.content.scanner import ContentAccumulator, ContentScanner, ScanOutcome
from clipper.content.transcript import (
    TRANSCRIPT_FILE,
    TLine,
    TWord,
    append_lines,
    from_whisper,
    read_lines,
    speech_rate,
    speech_ratio,
    window_text,
)
from clipper.detect.pipeline import analyze_session
from clipper.ingest import ingest_local
from clipper.llm.client import LLMClient
from clipper.llm.prompts import FeedbackExamples, candidate_context, feedback_block
from clipper.stages import Stages
from clipper.transcribe.whisper import Transcriber
from clipper.worker import run_until_idle
from tests.fixtures.make_fixtures import BASE_TS
from tests.llm_mock import FakeAnthropic, judge_payload
from tests.test_stages import FakeWhisper


def _line(t0: float, words: list[str], step: float = 0.5) -> TLine:
    ws = tuple(TWord(w, t0 + i * step, t0 + i * step + step * 0.9) for i, w in enumerate(words))
    return TLine(t0, ws[-1].e, " ".join(words), ws)


# -- transcript helpers -------------------------------------------------------------------


def test_transcript_roundtrip_and_helpers(tmp_path: Path) -> None:
    lines = from_whisper(
        [{"start": 0, "end": 2, "text": " a b", "words": [
            {"w": "a", "s": 0.0, "e": 1.0, "p": 0.9}, {"w": "b", "s": 1.0, "e": 2.0, "p": 0.8}
        ]}],
        offset=100,
    )  # fmt: skip
    path = tmp_path / TRANSCRIPT_FILE
    append_lines(path, lines)
    append_lines(path, [_line(110, ["c", "d"])])
    back = read_lines(path)
    assert back[0] == lines[0] and back[0].words[0].s == 100.0
    assert speech_ratio(back, 100, 104) == pytest.approx(0.5)
    assert window_text(back, 101, 112) == "[+0.0] b\n[+9.0] c d"
    assert speech_rate(back) == {100: 1.0, 101: 1.0, 110: 2.0}


def test_scan_windows_cover_the_session() -> None:
    w = scan_windows(0, 200, 60, 90)
    assert w == [(0, 90), (60, 150), (110, 200)]
    assert scan_windows(0, 50, 60, 90) == [(0, 50)]


# -- accumulator --------------------------------------------------------------------------


def _hit(a: float, score: int, bs: float, be: float) -> ScanOutcome:
    return ScanOutcome(a, a + 90, False, score, "story", f"r{a:g}", a + bs, a + be)


def test_consecutive_high_windows_merge() -> None:
    acc = ContentAccumulator(ContentScanConfig(), threshold=3.0)
    assert acc.feed(_hit(0, 80, 40, 90)) == []
    assert acc.feed(_hit(60, 90, 0, 50)) == []
    (d,) = acc.feed(ScanOutcome(120, 210, False, 20, "none", "x", 120, 130))
    assert (d.win_start, d.win_end) == (30, 120)
    assert d.content_score == 90 and d.features["content"]["windows"] == 2
    assert d.sources == ["content"] and d.det_score == pytest.approx(3.0 * 90 / 70)


def test_three_minute_cap() -> None:
    acc = ContentAccumulator(ContentScanConfig(), threshold=3.0)
    out = []
    for a in range(0, 600, 60):
        out += acc.feed(_hit(a, 85, 0, 90))
    out += acc.flush()
    assert len(out) >= 3
    assert all(d.win_end - d.win_start <= 180 for d in out)
    assert all(b.win_start >= a.win_end for a, b in itertools.pairwise(out))


def test_low_or_skipped_window_closes() -> None:
    acc = ContentAccumulator(ContentScanConfig(), threshold=3.0)
    acc.feed(_hit(0, 80, 10, 30))
    assert len(acc.feed(ScanOutcome(60, 150, True))) == 1
    assert acc.flush() == []


def test_silent_window_skipped_without_llm() -> None:
    scanner = ContentScanner(ContentScanConfig(), None, streamer="x", category=None)
    lines = [_line(0, ["tek", "kelime"])]
    o = asyncio.run(scanner.scan(lines, [], 0, 90))
    assert o.skipped and o.score is None


# -- batch scan against a session ---------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    return Config(
        paths=PathsConfig(data_dir=tmp_path / "data"),
        detector=DetectorConfig(warmup_seconds=30, lag_seconds=5, pre_roll=10, post_roll=10),
        content_scan=ContentScanConfig(window_seconds=60, every_seconds=60),
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


def _talk(t0: float, t1: float, word: str) -> list[TLine]:
    return [_line(t, [word] * 8) for t in range(int(t0), int(t1), 5)]


def _content_reply(body: dict[str, Any]) -> str:
    user = body["messages"][0]["content"]
    good = "hikaye" in user
    return json.dumps(
        {
            "score": 88 if good else 30,
            "kind": "story" if good else "none",
            "reason": "Yayıncı komik bir hikâye anlatıyor." if good else "rutin",
            "best_start": 5.0,
            "best_end": 55.0,
        }
    )


def test_scan_session_adds_and_merges(
    session: int, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    cfg = _cfg(tmp_path)
    sdir = Path(conn.execute("SELECT dir FROM sessions WHERE id = ?", (session,)).fetchone()[0])
    # 0-60 talk (boring), 60-120 a story, 120-180 silence.
    append_lines(sdir / TRANSCRIPT_FILE, _talk(0, 60, "oyun") + _talk(60, 120, "hikaye"))
    asyncio.run(analyze_session(conn, cfg, session, with_audio=False))
    before = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

    fake = FakeAnthropic([_content_reply] * 10)
    llm = LLMClient(conn, cfg.llm, fake.client())

    def factory(sess: sqlite3.Row, base: float | None) -> ContentScanner:
        return ContentScanner(cfg.content_scan, llm, streamer=sess["streamer"], category=None)

    ids = asyncio.run(scan_session(conn, cfg, session, factory))
    assert len(fake.requests) == 2  # third window is silent -> not sent
    assert fake.requests[0]["model"] == "claude-haiku-5-5"
    scans = conn.execute("SELECT score, kind FROM content_scans ORDER BY win_start").fetchall()
    assert [tuple(r) for r in scans] == [(30, "none"), (88, "story")]
    assert len(ids) == 1
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (ids[0],)).fetchone()
    sources = json.loads(row["sources"])
    assert "content" in sources and row["content_score"] == 88
    feats = json.loads(row["features"])
    assert feats["content"]["kind"] == "story"
    after = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    # Spike 2 (100 s) overlaps the story window (55..125) -> merged, no new candidate.
    assert after == before and sources == ["reactive", "content"]
    usage = conn.execute("SELECT purpose, COUNT(*) FROM llm_usage GROUP BY 1").fetchall()
    assert dict(usage) == {"content_scan": 2}


def test_speech_signal_reaches_detector(
    session: int, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    cfg = _cfg(tmp_path)
    sdir = Path(conn.execute("SELECT dir FROM sessions WHERE id = ?", (session,)).fetchone()[0])
    append_lines(sdir / TRANSCRIPT_FILE, _talk(0, 180, "laf"))
    asyncio.run(analyze_session(conn, cfg, session, with_audio=False))
    feats = [json.loads(r[0]) for r in conn.execute("SELECT features FROM candidates")]
    assert feats and all("speech" in f["x"] for f in feats)


# -- stage jobs ---------------------------------------------------------------------------


def test_session_transcribe_job_writes_transcript(
    session: int, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    cfg = _cfg(tmp_path)
    whisper = FakeWhisper()
    transcriber = Transcriber(TranscribeConfig(), cfg.model_dir, loader=lambda c, d: whisper)
    stages = Stages(conn, cfg, transcriber=transcriber, llm=None)
    jobs.enqueue(conn, "session_transcribe", {"session_id": session}, "gpu", 300)
    try:
        stats = asyncio.run(run_until_idle(conn, stages.handle, pools=("gpu",), poll=0.01))
    finally:
        transcriber.close()
    assert stats.by_type == {"session_transcribe": 1}
    assert whisper.calls and whisper.calls[0].endswith("session.wav")
    sdir = Path(conn.execute("SELECT dir FROM sessions WHERE id = ?", (session,)).fetchone()[0])
    lines = read_lines(sdir / TRANSCRIPT_FILE)
    assert [ln.text for ln in lines][:2] == ["valoran oynuyoruz", "oha ne oldu"]
    assert not (sdir / "work" / "session.wav").exists()
    q = conn.execute("SELECT type, pool FROM jobs WHERE status = 'queued'").fetchall()
    assert [tuple(r) for r in q] == [("content_scan", "io")]


def test_multi_source_skips_prefilter_and_judge_sees_context(
    session: int, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    cfg = _cfg(tmp_path)
    cfg.content_scan.enabled = False
    asyncio.run(analyze_session(conn, cfg, session, with_audio=False))
    cid = conn.execute("SELECT id FROM candidates ORDER BY peak_ts LIMIT 1").fetchone()[0]
    feats = json.loads(
        conn.execute("SELECT features FROM candidates WHERE id = ?", (cid,)).fetchone()[0]
    )
    feats["content"] = {"score": 91, "kind": "joke", "reason": "Komik bir laf."}
    feats["sources"] = ["reactive", "content"]
    conn.execute(
        "UPDATE candidates SET sources = ?, features = ? WHERE id = ?",
        (json.dumps(["reactive", "content"]), json.dumps(feats), cid),
    )
    conn.execute("DELETE FROM jobs WHERE json_extract(payload, '$.candidate_id') != ?", (cid,))
    # A past decision for this streamer feeds the few-shot block.
    conn.execute(
        "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score, features,"
        " status, created_at) VALUES (?, 1, 0, 2, 1, '{}', 'judged', 'x')",
        (session,),
    )
    old = conn.execute("SELECT MAX(id) FROM candidates").fetchone()[0]
    conn.execute(
        "INSERT INTO clips (candidate_id, title, created_at) VALUES (?, 'Eski güzel klip', 'x')",
        (old,),
    )
    clip_id = conn.execute("SELECT MAX(id) FROM clips").fetchone()[0]
    for decision, reason in (("approved", None), ("rejected", "no_context")):
        conn.execute(
            "INSERT INTO feedback (clip_id, decision, reason, features_snapshot, created_at)"
            " VALUES (?, ?, ?, '{}', 1)",
            (clip_id, decision, reason),
        )

    fake = FakeAnthropic([lambda b: json.dumps(judge_payload(start=2.0, end=30.0))] * 3)
    whisper = FakeWhisper()
    transcriber = Transcriber(TranscribeConfig(), cfg.model_dir, loader=lambda c, d: whisper)
    stages = Stages(conn, cfg, transcriber=transcriber, llm=LLMClient(conn, cfg.llm, fake.client()))
    try:
        stats = asyncio.run(run_until_idle(conn, stages.handle, poll=0.01))
    finally:
        transcriber.close()
    assert stats.by_type == {"cut": 1, "transcribe": 1, "judge": 1}
    user = fake.requests[0]["messages"][0]["content"]
    assert "Bulan yollar: chat/ses tepkisi, konuşma içeriği" in user
    assert "joke (91/100) - Komik bir laf." in user
    assert "- Eski güzel klip" in user and "bağlamsız ×1" in user
    clip = conn.execute("SELECT llm_json FROM clips WHERE candidate_id = ?", (cid,)).fetchone()
    assert json.loads(clip[0])["prefilter"]["skipped"] == "multi_source"


def test_prompt_blocks_empty_when_no_data() -> None:
    assert feedback_block(None) == "" and feedback_block(FeedbackExamples([], [])) == ""
    assert candidate_context({}) == "Bulan yollar: chat/ses tepkisi"
    ctx = candidate_context({"events": {"gift_sub": 10}, "event_filter": "sub_train"})
    assert "hediye abonelik: 10" in ctx and "abone yağmuru" in ctx
