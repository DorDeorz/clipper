"""Faz 8: learned score model, retention, autostart, panel learn endpoints, settings."""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from clipper import db, settings
from clipper.__main__ import main
from clipper.autostart import install, systemd_plan, windows_plan
from clipper.config import Config, LearnConfig, PathsConfig, RetentionConfig, Secrets
from clipper.learn import service
from clipper.learn.model import LogReg, fit, precision_at_k, roc_auc
from clipper.panel.app import PanelDeps, create_app
from clipper.retention import cleanup
from clipper.stages import Stages
from clipper.util.timeutil import to_iso
from tests.panel_client import panel_client

# ---- model ------------------------------------------------------------------------------


def test_logreg_fits_and_metrics() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=(400, 3))
    y = (x[:, 0] * 2 - x[:, 1] + rng.normal(scale=0.5, size=400) > 0).astype(float)
    m = fit(x, y, ["a", "b", "c"], l2=0.1)
    auc = roc_auc(y, m.predict(x))
    assert auc is not None and auc > 0.9
    raw = m.raw_weights()
    assert raw["a"] > 0 > raw["b"] and abs(raw["c"]) < abs(raw["b"])
    assert LogReg.from_json(m.to_json()).predict(x[:3]).tolist() == m.predict(x[:3]).tolist()
    assert roc_auc(np.array([1.0, 0.0]), np.array([0.5, 0.5])) == 0.5
    assert roc_auc(np.array([1.0, 1.0]), np.array([0.1, 0.2])) is None
    assert precision_at_k(np.array([1.0, 0.0, 1.0]), np.array([0.9, 0.8, 0.1]), 2) == 0.5


def test_prior_shrinkage_pulls_toward_global() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(size=(300, 2))
    y = (x[:, 0] > 0).astype(float)
    glob = fit(x, y, ["a", "b"])
    # A tiny streamer set that says the opposite.
    xs, ys = x[:12], 1.0 - y[:12]
    free = fit(xs, ys, ["a", "b"], prior=glob, prior_strength=0.0)
    tied = fit(xs, ys, ["a", "b"], prior=glob, prior_strength=1000.0)
    assert free.w[0] < 0 < tied.w[0]
    assert abs(tied.w[0] - glob.w[0]) < abs(free.w[0] - glob.w[0])


# ---- dataset / learn / apply / gate -----------------------------------------------------


def _snap(rate: float, judge: int, sources: list[str]) -> dict[str, object]:
    return {
        "det_score": 3 + rate,
        "sources": sources,
        "content_score": None,
        "window": [0, 60],
        "features": {"z": {"rate": rate, "uniq": random.random(), "emote": 0.5, "audio": 1.0}},
        "prefilter_score": 60,
        "judge_score": judge,
    }


@pytest.fixture
def learn_world(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Config]]:
    random.seed(3)
    cfg = Config(
        paths=PathsConfig(data_dir=tmp_path / "data"),
        learn=LearnConfig(min_decisions=200, min_streamer_decisions=100),
    )
    conn = db.open_db(cfg.db_path, check_same_thread=False)
    conn.execute(
        "INSERT INTO streamers (display_name, platform, channel, permission_ok, created_at,"
        " detector_overrides) VALUES ('Ali', 'twitch', 'ali', 1, 'x', '{\"lag_seconds\": 12}'),"
        " ('Veli', 'kick', 'veli', 1, 'x', NULL)"
    )
    for sid in (1, 2):
        conn.execute(
            "INSERT INTO sessions (streamer_id, source, started_at, dir, status)"
            " VALUES (?, 'vod', 'x', '.', 'ended')",
            (sid,),
        )
    for i in range(240):
        sid = 1 if i < 160 else 2
        rate = random.uniform(0, 6)
        good = rate + random.gauss(0, 1) > 3
        judge = int(min(99, max(10, 50 + (rate - 3) * 12 + random.gauss(0, 8))))
        cid = conn.execute(
            "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score,"
            " features, status, created_at) VALUES (?, 1, 0, 60, 3, '{}', 'rendered', 'x')",
            (sid,),
        ).lastrowid
        kid = conn.execute(
            "INSERT INTO clips (candidate_id, created_at) VALUES (?, 'x')", (cid,)
        ).lastrowid
        conn.execute(
            "INSERT INTO feedback (clip_id, decision, features_snapshot, created_at)"
            " VALUES (?, ?, ?, ?)",
            (kid, "approved" if good else "rejected",
             json.dumps(_snap(rate, judge, ["reactive"])), 1000.0 + i),
        )  # fmt: skip
    yield conn, cfg
    conn.close()


