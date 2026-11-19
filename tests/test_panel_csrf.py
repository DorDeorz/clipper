"""Audit B-02: the panel must not accept state-changing requests from other sites
(CSRF via text/plain "simple" requests) or from foreign Host names (DNS rebinding)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from clipper import db
from clipper.config import Config, PanelConfig, PathsConfig, Secrets
from clipper.panel.app import STATIC_DIR, PanelDeps, create_app
from tests.panel_client import BASE_URL, panel_client

pytestmark = pytest.mark.security

STREAMER = {"display_name": "evil", "platform": "twitch", "channel": "x", "permission_ok": 1}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.open_db(tmp_path / "data" / "clipper.db", check_same_thread=False)
    yield c
    c.close()


def _app(conn: sqlite3.Connection, tmp_path: Path, **panel: object) -> FastAPI:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"), panel=PanelConfig(**panel))
    secrets = Secrets(panel_password=SecretStr("gizli")) if panel.get("host") else Secrets()
    return create_app(PanelDeps(conn, cfg, secrets))


def _streamers(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM streamers").fetchone()[0])


def test_cross_site_text_plain_post_is_refused(conn: sqlite3.Connection, tmp_path: Path) -> None:
    # What a foreign page can send without a CORS preflight.
    c = TestClient(_app(conn, tmp_path), base_url=BASE_URL)
    r = c.post(
        "/api/streamers",
        content=json.dumps(STREAMER),
        headers={"Content-Type": "text/plain", "Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert _streamers(conn) == 0


def test_missing_csrf_token_is_refused(conn: sqlite3.Connection, tmp_path: Path) -> None:
    c = TestClient(_app(conn, tmp_path), base_url=BASE_URL)
    assert c.post("/api/streamers", json=STREAMER).status_code == 403
    assert c.post("/api/system/shutdown").status_code == 403  # no body, still guarded
    assert (
        c.post("/api/streamers", json=STREAMER, headers={"X-CSRF-Token": "nope"}).status_code == 403
    )
    assert _streamers(conn) == 0


def test_foreign_origin_with_token_is_refused(conn: sqlite3.Connection, tmp_path: Path) -> None:
    c = panel_client(_app(conn, tmp_path))
    r = c.post("/api/streamers", json=STREAMER, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert _streamers(conn) == 0


def test_non_json_body_is_refused(conn: sqlite3.Connection, tmp_path: Path) -> None:
    c = panel_client(_app(conn, tmp_path))
    r = c.post(
        "/api/streamers", content=json.dumps(STREAMER), headers={"Content-Type": "text/plain"}
    )
    assert r.status_code == 415
    assert _streamers(conn) == 0


def test_foreign_host_is_refused_on_loopback(conn: sqlite3.Connection, tmp_path: Path) -> None:
    # DNS rebinding: evil.example resolves to 127.0.0.1, the browser sends its own Host.
    c = TestClient(_app(conn, tmp_path), base_url="http://evil.example:8765")
    assert c.get("/api/status").status_code == 403
    assert c.get("/api/csrf").status_code == 403


def test_same_origin_browser_request_works(conn: sqlite3.Connection, tmp_path: Path) -> None:
    app = _app(conn, tmp_path)
    plain = TestClient(app, base_url=BASE_URL)
    token = plain.get("/api/csrf").json()["token"]
    assert token == app.state.csrf_token
    r = plain.post(
        "/api/streamers", json=STREAMER, headers={"X-CSRF-Token": token, "Origin": BASE_URL}
    )
    assert r.status_code == 201
    assert _streamers(conn) == 1
    for host in ("http://localhost:8765", "http://[::1]:8765"):
        assert TestClient(app, base_url=host).get("/api/status").status_code == 200


def test_exposed_panel_checks_origin_against_host(conn: sqlite3.Connection, tmp_path: Path) -> None:
    import base64

    app = _app(conn, tmp_path, host="0.0.0.0")
    auth = {"Authorization": "Basic " + base64.b64encode(b"panel:gizli").decode()}
    c = TestClient(app, base_url="http://192.168.1.20:8765", headers=auth)
    c.headers["X-CSRF-Token"] = app.state.csrf_token
    assert c.get("/api/status").status_code == 200  # LAN Host allowed when exposed
    bad = c.post("/api/streamers", json=STREAMER, headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403
    ok = c.post("/api/streamers", json=STREAMER, headers={"Origin": "http://192.168.1.20:8765"})
    assert ok.status_code == 201


def test_panel_js_sends_csrf_header() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "X-CSRF-Token" in js and "/api/csrf" in js
