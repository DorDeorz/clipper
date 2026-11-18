"""User-friendly panel: setup screen (.env), VOD import, system actions, shortcut."""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clipper import autostart, db
from clipper.config import Config, PathsConfig, load_secrets
from clipper.panel import app as panel_app
from clipper.panel import setup
from clipper.panel.app import PanelDeps, create_app
from clipper.panel.vodjobs import VodError, VodTask, VodTasks, detect_source, parse_ref
from clipper.runner import already_running
from clipper.util.crypto import generate_key

ANTH = "sk-ant-test-0123456789"


def test_update_env_keeps_other_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# yorum\nANTHROPIC_API_KEY=\nOTHER=1\n", encoding="utf-8")
    changed = setup.update_env(env, {"ANTHROPIC_API_KEY": ANTH, "X_CLIENT_ID": " abc ",
                                     "META_APP_ID": ""})  # fmt: skip
    assert changed == ["ANTHROPIC_API_KEY", "X_CLIENT_ID"]
    assert env.read_text(encoding="utf-8") == (
        f"# yorum\nANTHROPIC_API_KEY={ANTH}\nOTHER=1\nX_CLIENT_ID=abc\n"
    )
    for bad in ({"NOPE": "1"}, {"X_CLIENT_ID": "a\nB=2"}, {"ANTHROPIC_API_KEY": "abc"},
                {"CLIPPER_SECRET_KEY": "not-a-key"}):  # fmt: skip
        with pytest.raises((setup.SetupError, ValueError)):
            setup.update_env(env, bad)
    assert setup.update_env(tmp_path / "new" / ".env", {"X_CLIENT_ID": "z"}) == ["X_CLIENT_ID"]


def test_secret_key_generation_rules(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert setup.generate_secret_key(env, load_secrets(env), 0) is True
    sec = load_secrets(env)
    assert sec.clipper_secret_key is not None
    assert setup.generate_secret_key(env, sec, 0) is False  # never replaced
    with pytest.raises(setup.SetupError):
        setup.generate_secret_key(env, sec, 2)


def _client(tmp_path: Path, **kw: Any) -> tuple[TestClient, PanelDeps]:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))
    conn = db.open_db(cfg.db_path, check_same_thread=False)
    env = tmp_path / ".env"
    deps = PanelDeps(conn, cfg, load_secrets(env), env_path=env, **kw)
    return TestClient(create_app(deps)), deps


def test_setup_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    actions: list[str] = []
    client, deps = _client(tmp_path, system=actions.append)
    d = client.get("/api/setup").json()
    assert d["first_run"] is True and d["can_restart"] is True and d["restart_needed"] is False
    assert {k["name"] for k in d["env"]} >= {"ANTHROPIC_API_KEY", "CLIPPER_SECRET_KEY"}
    r = client.post("/api/setup/env", json={"values": {"ANTHROPIC_API_KEY": ANTH}})
    assert r.json() == {"ok": True, "changed": ["ANTHROPIC_API_KEY"]}
    assert deps.secrets.anthropic_api_key is not None and deps.restart_needed
    assert client.post("/api/setup/secret-key").json()["created"] is True
    d = client.get("/api/setup").json()
    assert d["first_run"] is False and ANTH not in str(d)  # values never sent back
    assert client.post("/api/setup/env", json={"values": {"ANTHROPIC_API_KEY": "x"}}
                       ).status_code == 400  # fmt: skip
    assert client.post("/api/setup/env", json={"values": ["x"]}).status_code == 400

    async def fake_checks(cfg: Config, secrets: Any) -> list[dict[str, str]]:
        return [{"name": "ffmpeg", "status": "ok", "detail": "9.0"}]

    monkeypatch.setattr(setup, "checks", fake_checks)
    assert client.get("/api/setup/checks").json()[0]["status"] == "ok"
    assert client.post("/api/system/restart").json() == {"ok": True}
    assert client.post("/api/system/bogus").status_code == 404
    assert actions == ["restart"]


def test_setup_secret_key_updates_publish_box(tmp_path: Path) -> None:
    from clipper.publish.wiring import build

    client0, deps = _client(tmp_path)
    import httpx

    deps.publish = build(deps.conn, deps.cfg, deps.secrets, httpx.AsyncClient())
    assert deps.publish.oauth.box is None
    client0.post("/api/setup/secret-key")
    assert deps.publish.oauth.box is not None


