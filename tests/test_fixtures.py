from __future__ import annotations

import itertools
import json
import subprocess
from collections import Counter
from pathlib import Path

from clipper.models import ChatMessage
from tests.fixtures.make_fixtures import BASE_TS, DEFAULT_SPIKES, iter_chat


def test_video_fixture(fixture_dir: Path) -> None:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height:format=duration",
            "-of",
            "json",
            str(fixture_dir / "video.mp4"),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    info = json.loads(out)
    kinds = {s["codec_type"] for s in info["streams"]}
    assert kinds == {"video", "audio"}
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1920, 1080)
    assert abs(float(info["format"]["duration"]) - 180) < 0.5


def test_chat_fixture_parses(fixture_dir: Path) -> None:
    lines = (fixture_dir / "chat.jsonl").read_text(encoding="utf-8").splitlines()
    msgs = [ChatMessage.from_json(json.loads(line)) for line in lines]
    assert len(msgs) > 300
    assert all(a.ts <= b.ts for a, b in itertools.pairwise(msgs))


def test_chat_has_three_clear_spikes() -> None:
    per_sec = Counter(int(m["ts"] - BASE_TS) for m in iter_chat(180))  # type: ignore[operator]
    flat = [
        per_sec.get(s, 0)
        for s in range(180)
        if not any(sp.at - 2 <= s < sp.at + sp.duration + 2 for sp in DEFAULT_SPIKES)
    ]
    flat_mean = sum(flat) / len(flat)
    assert flat_mean < 2
    for sp in DEFAULT_SPIKES:
        window = [per_sec.get(s, 0) for s in range(int(sp.at), int(sp.at + sp.duration))]
        assert sum(window) / len(window) > 5 * flat_mean


def test_chat_deterministic() -> None:
    assert list(iter_chat(60)) == list(iter_chat(60))
