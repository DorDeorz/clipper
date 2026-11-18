from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipper import chatlog
from clipper.models import ChatMessage, hash_user_id

TD_EXPORT = {
    "streamer": {"name": "yayinci", "id": 1},
    "video": {"id": "123", "start": 0, "end": 100},
    "comments": [
        {
            "_id": "c2",
            "content_offset_seconds": 12.5,
            "commenter": {"_id": "42", "name": "ayse", "display_name": "Ayşe"},
            "message": {
                "body": "KEKW çok iyi",
                "fragments": [
                    {"text": "KEKW", "emoticon": {"emoticon_id": "1"}},
                    {"text": " çok iyi", "emoticon": None},
                ],
            },
        },
        {
            "_id": "c1",
            "content_offset_seconds": 3,
            "commenter": {"_id": "7", "name": "mehmet"},
            "message": {"fragments": [{"text": "selam", "emoticon": None}]},
        },
    ],
}


def test_jsonl_roundtrip_gzip(tmp_path: Path) -> None:
    msgs = [
        ChatMessage(1.0, "u1", "ığüşöç İ", ["KEKW"], "twitch"),
        ChatMessage(2.5, "u2", "süper", [], "youtube", weight=1.5),
    ]
    for name in ("c.jsonl", "c.jsonl.gz"):
        path = tmp_path / name
        assert chatlog.write_jsonl(path, msgs) == 2
        assert list(chatlog.read_jsonl(path)) == msgs
    assert "weight" not in json.loads((tmp_path / "c.jsonl").read_text("utf-8").splitlines()[0])


def test_bad_line(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    p.write_text('{"ts": 1}\n', encoding="utf-8")
    with pytest.raises(chatlog.ChatFormatError, match=r"x\.jsonl:1"):
        list(chatlog.read_jsonl(p))


def test_twitchdownloader_import(tmp_path: Path) -> None:
    p = tmp_path / "td.json"
    p.write_text(json.dumps(TD_EXPORT), encoding="utf-8")
    msgs = chatlog.load_any(p)
    assert [m.ts for m in msgs] == [3.0, 12.5]
    assert msgs[0].text == "selam"
    assert msgs[1].text == "KEKW çok iyi"
    assert msgs[1].emotes == ["KEKW"]
    assert msgs[1].user_id == hash_user_id("twitch", "42")
    assert all(m.platform == "twitch" for m in msgs)


def test_unknown_json(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(chatlog.ChatFormatError):
        chatlog.load_any(p)


def test_shift_and_absolute() -> None:
    msgs = [ChatMessage(1_700_000_005.0, "u", "a"), ChatMessage(1_700_000_001.0, "u", "b")]
    assert chatlog.is_absolute(msgs)
    shifted = list(chatlog.shift(msgs, 1_700_000_002.0))
    assert [m.ts for m in shifted] == [3.0]
    assert not chatlog.is_absolute(shifted)
    assert not chatlog.is_absolute([])
