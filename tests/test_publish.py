"""Faz 7: OAuth, publishers (MockTransport), scheduler, auto-publish, publish jobs, panel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from clipper import db, jobs
from clipper.config import Config, PathsConfig, PublishConfig, Secrets
from clipper.panel.app import PanelDeps, create_app
from clipper.publish import scheduler
from clipper.publish.base import AccessToken, AuthError, ClipForPublish, PublishError, PublishResult
from clipper.publish.instagram import InstagramPublisher
from clipper.publish.oauth import OAuthError, OAuthManager, code_challenge, providers
from clipper.publish.runner import Publishing, clip_for
from clipper.publish.tiktok import TikTokPublisher, chunk_plan, chunk_ranges
from clipper.publish.wiring import PublishKit
from clipper.publish.x import XPublisher
from clipper.publish.youtube import YouTubePublisher
from clipper.util.crypto import SecretBox, generate_key
from tests.panel_client import panel_client

JUDGE = {
    "keep": True, "score": 90, "reason": "çok komik", "start": 2.0, "end": 30.0,
    "title": "Oha anı", "hook": "bak şuna", "caption_fixes": [], "hashtags": ["#oha", "yayın"],
    "texts": {"youtube": {"title": "YT başlık", "description": "YT açıklama"},
              "instagram": {"caption": "IG metni"}, "tiktok": {"caption": "TT metni"},
              "x": {"text": "X metni"}},
    "flags": {"music_risk": False, "nsfw": False, "sensitive_topic": False,
              "needs_context": False},
}  # fmt: skip


async def no_sleep(_s: float) -> None:
    return None


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def tokens_for(extra: dict[str, Any] | None = None, token: str = "tok") -> Any:
    async def tokens(platform: str, account: str) -> AccessToken:
        return AccessToken(token, extra or {})

    return tokens


def video(tmp_path: Path, size: int) -> ClipForPublish:
    p = tmp_path / "final.mp4"
    p.write_bytes(bytes((i * 7) % 251 for i in range(size)))
    return ClipForPublish(1, p, "Başlık", "Açıklama", "Metin #oha", "X metni", ["oha"])


# ---- OAuth ------------------------------------------------------------------------------


def _secrets() -> Secrets:
    return Secrets(
        clipper_secret_key=SecretStr(generate_key()),
        youtube_client_id=SecretStr("yt-id"),
        youtube_client_secret=SecretStr("yt-secret"),
        x_client_id=SecretStr("x-id"),
        x_client_secret=SecretStr("x-secret"),
        tiktok_client_key=SecretStr("tt-key"),
        tiktok_client_secret=SecretStr("tt-secret"),
        meta_app_id=SecretStr("fb-id"),
        meta_app_secret=SecretStr("fb-secret"),
    )


def _oauth(
    tmp_path: Path, handler: Any, clock: Any = time.time
) -> tuple[OAuthManager, sqlite3.Connection]:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))
    conn = db.open_db(cfg.db_path)
    secrets = _secrets()
    assert secrets.clipper_secret_key is not None
    box = SecretBox(secrets.clipper_secret_key.get_secret_value())
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OAuthManager(conn, cfg, box, providers(cfg, secrets), client, clock=clock), conn


def test_pkce_challenges() -> None:
    v = "a" * 64
    digest = hashlib.sha256(v.encode()).digest()
    assert code_challenge(v, "S256") == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert code_challenge(v, "S256hex") == digest.hex()


def test_oauth_flow_stores_encrypted_and_refreshes(tmp_path: Path) -> None:
    now = [1000.0]
    calls: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/2/oauth2/token":
            form = parse_qs(req.content.decode())
            calls.append({"form": form, "auth": req.headers.get("authorization")})
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1",
                                                 "expires_in": 7200})  # fmt: skip
            assert form["refresh_token"] == ["RT1"]
            return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2",
                                             "expires_in": 7200})  # fmt: skip
        if req.url.path == "/2/users/me":
            assert req.headers["authorization"] == "Bearer AT1"
            return httpx.Response(200, json={"data": {"id": "42", "username": "klipci"}})
        raise AssertionError(req.url)

    oauth, conn = _oauth(tmp_path, handler, clock=lambda: now[0])
    url = oauth.start("x")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://x.com/i/oauth2/authorize?")
    assert q["redirect_uri"] == ["http://127.0.0.1:8765/callback/x"]
    assert q["code_challenge_method"] == ["S256"] and "offline.access" in q["scope"][0]
    with pytest.raises(OAuthError):
        run(oauth.callback("x", "code", "wrong-state"))
    account = run(oauth.callback("x", "code", q["state"][0]))
    assert account == "klipci"
    # Confidential client → HTTP Basic, verifier sent.
    assert calls[0]["auth"].startswith("Basic ") and "code_verifier" in calls[0]["form"]
    raw = conn.execute("SELECT data_enc FROM oauth_tokens").fetchone()[0]
    assert b"refresh_token" not in raw and b"access_token" not in raw
    assert run(oauth.access_token("x", "klipci")).token == "AT1"
    now[0] += 7200  # expired → refresh, rotated refresh token is stored
    tok = run(oauth.access_token("x", "klipci"))
    assert tok.token == "AT2" and tok.extra == {"user_id": "42"}
    assert oauth.load("x", "klipci")["refresh_token"] == "RT2"
    # State is single use.
    with pytest.raises(OAuthError):
        run(oauth.callback("x", "code", q["state"][0]))


def test_oauth_tiktok_hex_pkce_and_failed_refresh(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        form = parse_qs(req.content.decode())
        assert form["client_key"] == ["tt-key"] and form["client_secret"] == ["tt-secret"]
        if form["grant_type"] == ["authorization_code"]:
            return httpx.Response(200, json={"access_token": "A", "refresh_token": "R",
                                             "expires_in": 10, "open_id": "oid"})  # fmt: skip
        return httpx.Response(200, json={"error": "invalid_grant",
                                         "error_description": "expired"})  # fmt: skip

    oauth, _ = _oauth(tmp_path, handler, clock=lambda: 5000.0)
    url = oauth.start("tiktok", "benim")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert q["client_key"] == ["tt-key"] and q["scope"] == ["user.info.basic,video.upload"]
    assert len(q["code_challenge"][0]) == 64  # hex SHA256
    assert run(oauth.callback("tiktok", "c", q["state"][0])) == "benim"
    with pytest.raises(AuthError, match="yeniden bağla"):
        run(oauth.access_token("tiktok", "benim"))


def test_oauth_instagram_page_token(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        params = dict(req.url.params)
        if req.url.path.endswith("/oauth/access_token"):
            if params.get("grant_type") == "fb_exchange_token":
                assert params["fb_exchange_token"] == "short"
                return httpx.Response(200, json={"access_token": "long", "expires_in": 5e6})
            assert "grant_type" not in params and params["client_secret"] == "fb-secret"
            return httpx.Response(200, json={"access_token": "short", "expires_in": 3600})
        if req.url.path.endswith("/me/accounts"):
            assert params["access_token"] == "long"
            return httpx.Response(200, json={"data": [
                {"id": "p1", "name": "Sayfasız", "access_token": "x"},
                {"id": "p2", "name": "Klip Sayfası", "access_token": "PAGE",
                 "instagram_business_account": {"id": "ig9"}},
            ]})  # fmt: skip
        raise AssertionError(req.url)

    oauth, _ = _oauth(tmp_path, handler)
    url = oauth.start("instagram")
    assert url.startswith("https://www.facebook.com/v25.0/dialog/oauth?")
    state = parse_qs(urlparse(url).query)["state"][0]
    assert "code_challenge" not in url
    assert run(oauth.callback("instagram", "c", state)) == "Klip Sayfası"
    tok = run(oauth.access_token("instagram", "Klip Sayfası"))
    assert tok.token == "PAGE" and tok.extra["ig_user_id"] == "ig9"


def test_oauth_needs_secret_key(tmp_path: Path) -> None:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))
    conn = db.open_db(cfg.db_path)
    oauth = OAuthManager(conn, cfg, None, providers(cfg, _secrets()), httpx.AsyncClient())
    with pytest.raises(OAuthError, match="CLIPPER_SECRET_KEY"):
        oauth.start("youtube")
    assert providers(cfg, Secrets()) == {}


# ---- YouTube ----------------------------------------------------------------------------


class FakeYouTube:
    """Resumable upload server: tracks received bytes, can drop or 503 once."""

    def __init__(self, total: int, *, drop_at: int | None = None, fail_503: bool = False) -> None:
        self.total = total
        self.data = bytearray()
        self.drop_at = drop_at
        self.fail_503 = fail_503
        self.inits = 0
        self.queries = 0
        self.meta: dict[str, Any] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            self.inits += 1
            assert req.url.params["uploadType"] == "resumable"
            assert req.headers["x-upload-content-length"] == str(self.total)
            self.meta = json.loads(req.content)
            return httpx.Response(200, headers={"Location": "https://up.example/session/1"})
        assert req.method == "PUT" and req.url.path == "/session/1"
        cr = req.headers["content-range"]
        if cr.startswith("bytes */"):
            self.queries += 1
            return self._progress()
        first, last = (int(x) for x in cr[6:].split("/")[0].split("-"))
        if self.drop_at is not None and first >= self.drop_at:
            self.drop_at = None
            # Half of the chunk arrives (a multiple of 256 KiB would be committed).
            raise httpx.ConnectError("connection reset")
        if self.fail_503 and first > 0:
            self.fail_503 = False
            return httpx.Response(503)
        assert first == len(self.data), "overlapping or skipped bytes"
        self.data.extend(req.content)
        assert last == len(self.data) - 1
        return self._progress()

    def _progress(self) -> httpx.Response:
        if len(self.data) >= self.total:
            return httpx.Response(201, json={"id": "vid123", "snippet": {}})
        if not self.data:
            return httpx.Response(308)
        return httpx.Response(308, headers={"Range": f"bytes=0-{len(self.data) - 1}"})


def _yt(fake: Any) -> YouTubePublisher:
    cfg = PublishConfig(youtube_chunk_mb=0)  # → 256 KiB chunks
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return YouTubePublisher(cfg, client, tokens_for(), sleep=no_sleep)


def test_youtube_chunked_upload_resumes_after_drop(tmp_path: Path) -> None:
    clip = video(tmp_path, 700_000)
    fake = FakeYouTube(clip.size, drop_at=262_144, fail_503=False)
    states: list[dict[str, Any]] = []
    res = run(_yt(fake).upload(clip, "main", {}, lambda s: states.append(dict(s))))
    assert res == PublishResult("published", "vid123", "https://www.youtube.com/shorts/vid123")
    assert bytes(fake.data) == clip.path.read_bytes()
    assert fake.inits == 1 and fake.queries == 1
    assert states[0]["session_uri"] == "https://up.example/session/1"
    meta = fake.meta
    assert meta["snippet"]["categoryId"] == "20" and meta["status"]["privacyStatus"] == "unlisted"
    assert meta["status"]["selfDeclaredMadeForKids"] is False


def test_youtube_503_then_resume_and_saved_session(tmp_path: Path) -> None:
    clip = video(tmp_path, 600_000)
    fake = FakeYouTube(clip.size, fail_503=True)
    res = run(_yt(fake).upload(clip, "main", {}, lambda s: None))
    assert res.remote_id == "vid123" and bytes(fake.data) == clip.path.read_bytes()
    # A crash after the first chunk: the next attempt asks where to continue.
    fake2 = FakeYouTube(clip.size)
    fake2.data.extend(clip.path.read_bytes()[:262_144])
    state = {"session_uri": "https://up.example/session/1"}
    res = run(_yt(fake2).upload(clip, "main", state, lambda s: None))
    assert res.status == "published" and fake2.inits == 0
    assert bytes(fake2.data) == clip.path.read_bytes()


def test_youtube_4xx_is_final(tmp_path: Path) -> None:
    clip = video(tmp_path, 1000)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 400, "message": "invalidTitle"}})

    with pytest.raises(PublishError, match="invalidTitle") as e:
        run(_yt(handler).upload(clip, "main", {}, lambda s: None))
    assert e.value.retry is False


# ---- Instagram --------------------------------------------------------------------------


def test_instagram_resumable_reel(tmp_path: Path) -> None:
    clip = video(tmp_path, 5000)
    log: list[str] = []
    polls = iter(["IN_PROGRESS", "FINISHED"])
    limited = [True]

    def handler(req: httpx.Request) -> httpx.Response:
        p = dict(req.url.params)
        log.append(f"{req.method} {req.url.host}{req.url.path}")
        if req.url.host == "rupload.facebook.com":
            assert req.headers["authorization"] == "OAuth PAGE"
            assert req.headers["offset"] == "0" and req.headers["file_size"] == "5000"
            assert req.content == clip.path.read_bytes()
            return httpx.Response(200, json={"success": True})
        assert p["access_token"] == "PAGE"
        if req.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(
                200, json={"data": [{"quota_usage": 3, "config": {"quota_total": 100}}]}
            )
        if req.url.path.endswith("/ig9/media"):
            if limited[0]:  # one 429 with Retry-After first
                limited[0] = False
                return httpx.Response(429, headers={"Retry-After": "1"})
            assert p["media_type"] == "REELS" and p["upload_type"] == "resumable"
            assert p["caption"] == "Metin #oha" and p["share_to_feed"] == "true"
            return httpx.Response(
                200, json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"}
            )
        if req.url.path.endswith("/c1"):
            return httpx.Response(200, json={"status_code": next(polls)})
        if req.url.path.endswith("/media_publish"):
            assert p["creation_id"] == "c1"
            return httpx.Response(200, json={"id": "m77"})
        if req.url.path.endswith("/m77"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/"})
        raise AssertionError(req.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pub = InstagramPublisher(PublishConfig(), client, tokens_for({"ig_user_id": "ig9"}, "PAGE"),
                             sleep=no_sleep)  # fmt: skip
    state: dict[str, Any] = {}
    res = run(pub.upload(clip, "Klip Sayfası", state, lambda s: None))
    assert res == PublishResult("published", "m77", "https://www.instagram.com/reel/abc/")
    assert sum(1 for x in log if x.endswith("/ig9/media")) == 2
    assert state["uploaded"] is True and state["container_id"] == "c1"


def test_instagram_error_status_fails(tmp_path: Path) -> None:
    clip = video(tmp_path, 100)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        if req.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(
                200, json={"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]}
            )
        raise AssertionError(req.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pub = InstagramPublisher(PublishConfig(), client, tokens_for({"ig_user_id": "ig9"}),
                             sleep=no_sleep)  # fmt: skip
    with pytest.raises(PublishError, match="limiti dolu") as e:
        run(pub.upload(clip, "a", {}, lambda s: None))
    assert e.value.retry is True


# ---- X ----------------------------------------------------------------------------------


def test_x_chunked_upload_status_and_tweet(tmp_path: Path) -> None:
    clip = video(tmp_path, 9 * 1024 * 1024)  # 4 MiB chunks → 3 segments
    received: dict[int, int] = {}
    status = iter([{"state": "in_progress", "check_after_secs": 1}, {"state": "succeeded"}])
    tweet: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer tok"
        path = req.url.path
        if path == "/2/media/upload/initialize":
            body = json.loads(req.content)
            assert body == {"media_type": "video/mp4", "total_bytes": clip.size,
                            "media_category": "tweet_video"}  # fmt: skip
            return httpx.Response(200, json={"data": {"id": "m1", "expires_after_secs": 86400}})
        if path == "/2/media/upload/m1/append":
            ctype = req.headers["content-type"]
            assert ctype.startswith("multipart/form-data")
            text = req.content
            idx = int(text.split(b'name="segment_index"\r\n\r\n')[1].split(b"\r\n")[0])
            received[idx] = len(req.content)
            return httpx.Response(204)
        if path == "/2/media/upload/m1/finalize":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "m1",
                        "processing_info": {"state": "pending", "check_after_secs": 1},
                    }
                },
            )
        if path == "/2/media/upload":
            assert req.url.params["command"] == "STATUS" and req.url.params["media_id"] == "m1"
            return httpx.Response(200, json={"data": {"processing_info": next(status)}})
        if path == "/2/tweets":
            tweet.update(json.loads(req.content))
            return httpx.Response(201, json={"data": {"id": "t9", "text": "X metni"}})
        raise AssertionError(req.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pub = XPublisher(PublishConfig(), client, tokens_for(), sleep=no_sleep)
    state: dict[str, Any] = {}
    res = run(pub.upload(clip, "klipci", state, lambda s: None))
    assert res == PublishResult("published", "t9", "https://x.com/i/status/t9")
    assert sorted(received) == [0, 1, 2]
    assert tweet == {"text": "X metni", "media": {"media_ids": ["m1"]}}
    # Already tweeted → a retry does not post twice.
    res2 = run(pub.upload(clip, "klipci", state, lambda s: None))
    assert res2.remote_id == "t9"


def test_x_resumes_from_saved_segment(tmp_path: Path) -> None:
    clip = video(tmp_path, 9 * 1024 * 1024)
    segs: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/append"):
            segs.append(int(req.content.split(b'name="segment_index"\r\n\r\n')[1][:1]))
            return httpx.Response(200)
        if req.url.path.endswith("/finalize"):
            return httpx.Response(200, json={"data": {"id": "m1"}})
        if req.url.path == "/2/tweets":
            return httpx.Response(201, json={"data": {"id": "t1"}})
        raise AssertionError(req.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pub = XPublisher(PublishConfig(), client, tokens_for(), sleep=no_sleep, clock=lambda: 0.0)
    state = {"media_id": "m1", "next_segment": 2, "finalized": False, "expires_at": 100.0}
    assert run(pub.upload(clip, "a", state, lambda s: None)).remote_id == "t1"
    assert segs == [2]


# ---- TikTok -----------------------------------------------------------------------------


def test_tiktok_chunk_rules() -> None:
    mb = 1024 * 1024
    assert chunk_plan(3 * mb, 10 * mb) == (3 * mb, 1)
    assert chunk_plan(50_000_123, 10_000_000) == (10_000_000, 5)
    assert chunk_ranges(50_000_123, 10_000_000, 5)[-1] == (40_000_000, 50_000_122)
    assert chunk_plan(200 * mb, 100 * mb) == (64 * mb, 3)  # max chunk 64 MB


def test_tiktok_inbox_flow(tmp_path: Path) -> None:
    clip = video(tmp_path, 12 * 1024 * 1024)
    puts: list[str] = []
    status = iter(["PROCESSING_UPLOAD", "SEND_TO_USER_INBOX"])

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            assert req.url.params["upload_token"] == "u"
            puts.append(req.headers["content-range"])
            done = req.headers["content-range"].split("-")[1].startswith(str(clip.size - 1))
            return httpx.Response(201 if done else 206)
        body = json.loads(req.content)
        if req.url.path == "/v2/post/publish/inbox/video/init/":
            src = body["source_info"]
            assert src["source"] == "FILE_UPLOAD" and src["video_size"] == clip.size
            assert src["total_chunk_count"] == 1  # 12 MiB / 10 MiB → 1 chunk (+ remainder)
            return httpx.Response(200, json={"data": {"publish_id": "p1", "upload_url":
                                  "https://up.tiktok.example/x?upload_token=u"},
                                  "error": {"code": "ok"}})  # fmt: skip
        if req.url.path == "/v2/post/publish/status/fetch/":
            assert body == {"publish_id": "p1"}
            return httpx.Response(200, json={"data": {"status": next(status)},
                                             "error": {"code": "ok"}})  # fmt: skip
        raise AssertionError(req.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pub = TikTokPublisher(PublishConfig(), client, tokens_for(), sleep=no_sleep)
    res = run(pub.upload(clip, "a", {}, lambda s: None))
    assert res == PublishResult("inbox", "p1")
    assert puts == [f"bytes 0-{clip.size - 1}/{clip.size}"]


def test_tiktok_direct_needs_audit(tmp_path: Path) -> None:
    clip = video(tmp_path, 100)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no request without audit")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pub = TikTokPublisher(PublishConfig(tiktok_mode="direct"), client, tokens_for())
    with pytest.raises(PublishError, match="audit") as e:
        run(pub.upload(clip, "a", {}, lambda s: None))
    assert e.value.retry is False
    cfg = Config(publish=PublishConfig(tiktok_mode="direct"))
    assert "video.publish" not in providers(cfg, _secrets())["tiktok"].scopes


def test_tiktok_direct_after_audit_uses_creator_info(tmp_path: Path) -> None:
    clip = video(tmp_path, 100)
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            return httpx.Response(201)
        paths.append(req.url.path)
        ok = {"error": {"code": "ok"}}
        if req.url.path.endswith("creator_info/query/"):
            return httpx.Response(200, json={"data": {"privacy_level_options": ["SELF_ONLY"]},
                                             **ok})  # fmt: skip
        if req.url.path == "/v2/post/publish/video/init/":
            assert json.loads(req.content)["post_info"]["privacy_level"] == "SELF_ONLY"
            return httpx.Response(200, json={"data": {"publish_id": "p", "upload_url":
                                  "https://u.example/"}, **ok})  # fmt: skip
        return httpx.Response(
            200,
            json={
                "data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [55]},
                **ok,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = PublishConfig(tiktok_mode="direct", tiktok_audit_passed=True)
    res = run(TikTokPublisher(cfg, client, tokens_for(), sleep=no_sleep)
              .upload(clip, "a", {}, lambda s: None))  # fmt: skip
    assert res.status == "published" and res.url == "https://www.tiktok.com/video/55"
    assert "/v2/post/publish/inbox/video/init/" not in paths


# ---- scheduler / auto-publish / publish jobs --------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Config]]:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))
    conn = db.open_db(cfg.db_path, check_same_thread=False)
    conn.execute(
        "INSERT INTO streamers (display_name, platform, channel, permission_ok, auto_publish,"
        " publish_targets, created_at) VALUES ('Ali', 'twitch', 'ali', 1, 1, ?, 'x'),"
        " ('Veli', 'twitch', 'veli', 0, 1, ?, 'x')",
        (json.dumps(["youtube", "x"]), json.dumps(["youtube"])),
    )
    final = tmp_path / "final.mp4"
    final.write_bytes(b"\x00" * 100)
    for sid in (1, 2):
        conn.execute(
            "INSERT INTO sessions (streamer_id, source, started_at, dir, status)"
            " VALUES (?, 'vod', 'x', '.', 'ended')",
            (sid,),
        )
        conn.execute(
            "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score,"
            " features, status, created_at) VALUES (?, 1, 0, 2, 1, '{}', 'rendered', 'x')",
            (sid,),
        )
        conn.execute(
            "INSERT INTO clips (candidate_id, llm_json, llm_score, title, final_path,"
            " created_at) VALUES (?, ?, 90, 'Oha anı', ?, 'x')",
            (sid, json.dumps({"judge": JUDGE}), str(final)),
        )
    yield conn, cfg
    conn.close()


def accounts(platform: str) -> list[str]:
    return {"youtube": ["kanal"], "x": ["klipci"]}.get(platform, [])


def test_plan_refuses_without_permission(world: tuple[sqlite3.Connection, Config]) -> None:
    conn, cfg = world
    with pytest.raises(scheduler.PermissionDenied):
        scheduler.plan(conn, cfg, 2, ["youtube"], accounts)
    assert conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0
    assert scheduler.maybe_auto_publish(conn, cfg, 2, accounts) is False


def test_plan_gaps_caps_and_missing_account(world: tuple[sqlite3.Connection, Config]) -> None:
    conn, cfg = world
    now = time.mktime((2026, 11, 18, 10, 0, 0, 0, 0, -1))
    ids = scheduler.plan(conn, cfg, 1, ["x", "youtube", "tiktok"], accounts, now=now)
    rows = {r["platform"]: r for r in conn.execute("SELECT * FROM publications")}
    assert len(ids) == 2
    assert rows["youtube"]["run_at"] == now  # YouTube first
    assert rows["x"]["run_at"] == now + 15 * 60  # then the cross-platform gap
    assert rows["tiktok"]["status"] == "failed" and "hesap" in rows["tiktok"]["error"]
    assert rows["tiktok"]["mode"] == "inbox"
    # Idempotent per (clip, platform).
    assert scheduler.plan(conn, cfg, 1, ["youtube"], accounts, now=now) == []
    job = conn.execute("SELECT * FROM jobs WHERE type = 'publish' ORDER BY run_after").fetchone()
    assert job["pool"] == "pub" and job["run_after"] == now
    # Gap: next YouTube slot is ≥ 90 min after the first; cap: 8 a day, then next day.
    slot = scheduler.next_slot(conn, cfg, "youtube", now + 60)
    assert slot == now + 90 * 60
    for i in range(1, 8):
        conn.execute(
            "INSERT INTO publications (clip_id, platform, account, status, run_at)"
            " VALUES (?, 'youtube', ?, 'published', ?)",
            (1, f"a{i}", now + i * 90 * 60),
        )
    late = scheduler.next_slot(conn, cfg, "youtube", now + 60)
    assert time.localtime(late).tm_mday == 19 and time.localtime(late).tm_hour == 0


def test_auto_publish_rule(world: tuple[sqlite3.Connection, Config]) -> None:
    conn, cfg = world
    assert scheduler.maybe_auto_publish(conn, cfg, 1, accounts) is True
    assert conn.execute("SELECT review FROM clips WHERE id = 1").fetchone()[0] == "approved"
    plats = {r[0] for r in conn.execute("SELECT platform FROM publications")}
    assert plats == {"youtube", "x"}
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    # Dirty flags or a low score → no auto approval.
    owner = dict(scheduler.clip_owner(conn, 1))
    owner["llm_json"] = json.dumps(
        {"judge": {**JUDGE, "flags": {**JUDGE["flags"], "music_risk": True}}}
    )
    assert scheduler.auto_publish_ok(cfg, owner) is False  # type: ignore[arg-type]
    owner["llm_json"] = json.dumps({"judge": JUDGE})
    owner["llm_score"] = 84
    assert scheduler.auto_publish_ok(cfg, owner) is False  # type: ignore[arg-type]
    owner["llm_json"] = json.dumps({"judge": {**JUDGE, "flags": {}}})
    owner["llm_score"] = 99
    assert scheduler.auto_publish_ok(cfg, owner) is False  # type: ignore[arg-type]


class FakePublisher:
    platform = "youtube"

    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[ClipForPublish] = []

    async def upload(
        self, clip: ClipForPublish, account: str, state: dict[str, Any], save: Any
    ) -> PublishResult:
        self.calls.append(clip)
        state["seen"] = True
        save(state)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r  # type: ignore[no-any-return]

    async def check_status(
        self, account: str, remote_id: str, state: dict[str, Any]
    ) -> PublishResult:
        r = self.results.pop(0)
        assert isinstance(r, PublishResult)
        return r


def _claim(conn: sqlite3.Connection) -> jobs.Job:
    job = jobs.claim(conn, "pub", clock=lambda: 1e12)
    assert job is not None
    return job


def test_publish_job_success_and_status(world: tuple[sqlite3.Connection, Config]) -> None:
    conn, cfg = world
    scheduler.plan(conn, cfg, 1, ["youtube"], accounts, now=1_800_000_000.0)
    fake = FakePublisher(
        [PublishResult("processing", "v1"), PublishResult("published", "v1", "https://y/v1")]
    )
    pubs = Publishing(conn, cfg, {"youtube": fake})  # type: ignore[dict-item]
    run(pubs.handle(_claim(conn)))
    row = conn.execute("SELECT * FROM publications").fetchone()
    assert row["status"] == "processing" and json.loads(row["state"]) == {"seen": True}
    assert fake.calls[0].title == "YT başlık" and "#oha" in fake.calls[0].description
    run(pubs.handle(_claim(conn)))  # publish_status job
    row = conn.execute("SELECT * FROM publications").fetchone()
    assert row["status"] == "published" and row["url"] == "https://y/v1"


def test_publish_job_errors_and_permission_guard(
    world: tuple[sqlite3.Connection, Config],
) -> None:
    conn, cfg = world
    scheduler.plan(conn, cfg, 1, ["youtube"], accounts, now=1_800_000_000.0)
    fake = FakePublisher([PublishError("HTTP 503"), PublishError("format", retry=False)])
    pubs = Publishing(conn, cfg, {"youtube": fake})  # type: ignore[dict-item]
    with pytest.raises(PublishError):
        run(pubs.handle(_claim(conn)))
    assert conn.execute("SELECT status FROM publications").fetchone()[0] == "retrying"
    conn.execute("UPDATE jobs SET status = 'queued'")
    run(pubs.handle(_claim(conn)))
    row = conn.execute("SELECT status, error FROM publications").fetchone()
    assert tuple(row) == ("failed", "format")
    # Permission revoked after scheduling → never uploaded.
    conn.execute("UPDATE publications SET status = 'scheduled'")
    conn.execute("UPDATE streamers SET permission_ok = 0 WHERE id = 1")
    conn.execute("UPDATE jobs SET status = 'queued'")
    fake.results = [PublishResult("published", "zzz")]
    run(pubs.handle(_claim(conn)))
    row = conn.execute("SELECT status, error FROM publications").fetchone()
    assert row["status"] == "failed" and "izni" in row["error"] and len(fake.results) == 1


def test_clip_texts_prefer_user_title(world: tuple[sqlite3.Connection, Config]) -> None:
    conn, _ = world
    row = conn.execute("SELECT * FROM clips WHERE id = 1").fetchone()
    c = clip_for(row, "instagram")
    assert c.caption.startswith("IG metni") and "#yayın" in c.caption and c.x_text == "X metni"
    llm = {"judge": JUDGE, "original": {"title": "Oha anı"}}
    conn.execute("UPDATE clips SET title = 'Benim başlığım', llm_json = ? WHERE id = 1",
                 (json.dumps(llm),))  # fmt: skip
    row = conn.execute("SELECT * FROM clips WHERE id = 1").fetchone()
    assert clip_for(row, "youtube").title == "Benim başlığım"


# ---- panel ------------------------------------------------------------------------------


def test_panel_publishing_endpoints(world: tuple[sqlite3.Connection, Config]) -> None:
    conn, cfg = world
    secrets = _secrets()
    assert secrets.clipper_secret_key is not None

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "A", "refresh_token": "R",
                                             "expires_in": 3600})  # fmt: skip
        raise AssertionError(req.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    box = SecretBox(secrets.clipper_secret_key.get_secret_value())
    oauth = OAuthManager(conn, cfg, box, providers(cfg, secrets), client)
    kit = PublishKit(oauth, {"youtube": FakePublisher([])})  # type: ignore[dict-item]
    app = panel_client(create_app(PanelDeps(conn, cfg, secrets, publish=kit)))

    acc = app.get("/api/accounts").json()
    assert acc["secret_key"] is True and all(p["configured"] for p in acc["platforms"])
    start = app.post("/api/auth/youtube/start", json={"account": "kanal"}).json()
    q = parse_qs(urlparse(start["url"]).query)
    assert q["access_type"] == ["offline"]
    page = app.get("/callback/youtube", params={"code": "c", "state": q["state"][0]})
    assert page.status_code == 200 and "bağlandı" in page.text
    assert app.get("/callback/youtube", params={"error": "access_denied"}).status_code == 400
    assert [a["account"] for a in app.get("/api/accounts").json()["accounts"]] == ["kanal"]
    assert b'"A"' not in conn.execute("SELECT data_enc FROM oauth_tokens").fetchone()[0]

    # Clip 2's streamer has no permission → platforms are refused.
    conn.execute("UPDATE clips SET review = 'pending'")
    r = app.post("/api/clips/2/approve", json={"platforms": ["youtube"]})
    assert r.status_code == 403
    r = app.post("/api/clips/1/approve", json={"platforms": ["youtube", "x"]}).json()
    assert r["planned"] == 1  # x has no connected account (FakePublisher only for youtube)
    pubs = {p["platform"]: p for p in app.get("/api/publications").json()}
    assert pubs["youtube"]["status"] == "scheduled" and pubs["x"]["status"] == "failed"
    assert app.post(f"/api/publications/{pubs['x']['id']}/retry").status_code == 400
    conn.execute("UPDATE publications SET status = 'failed' WHERE platform = 'youtube'")
    assert app.post(f"/api/publications/{pubs['youtube']['id']}/retry").json() == {"ok": True}
    assert app.delete("/api/accounts/youtube/kanal").json() == {"ok": True}
    assert app.delete("/api/accounts/youtube/kanal").status_code == 404