def test_learn_needs_enough_decisions(learn_world: tuple[sqlite3.Connection, Config]) -> None:
    conn, cfg = learn_world
    strict = cfg.model_copy(update={"learn": LearnConfig(min_decisions=500)})
    with pytest.raises(service.NotEnoughData, match="yetersiz veri"):
        service.train_all(conn, strict)


def test_learn_report_suggestions_versions(
    learn_world: tuple[sqlite3.Connection, Config],
) -> None:
    conn, cfg = learn_world
    v1, res = service.learn(conn, cfg)
    assert v1 == 1 and res["decisions"] == 240
    g = res["global"]
    assert g["report"]["pre"]["auc"] > 0.8 and g["report"]["test"] > 0
    w = g["suggest"]["weights"]
    assert w["rate"] == max(w.values())
    assert abs(sum(w.values()) - sum(g["current"]["weights"].values())) < 0.01
    assert "1" in res["streamers"] and "2" not in res["streamers"]  # 160 vs 80 decisions
    v2, _ = service.learn(conn, cfg)
    assert v2 == 2 and service.Store.at(cfg).versions == [1, 2]
    s = service.summaries(conn, cfg)
    assert [v["version"] for v in s["versions"]] == [2, 1] and s["applied"] is None


def test_apply_undo_and_gate(learn_world: tuple[sqlite3.Connection, Config]) -> None:
    conn, cfg = learn_world
    v, res = service.learn(conn, cfg)
    service.apply(conn, cfg, v)
    over = json.loads(conn.execute("SELECT detector_overrides FROM streamers WHERE id = 1")
                      .fetchone()[0])  # fmt: skip
    assert over["lag_seconds"] == 12  # existing override kept
    assert over["weights"]["rate"] == res["streamers"]["1"]["suggest"]["weights"]["rate"]
    over2 = json.loads(conn.execute("SELECT detector_overrides FROM streamers WHERE id = 2")
                       .fetchone()[0])  # fmt: skip
    assert over2["threshold"] == res["global"]["suggest"]["threshold"]
    assert settings.get(conn, settings.MIN_KEEP_SCORE) == res["global"]["suggest"]["min_keep_score"]
    assert service.summaries(conn, cfg)["applied"] == v

    # Gate: an obviously bad candidate never reaches the LLM.
    cid = conn.execute(
        "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score, features,"
        " status, created_at) VALUES (2, 1, 0, 60, 0.1, ?, 'transcribed', 'x')",
        (json.dumps({"z": {"rate": -8, "uniq": 0, "emote": 0, "audio": 0}}),),
    ).lastrowid
    conn.execute("INSERT INTO clips (candidate_id, created_at) VALUES (?, 'x')", (cid,))
    stages = Stages(conn, cfg)
    cand = stages._candidate(int(cid or 0))
    clip = stages._clip(int(cid or 0))
    assert clip is not None
    assert stages._learned_gate(int(cid or 0), cand, clip, json.loads(cand["features"]))
    status = conn.execute("SELECT status FROM candidates WHERE id = ?", (cid,)).fetchone()[0]
    assert status == "rejected"

    assert service.undo(conn) == v
    raw = conn.execute("SELECT detector_overrides FROM streamers ORDER BY id").fetchall()
    assert [r[0] for r in raw] == ['{"lag_seconds": 12}', None]
    assert settings.get(conn, settings.MIN_KEEP_SCORE) is None
    assert service.undo(conn) is None
    assert service.gate_model(conn, cfg, 1) is None


