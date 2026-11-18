"""Faz 4: captions, layouts, render output checks (ffprobe size/duration/audio, loudness)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from clipper.config import LLMConfig, RenderConfig
from clipper.llm.client import LLMClient
from clipper.llm.facecam_vision import suggest_facecam
from clipper.media import probe
from clipper.render.ass_captions import CapWord, ass_time, build_ass, clip_words, group_words
from clipper.render.clip import render_clip
from clipper.render.ffmpeg import encoder_args, parse_progress
from clipper.render.layouts import Rect, effective_layout, filter_graph
from clipper.render.tr_text import wrap_words
from tests.llm_mock import FakeAnthropic

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")


def _w(text: str, s: float, e: float) -> CapWord:
    return CapWord(text, s, e)


# -- captions -----------------------------------------------------------------------------


def test_wrap_words() -> None:
    assert wrap_words(["bir", "iki", "üç", "dört"], 8) == [["bir", "iki"], ["üç", "dört"]]
    assert wrap_words(["çokuzunbirkelime", "x"], 8) == [["çokuzunbirkelime"], ["x"]]


def test_group_words_rules() -> None:
    words = [
        _w("bir", 0.0, 0.2), _w("iki", 0.3, 0.5), _w("üç", 0.6, 0.8), _w("dört", 0.9, 1.1),
        _w("beş", 1.2, 1.4),  # 5th word -> new group
        _w("altı.", 1.5, 1.7),  # punctuation ends a group
        _w("yedi", 1.8, 2.0), _w("sekiz", 3.0, 3.2),  # > 0.6 s gap between them
        _w("dokuzuncukelime", 3.3, 3.6), _w("onuncukelime", 3.7, 4.0),  # > 18 chars each pair
    ]  # fmt: skip
    groups = [[w.w for w in g] for g in group_words(words)]
    assert groups == [
        ["bir", "iki", "üç", "dört"],
        ["beş", "altı."],
        ["yedi"],
        ["sekiz"],
        ["dokuzuncukelime"],
        ["onuncukelime"],
    ]


def test_build_ass_lines_and_highlight() -> None:
    words = [_w("istanbul", 0.5, 1.0), _w("ılık", 1.1, 1.5), _w("{x}", 1.6, 2.0)]
    ass = build_ass(words, duration=10, layout="stack", hook="bak şuna")
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == 4  # one per word + hook
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Montserrat ExtraBold,78" in ass and "BorderStyle" in ass
    hook = dialogues[0]
    assert ",Hook," in hook and "BAK ŞUNA" in hook and "0:00:00.00,0:00:02.50" in hook
    first = dialogues[1]
    assert r"\pos(540,640)" in first  # stack: captions on the seam
    assert r"{\c&H00FFFF&\fscx110\fscy110}İSTANBUL{\r} ILIK (X)" in first
    assert "0:00:00.50,0:00:01.10" in first  # until the next word starts
    assert "{x}" not in ass
    other = build_ass(words, duration=10, layout="blur")
    assert r"\pos(540,1350)" in other and ",Hook," not in other


def test_clip_words_and_times() -> None:
    raw = [("a", 1.0, 1.5), ("b", 5.0, 5.5), ("c", 20.0, 21.0)]
    assert clip_words(raw, 4.0, 10.0) == [CapWord("b", 1.0, 1.5)]
    assert ass_time(3725.456) == "1:02:05.46"


# -- layouts / encoder --------------------------------------------------------------------


def test_rect_and_layout_fallback() -> None:
    r = Rect.from_json('{"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4}')
    assert r is not None and Rect.from_json(r.to_json()) == r
    with pytest.raises(ValueError):
        Rect(0.8, 0.1, 0.3, 0.2)  # spills over the right edge
    assert effective_layout("stack", None) == "blur"
    assert effective_layout("stack", r) == "stack"
    assert effective_layout("weird", r) == "blur"


def test_filter_graph_uses_relative_ass() -> None:
    g = filter_graph("stack", Rect(0.7, 0.6, 0.3, 0.4), ass_name="clip.ass", fontsdir="fonts")
    assert "vstack=inputs=2" in g and "subtitles=clip.ass:fontsdir=fonts" in g
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" in g and g.endswith("[a]")
    assert ":\\" not in g


def test_encoder_args_and_progress() -> None:
    assert encoder_args("h264_nvenc")[:4] == ["-c:v", "h264_nvenc", "-preset", "p5"]
    assert "libx264" in encoder_args("libx264") and "+faststart" in encoder_args("libx264")
    assert parse_progress("out_time_us=2500000\n") == 2.5
    assert parse_progress("frame=10") is None


# -- real renders -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sample_raw(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg yok")
    out = tmp_path_factory.mktemp("render") / "raw.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12",
            "-f", "lavfi", "-i", "sine=frequency=330:duration=12",
            "-af", "volume=-20dB",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out),
        ],
        check=True,
    )  # fmt: skip
    return out


def _loudness(path: Path) -> float:
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "loudnorm=print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", res.stderr, re.S)
    assert m, res.stderr[-500:]
    return float(json.loads(m.group(0))["input_i"])


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["stack", "blur", "center"])
def test_render_output(layout: str, sample_raw: Path, tmp_path: Path) -> None:
    words = [("oha", 1.0, 1.4), ("ne", 1.5, 1.7), ("oldu", 1.8, 2.3), ("şimdi", 3.0, 3.5)]
    seen: list[float] = []
    res = asyncio.run(
        render_clip(
            RenderConfig(encoder="libx264"),
            sample_raw,
            0.5,
            10.5,
            words,
            tmp_path / layout,
            layout=layout,
            facecam=Rect(0.7, 0.6, 0.3, 0.4),
            hook="izle",
            thumb_at=5.0,
            progress=seen.append,
        )
    )
    info = asyncio.run(probe(res.final))
    assert (info.width, info.height) == (1080, 1920)
    assert abs(info.duration - 10.0) <= 0.2
    assert info.has_audio
    assert abs(_loudness(res.final) - (-14.0)) <= 1.5
    assert res.thumb.is_file() and res.thumb.stat().st_size > 1000
    assert seen and seen[-1] == pytest.approx(1.0, abs=0.05)
    assert (tmp_path / layout / "clip.ass").is_file()
    assert not list((tmp_path / layout).glob(".*part*"))


# -- facecam vision -----------------------------------------------------------------------


def test_facecam_median_of_frames(tmp_path: Path, conn: object) -> None:
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)
    frames = []
    for i in range(3):
        f = tmp_path / f"f{i}.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        frames.append(f)
    boxes = [
        {"found": True, "x": 0.70, "y": 0.60, "w": 0.28, "h": 0.35, "confidence": 80},
        {"found": True, "x": 0.72, "y": 0.62, "w": 0.26, "h": 0.33, "confidence": 90},
        {"found": True, "x": 0.10, "y": 0.10, "w": 0.20, "h": 0.20, "confidence": 30},
    ]
    replies = [lambda b, d=d: json.dumps({**d, "reason": "sağ alt"}) for d in boxes]
    fake = FakeAnthropic(list(replies))
    llm = LLMClient(conn, LLMConfig(), fake.client())
    sug = asyncio.run(suggest_facecam(llm, frames, "claude-sonnet-5-5"))
    assert sug.rect == Rect(0.7, 0.6, 0.26, 0.33)
    assert sug.confidence == 80
    content = fake.requests[0]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"


def test_facecam_not_found(tmp_path: Path, conn: object) -> None:
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)
    f = tmp_path / "f.jpg"
    f.write_bytes(b"\xff\xd8")
    no = json.dumps(
        {"found": False, "x": 0, "y": 0, "w": 0, "h": 0, "confidence": 70, "reason": "yok"}
    )
    fake = FakeAnthropic([lambda b: no, lambda b: no])
    llm = LLMClient(conn, LLMConfig(), fake.client())
    sug = asyncio.run(suggest_facecam(llm, [f, f], "claude-sonnet-5-5"))
    assert sug.rect is None and sug.reasons == ["yok", "yok"]
