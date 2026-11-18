"""TESTPLAN Bölüm 3: unit/integration checks that had no test yet (IDs in each name)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from clipper import chatlog
from clipper.config import LLMConfig, RenderConfig
from clipper.llm.client import LLMClient
from clipper.llm.schemas import PrefilterDecision
from clipper.models import ChatMessage, Segment
from clipper.platforms.twitch.chat_irc import parse_line, to_items
from clipper.render.ass_captions import ass_time
from clipper.render.clip import render_clip
from clipper.sessions import add_segment, create_session, get_or_create_streamer
from tests.llm_mock import message

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")


def _ffprobe_streams(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def raw_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg yok")
    out = tmp_path_factory.mktemp("raw") / "raw.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=8", "-af", "volume=-20dB",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac", "-shortest",
         str(out)],
        check=True,
    )  # fmt: skip
    return out


# ---- CHAT-05 ----------------------------------------------------------------------------


def test_chat05_raw_usernames_never_written(tmp_path: Path) -> None:
    line = (
        "@badge-info=;color=#FF0000;display-name=GizliKullanici;emotes=;id=abc;"
        "tmi-sent-ts=1700000000000;user-id=987654 :gizlikullanici!gizlikullanici@"
        "gizlikullanici.tmi.twitch.tv PRIVMSG #kanal :selam millet"
    )
    msg = to_items(parse_line(line), 0.0)[0]
    assert isinstance(msg, ChatMessage)
    out = tmp_path / "chat.jsonl"
    chatlog.write_jsonl(out, [msg])
    raw = out.read_text(encoding="utf-8").lower()
    assert "gizlikullanici" not in raw and "987654" not in raw
    assert len(msg.user_id) == 40


# ---- REC-05 -----------------------------------------------------------------------------


@needs_ffmpeg
def test_rec05_cut_across_segment_boundary(conn: sqlite3.Connection, tmp_path: Path) -> None:
    from clipper.media import probe
    from clipper.recorder.cutter import cut

    sid = get_or_create_streamer(conn, "k", "twitch", "k")
    session_id, sdir = create_session(conn, tmp_path / "data", sid, "vod")
    for i in range(3):
        p = sdir / "seg" / f"{i}.ts"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=4",
             "-f", "lavfi", "-i", "sine=frequency=500:duration=4",
             "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac",
             "-output_ts_offset", str(4 * i), "-f", "mpegts", str(p)],
            check=True,
        )  # fmt: skip
        add_segment(conn, Segment(session_id, i, str(p), 4.0 * i, 4.0))
    res = asyncio.run(cut(conn, session_id, 2.5, 9.5, tmp_path / "clip"))
    info = asyncio.run(probe(res.raw_mp4))
    assert abs(res.t0 - 2.5) <= 0.5
    assert abs(info.duration - 7.0) <= 0.5
    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(res.raw_mp4)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    # No dropped frames across the 4 s / 8 s boundaries (30 fps, keyframe slack 1 s).
    assert abs(int(frames) - 30 * info.duration) <= 30


# ---- CNT-04 / JOB-04 --------------------------------------------------------------------


def _live_run(conn: sqlite3.Connection, tmp_path: Path, monkeypatch: Any, *, recorded: float,
              clip_job: bool) -> list[tuple[float, float]]:  # fmt: skip
    from types import SimpleNamespace

    from clipper import jobs
    from clipper.config import Config
    from clipper.live import content

    sid = get_or_create_streamer(conn, "k", "twitch", "k")
    session_id, sdir = create_session(conn, tmp_path / "data", sid, "live")
    add_segment(conn, Segment(session_id, 0, str(sdir / "seg" / "0.ts"), 0.0, recorded))
    if clip_job:
        jobs.enqueue(conn, "transcribe", {"candidate_id": 1}, "gpu")
    ranges: list[tuple[float, float]] = []
    stop = asyncio.Event()

    async def fake_extract(_c: Any, _s: int, a: float, b: float, wav: Path, _f: str) -> float:
        ranges.append((a, b))
        stop.set()
        return a

    class Tr:
        async def transcribe(self, wav: Path, out: Path, *, origin: float | None = None) -> list:
            return []

    monkeypatch.setattr(content, "extract_range", fake_extract)
    sess = SimpleNamespace(cfg=Config(), conn=conn, session_id=session_id, session_dir=sdir)
    task = content.live_transcriber_task(Tr(), poll=0.01)  # type: ignore[arg-type]

    async def go() -> None:
        loop_task = asyncio.create_task(task(sess, stop))  # type: ignore[arg-type]
        await asyncio.sleep(0.3)
        stop.set()
        await loop_task

    asyncio.run(go())
    return ranges


def test_cnt04_live_transcript_skips_when_behind(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    caplog.set_level(logging.INFO)
    ranges = _live_run(conn, tmp_path, monkeypatch, recorded=300.0, clip_job=False)
    # 300 s recorded, 60 s max lag: jumps to the last 20 s chunk (minus 4 s overlap).
    assert ranges == [(276.0, 300.0)]
    assert "live transcript behind, skipping" in caplog.text


def test_job04_clip_transcripts_go_before_live(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: Any
) -> None:
    assert _live_run(conn, tmp_path, monkeypatch, recorded=40.0, clip_job=True) == []


# ---- SEC-02 -----------------------------------------------------------------------------


def test_sec02_symlink_out_of_data_dir_refused(tmp_path: Path) -> None:
    from clipper.util.fs import safe_join

    base = tmp_path / "data"
    base.mkdir()
    (tmp_path / "secret.txt").write_text("gizli")
    try:
        (base / "link.txt").symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("symlink oluşturulamıyor (Windows yetkisi)")
    with pytest.raises(ValueError):
        safe_join(base, "link.txt")


# ---- LLM-06 -----------------------------------------------------------------------------


def _client_with(statuses: list[int], max_retries: int) -> tuple[anthropic.AsyncAnthropic, list]:
    seen: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        code = statuses.pop(0) if statuses else 200
        seen.append(code)
        if code != 200:
            return httpx2.Response(
                code, headers={"retry-after-ms": "1"},
                json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}},
            )  # fmt: skip
        text = json.dumps({"keep": True, "rough_score": 60, "reason": "ok"})
        return httpx2.Response(200, json=message(body["model"], text))

    client = anthropic.AsyncAnthropic(
        api_key="k", max_retries=max_retries,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )  # fmt: skip
    return client, seen


def _ask(llm: LLMClient) -> Any:
    return asyncio.run(
        llm.structured(purpose="prefilter", model="claude-haiku-5-5", system="s", user="u",
                       schema=PrefilterDecision, effort="low")
    )  # fmt: skip


def test_llm06_overloaded_is_retried_and_never_swallowed(conn: sqlite3.Connection) -> None:
    client, seen = _client_with([529, 429], max_retries=2)
    assert _ask(LLMClient(conn, LLMConfig(), client)).value.rough_score == 60
    assert seen == [529, 429, 200]
    # Still overloaded after the SDK retries: the error propagates (the job queue retries
    # the stage with backoff) instead of turning into a failed candidate.
    client, _ = _client_with([529, 529, 529], max_retries=2)
    with pytest.raises(anthropic.APIStatusError):
        _ask(LLMClient(conn, LLMConfig(), client))


# ---- RND-01 / RND-06 / RND-08 -----------------------------------------------------------


@needs_ffmpeg
def test_rnd01_rnd06_output_format_in_awkward_dir(raw_clip: Path, tmp_path: Path) -> None:
    # ":" cannot be in a Windows file name; there the drive letter ("C:") covers it.
    import sys

    out_dir = tmp_path / "Çıktı klasörü; ğüşiöç" / ("a b,c" if sys.platform == "win32" else "a b:c")
    res = asyncio.run(
        render_clip(RenderConfig(encoder="libx264"), raw_clip, 1.0, 7.0,
                    [("İstanbul'da", 1.2, 1.8), ("ılık", 2.0, 2.4)], out_dir, layout="blur",
                    hook="izle")
    )  # fmt: skip
    info = _ffprobe_streams(res.final)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (v["codec_name"], v["pix_fmt"], v["width"], v["height"]) == (
        "h264", "yuv420p", 1080, 1920)  # fmt: skip
    assert (a["codec_name"], a["sample_rate"]) == ("aac", "48000")
    head = res.final.read_bytes()[:4096]
    assert head.find(b"moov") != -1 and head.find(b"moov") < max(head.find(b"mdat"), 4096)


def test_rnd08_caption_events_match_word_times(tmp_path: Path) -> None:
    from clipper.render.ass_captions import build_ass, clip_words

    words = [("bir", 10.00, 10.31), ("iki", 10.40, 10.77), ("üç", 11.02, 11.55)]
    ass = build_ass(clip_words(words, 9.0, 15.0), duration=6.0, layout="blur")
    starts = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            h, m, sec = line.split(",")[1].split(":")
            starts.append(int(h) * 3600 + int(m) * 60 + float(sec))
    # One highlight event per word, starting at the word's own time (±50 ms); it stays on
    # until the next word starts, and the last ends with the word.
    assert len(starts) == len(words)
    for (_w, s, _e), got in zip(words, starts, strict=True):
        assert abs(got - (s - 9.0)) <= 0.05
    assert ass_time(words[-1][2] - 9.0) in ass


def test_logs_have_no_secrets_after_llm_call(conn: sqlite3.Connection, caplog: Any) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = _client_with([], max_retries=0)
    _ask(LLMClient(conn, LLMConfig(), client))
    assert "sk-ant" not in caplog.text and "x-api-key" not in caplog.text.lower()