def test_panel_learn_endpoints(learn_world: tuple[sqlite3.Connection, Config]) -> None:
    conn, cfg = learn_world
    app = panel_client(create_app(PanelDeps(conn, cfg, Secrets())))
    d = app.get("/api/learn").json()
    assert d["versions"] == [] and d["decisions"] == 240
    assert app.post("/api/learn/1/apply").status_code == 404
    assert app.post("/api/learn/undo").status_code == 400
    service.learn(conn, cfg)
    assert app.post("/api/learn/1/apply").json() == {"ok": True}
    assert app.get("/api/learn").json()["applied"] == 1
    assert app.post("/api/learn/undo").json() == {"ok": True, "version": 1}
    assert "percent" in app.get("/api/status").json()["disk"]


def test_cli_learn(learn_world: tuple[sqlite3.Connection, Config], tmp_path: Path,
                   capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    _, cfg = learn_world
    p = tmp_path / "config.yaml"
    p.write_text(f"paths:\n  data_dir: {cfg.paths.data_dir.as_posix()}\n", encoding="utf-8")
    env = tmp_path / ".env"
    assert main(["--config", str(p), "--env", str(env), "learn", "--streamer", "ali"]) == 0
    out = capsys.readouterr().out
    assert "Model v1" in out and "otomatik uygulanmadı" in out


# ---- retention --------------------------------------------------------------------------


def test_retention(tmp_path: Path) -> None:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"),
                 retention=RetentionConfig(raw_days=7, final_days=30))  # fmt: skip
    conn = db.open_db(cfg.db_path)
    now = 2_000_000_000.0
    conn.execute(
        "INSERT INTO streamers (display_name, platform, channel, created_at)"
        " VALUES ('A', 'twitch', 'a', 'x')"
    )
    files: dict[str, Path] = {}
    for name in ("seg_old", "seg_busy", "seg_recent", "raw_old", "final_old", "final_pending",
                 "raw_new", "thumb_old"):  # fmt: skip
        files[name] = tmp_path / f"{name}.bin"
        files[name].write_bytes(b"x" * 1000)
    ended = [to_iso(now - 3600), to_iso(now - 3600), to_iso(now - 60)]
    for i, seg in enumerate(("seg_old", "seg_busy", "seg_recent"), start=1):
        conn.execute(
            "INSERT INTO sessions (streamer_id, source, started_at, ended_at, dir, status)"
            " VALUES (1, 'live', 'x', ?, '.', 'ended')",
            (ended[i - 1],),
        )
        conn.execute(
            "INSERT INTO segments (session_id, seq, path, pdt_start, duration)"
            " VALUES (?, 1, ?, 0, 2)",
            (i, str(files[seg])),
        )
    # Session 2 still has a cut waiting.
    conn.execute(
        "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score, features,"
        " status, created_at) VALUES (2, 1, 0, 1, 1, '{}', 'new', 'x')"
    )
    conn.execute(
        "INSERT INTO jobs (type, pool, payload, run_after, created_at)"
        " VALUES ('cut', 'cpu', '{\"candidate_id\": 1}', 0, 0)"
    )
    old, new = to_iso(now - 40 * 86400), to_iso(now - 86400)
    conn.execute(
        "INSERT INTO clips (candidate_id, raw_path, final_path, thumb_path, created_at)"
        " VALUES (1, ?, ?, ?, ?)",
        (str(files["raw_old"]), str(files["final_old"]), str(files["thumb_old"]), old),
    )
    conn.execute(
        "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score, features,"
        " status, created_at) VALUES (1, 1, 0, 1, 1, '{}', 'rendered', 'x'),"
        " (1, 1, 0, 1, 1, '{}', 'rendered', 'x')"
    )
    conn.execute(
        "INSERT INTO clips (candidate_id, final_path, created_at) VALUES (2, ?, ?)",
        (str(files["final_pending"]), old),
    )
    conn.execute(
        "INSERT INTO clips (candidate_id, raw_path, created_at) VALUES (3, ?, ?)",
        (str(files["raw_new"]), new),
    )
    conn.execute(
        "INSERT INTO publications (clip_id, platform, account, status) VALUES (2, 'x', 'a',"
        " 'scheduled')"
    )

    def low(_p: Path) -> tuple[int, int]:
        return 10, 100

    dry = cleanup(conn, cfg, now=now, dry_run=True, usage=low)
    assert dry.files == 3 and all(p.exists() for p in files.values())
    rep = cleanup(conn, cfg, now=now, usage=low)
    assert (rep.segments, rep.raw, rep.final, rep.aggressive) == (1, 1, 1, False)
    gone = {k for k, p in files.items() if not p.exists()}
    assert gone == {"seg_old", "raw_old", "final_old", "thumb_old"}
    assert conn.execute("SELECT deleted FROM segments WHERE session_id = 1").fetchone()[0] == 1
    # Disk nearly full → aggressive ages: a 1-day-old raw cut goes too.
    rep = cleanup(conn, cfg, now=now, usage=lambda _p: (90, 100))
    assert rep.aggressive and rep.raw == 1 and not files["raw_new"].exists()
    assert files["final_pending"].exists()  # still waiting to be published


