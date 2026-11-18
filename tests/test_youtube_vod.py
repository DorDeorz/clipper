from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from clipper import chatlog, ingest
from clipper.config import Config, PathsConfig, YouTubeConfig
from clipper.models import hash_user_id
from clipper.platforms.youtube import innertube

VID = "abcDEF12345"

PLAYER = {
    "videoDetails": {
        "videoId": VID,
        "title": "Büyük final",
        "channelId": "UCxyz",
        "author": "Kanal",
        "isLive": False,
    },
    "microformat": {
        "playerMicroformatRenderer": {
            "liveBroadcastDetails": {"startTimestamp": "2026-11-17T18:00:00+00:00"}
        }
    },
}
INITIAL = {
    "contents": {
        "twoColumnWatchNextResults": {
            "conversationBar": {
                "liveChatRenderer": {
                    "continuations": [{"reloadContinuationData": {"continuation": "TOP"}}],
                    "header": {
                        "liveChatHeaderRenderer": {
                            "viewSelector": {
                                "sortFilterSubMenuRenderer": {
                                    "subMenuItems": [
                                        {
                                            "title": "Top chat replay",
                                            "continuation": {
                                                "reloadContinuationData": {"continuation": "TOP"}
                                            },
                                        },
                                        {
                                            "title": "Live chat replay",
                                            "continuation": {
                                                "reloadContinuationData": {"continuation": "ALL"}
                                            },
                                        },
                                    ]
                                }
                            }
                        }
                    },
                }
            }
        }
    }
}
WATCH_HTML = (
    '<html><script>ytcfg.set({"INNERTUBE_API_KEY":"KEY123",'
    '"INNERTUBE_CONTEXT_CLIENT_VERSION":"2.20261117.01.00"});</script>'
    f"<script>var ytInitialPlayerResponse = {json.dumps(PLAYER)};var meta = 1;</script>"
    f"<script>var ytInitialData = {json.dumps(INITIAL)};</script></html>"
)


def _text_action(
    offset_ms: int,
    author: str,
    runs: list[dict[str, Any]],
    renderer: str = "liveChatTextMessageRenderer",
) -> dict[str, Any]:
    return {
        "replayChatItemAction": {
            "videoOffsetTimeMsec": str(offset_ms),
            "actions": [
                {
                    "addChatItemAction": {
                        "item": {
                            renderer: {
                                "authorExternalChannelId": author,
                                "timestampUsec": "1794990000000000",
                                "message": {"runs": runs},
                            }
                        }
                    }
                }
            ],
        }
    }


REPLAY_PAGES: dict[str, dict[str, Any]] = {
    "ALL": {
        "continuationContents": {
            "liveChatContinuation": {
                "actions": [
                    _text_action(
                        1500,
                        "UC1",
                        [{"text": "selam "}, {"emoji": {"emojiId": "😂", "shortcuts": [":joy:"]}}],
                    ),
                    _text_action(
                        3000,
                        "UC2",
                        [
                            {
                                "emoji": {
                                    "emojiId": "UCx/abc",
                                    "isCustomEmoji": True,
                                    "shortcuts": [":kekw:"],
                                }
                            }
                        ],
                    ),
                ],
                "continuations": [{"liveChatReplayContinuationData": {"continuation": "P2"}}],
            }
        }
    },
    "P2": {
        "continuationContents": {
            "liveChatContinuation": {
                "actions": [
                    _text_action(
                        9000, "UC3", [{"text": "bağış!"}], renderer="liveChatPaidMessageRenderer"
                    )
                ],
                "continuations": [{"playerSeekContinuationData": {"continuation": "SEEK"}}],
            }
        }
    },
}


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/watch":
        assert request.url.params["v"] == VID
        assert "SOCS=" in request.headers["Cookie"]
        return httpx.Response(200, text=WATCH_HTML)
    if request.url.path == "/youtubei/v1/live_chat/get_live_chat_replay":
        assert request.url.params["key"] == "KEY123"
        body = json.loads(request.content)
        assert body["context"]["client"]["clientVersion"] == "2.20261117.01.00"
        return httpx.Response(200, json=REPLAY_PAGES[body["continuation"]])
    return httpx.Response(404)


def test_parse_watch_page() -> None:
    page = innertube.parse_watch_page(WATCH_HTML, VID)
    assert page.api_key == "KEY123"
    assert page.title == "Büyük final" and page.channel_id == "UCxyz"
    assert page.chat_continuation == "ALL"  # "all messages" view preferred
    assert page.start_ts is not None


def test_consent_page_error() -> None:
    with pytest.raises(innertube.YouTubeParseError, match="INNERTUBE"):
        innertube.parse_watch_page("<html>consent.youtube.com</html>", VID)


def test_chat_replay_paging() -> None:
    async def go() -> list[Any]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://www.youtube.com"
        ) as client:
            cfg = YouTubeConfig()
            page = await innertube.fetch_watch_page(client, cfg, VID)
            return [m async for m in innertube.iter_chat_replay(client, cfg, page)]

    msgs = asyncio.run(go())
    assert [m.ts for m in msgs] == [1.5, 3.0, 9.0]
    assert msgs[0].text == "selam 😂" and msgs[0].emotes == ["😂"]
    assert msgs[1].emotes == [":kekw:"]
    assert msgs[2].weight == innertube.PAID_WEIGHT
    assert msgs[0].user_id == hash_user_id("youtube", "UC1")


def test_live_message_uses_timestamp() -> None:
    item = {
        "liveChatTextMessageRenderer": {
            "authorExternalChannelId": "UC9",
            "timestampUsec": "1794990000500000",
            "message": {"runs": [{"text": "canlı"}]},
        }
    }
    msg = innertube.renderer_to_message(item)
    assert msg is not None and msg.ts == pytest.approx(1794990000.5)
    assert innertube.renderer_to_message({"liveChatViewerEngagementMessageRenderer": {}}) is None


def test_no_chat_replay() -> None:
    page = innertube.parse_watch_page(WATCH_HTML.replace("liveChatRenderer", "x"), VID)
    assert page.chat_continuation is None

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for _ in innertube.iter_chat_replay(client, YouTubeConfig(), page):
                pass

    with pytest.raises(innertube.YouTubeParseError, match="chat tekrarı yok"):
        asyncio.run(go())


def test_full_youtube_ingest(
    fixture_dir: Path, tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(
        cfg: YouTubeConfig, video_id: str, out_dir: Path, ffmpeg: str = "ffmpeg"
    ) -> Path:
        target = out_dir / "video.mp4"
        shutil.copy(fixture_dir / "video.mp4", target)
        return target

    monkeypatch.setattr("clipper.platforms.youtube.vod.download_video", fake_download)
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))

    async def go() -> ingest.IngestResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ingest.ingest_youtube_vod(conn, cfg, client, VID)

    res = asyncio.run(go())
    assert res.chat_count == 3
    st = conn.execute("SELECT platform, channel, display_name FROM streamers").fetchone()
    assert tuple(st) == ("youtube", "UCxyz", "Kanal")
    msgs = list(chatlog.read_jsonl(res.session_dir / "chat.jsonl.gz"))
    assert msgs[-1].weight == innertube.PAID_WEIGHT