def test_system_actions_unavailable_without_hook(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.post("/api/system/shutdown").status_code == 503
    assert client.get("/api/vod").status_code == 503


# ---- VOD import -------------------------------------------------------------------------


def test_detect_and_parse_refs() -> None:
    u = "0b8a5e3c-1d2f-4a5b-9c8d-7e6f5a4b3c2d"
    assert detect_source(f"https://kick.com/neon/videos/{u}") == "kick"
    assert parse_ref("kick", f"https://kick.com/neon/videos/{u.upper()}") == u
    assert detect_source("https://www.twitch.tv/videos/2345678901") == "twitch"
    assert parse_ref("twitch", "https://www.twitch.tv/videos/2345678901?t=1h") == "2345678901"
    assert parse_ref("twitch", "v123") == "123"
    for link in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5", "https://youtu.be/dQw4w9WgXcQ",
                 "https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"):  # fmt: skip
        assert parse_ref("youtube", link) == "dQw4w9WgXcQ"
    assert detect_source("https://youtu.be/dQw4w9WgXcQ") == "youtube"
    assert detect_source("merhaba dünya") is None
    with pytest.raises(VodError):
        parse_ref("kick", "neon")
    with pytest.raises(VodError):
        parse_ref("twitch", "https://twitch.tv/neon")


def _wait(tasks: VodTasks, tid: int) -> VodTask:
    for _ in range(200):
        t = tasks.tasks[tid]
        if t.status != "running":
            return t
        time.sleep(0.01)
    raise AssertionError("task did not finish")


def test_vod_tasks_background(tmp_path: Path) -> None:
    gate: list[str] = []

    async def runner(task: VodTask) -> None:
        task.say("Video indiriliyor...")
        task.say("  segment 1/3 (%33)")
        task.say("  segment 3/3 (%100)")
        if task.ref == "999":
            raise RuntimeError("ağ hatası")
        task.session_id, task.candidates = 7, 4
        gate.append(task.ref)

    tasks = VodTasks(Config(paths=PathsConfig(data_dir=tmp_path)), runner=runner)
    t = tasks.start("twitch", "https://www.twitch.tv/videos/123")
    done = _wait(tasks, t.id)
    assert done.status == "done" and done.session_id == 7
    assert done.lines == ["Başladı", "Video indiriliyor...", "  segment 3/3 (%100)"]
    bad = _wait(tasks, tasks.start("twitch", "999").id)
    assert bad.status == "failed" and bad.error == "ağ hatası"
    with pytest.raises(VodError, match="dosya bulunamadı"):
        tasks.start("file", str(tmp_path / "yok.mp4"), streamer="a")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(VodError, match="yayıncı"):
        tasks.start("file", str(video))
    assert [x["ref"] for x in tasks.list()] == ["999", "123"]


def test_vod_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner(task: VodTask) -> None:
        task.say("tamam")

    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))
    client, deps = _client(tmp_path, vod=VodTasks(cfg, runner=runner))
    r = client.post("/api/vod", json={"text": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 200 and r.json()["source"] == "youtube"
    assert client.post("/api/vod", json={"text": "ne bu"}).status_code == 400
    assert client.post("/api/vod", json={"text": ""}).status_code == 400
    assert deps.vod is not None
    _wait(deps.vod, r.json()["id"])
    assert client.get("/api/vod").json()[0]["status"] == "done"

    async def fake_kick(cfg: Config, slug: str) -> list[dict[str, Any]]:
        if slug == "yok":
            raise RuntimeError("403 Cloudflare")
        return [{"uuid": "u1", "title": "t", "duration": 60.0, "start_time": "", "category": ""}]

    monkeypatch.setattr(panel_app, "kick_vods", fake_kick)
    assert client.get("/api/kick-vods", params={"channel": "neon"}).json()[0]["uuid"] == "u1"
    r = client.get("/api/kick-vods", params={"channel": "yok"})
    assert r.status_code == 502 and "Cloudflare" in r.json()["error"]


# ---- shortcut / single instance ---------------------------------------------------------


def test_shortcut_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = autostart.shortcut_plan(tmp_path, Path("config.yaml"), Path(".env"), home=tmp_path)
    if sys.platform != "win32":
        ((target, body),) = p.files.items()
        assert target.name == "clipper.desktop" and "Terminal=false" in body
        assert " run" in body and "--config" in body
        autostart.install(p)
        assert target.is_file()
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    w = autostart.shortcut_plan(Path("C:/Users/o'g/clipper"), Path("config.yaml"), Path(".env"))
    cmd = w.commands[0]
    assert cmd[0] == "powershell" and "Clipper.lnk" in cmd[-1] and "o''g" in cmd[-1]
    assert "GetFolderPath('Desktop')" in cmd[-1] and w.files == {}


def test_already_running() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert already_running("127.0.0.1", port) is True
    finally:
        srv.close()
    assert already_running("127.0.0.1", port) is False


def test_restart_spawns_new_process(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipper import __main__ as cli

    calls: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda args, **kw: calls.append(args))
    cli._restart(["--config", "c.yaml", "run"])
    assert calls[0][1:] == ["-m", "clipper", "--config", "c.yaml", "run", "--no-browser"]
    assert generate_key()  # keygen still available


def test_no_console_window_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipper.util import proc

    monkeypatch.setattr(proc.sys, "platform", "linux")
    assert proc.no_window() == {}
    monkeypatch.setattr(proc.sys, "platform", "win32")
    assert proc.no_window() == {"creationflags": 0x08000000}