# ---- autostart --------------------------------------------------------------------------


def test_autostart_plans(tmp_path: Path) -> None:
    p = systemd_plan(tmp_path / "repo", Path("config.yaml"), Path(".env"), home=tmp_path)
    ((unit, body),) = p.files.items()
    assert unit == tmp_path / ".config" / "systemd" / "user" / "clipper.service"
    assert "ExecStart=" in body and "run --no-browser" in body and "Restart=on-failure" in body
    assert "systemctl --user enable --now clipper" in p.hint and p.commands == []
    install(p, dry_run=True)
    assert not unit.exists()
    install(p)
    assert unit.read_text(encoding="utf-8") == body

    w = windows_plan(Path("C:/Users/a b/clipper"), Path("config.yaml"), Path(".env"))
    cmd = w.commands[0]
    assert cmd[:5] == ["schtasks", "/Create", "/TN", "clipper", "/TR"]
    assert cmd[5].startswith('"') and cmd[-1] == "/F" and "ONLOGON" in cmd
    ((launcher, script),) = w.files.items()
    assert launcher.name == "clipper-autostart.cmd" and "cd /d" in script
    assert "--no-browser" in script


def test_cli_autostart_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(f"paths:\n  data_dir: {(tmp_path / 'd').as_posix()}\n", encoding="utf-8")
    env = tmp_path / ".env"
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert main(["--config", str(p), "--env", str(env), "install-autostart",
                     "--dry-run"]) == 0  # fmt: skip
        assert main(["--config", str(p), "--env", str(env), "cleanup", "--dry-run"]) == 0
    finally:
        os.chdir(cwd)
    out = capsys.readouterr().out
    assert "run --no-browser" in out and "silinecek" in out


def test_settings_override_min_scores(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "x.db")
    assert settings.number(conn, settings.MIN_KEEP_SCORE, 65) == 65
    settings.put(conn, settings.MIN_KEEP_SCORE, 72)
    assert settings.number(conn, settings.MIN_KEEP_SCORE, 65) == 72
    settings.delete(conn, settings.MIN_KEEP_SCORE)
    assert settings.get(conn, settings.MIN_KEEP_SCORE) is None
    assert time.time() > 0
