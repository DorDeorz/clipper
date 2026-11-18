"""TESTPLAN DET-01..DET-18: the detector part of synthetic scenarios S01–S18."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from clipper import db
from clipper.config import Config, PathsConfig
from clipper.evals.detect_eval import run_detect
from clipper.evals.scenarios import BY_ID, SCENARIOS, build, chat_messages


@pytest.mark.parametrize("sid", [s.id for s in SCENARIOS])
def test_scenario_detector(sid: str, tmp_path: Path) -> None:
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))
    conn = db.open_db(cfg.db_path)
    try:
        out = asyncio.run(run_detect(BY_ID[sid], cfg, conn, tmp_path))
    finally:
        conn.close()
    failed = [f"{c.name}: {c.detail}" for c in out.checks if not c.ok]
    assert not failed, f"{sid} {BY_ID[sid].title}: {failed}; adaylar={out.candidates}"


def test_scenarios_are_deterministic(tmp_path: Path) -> None:
    s = BY_ID["S02"]
    assert chat_messages(s) == chat_messages(s)
    a = build(s, tmp_path / "a")
    b = build(s, tmp_path / "b")
    for name in ("chat.jsonl", "expected.json", "seg/0000.wav"):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_silent_windows_skip_llm(tmp_path: Path) -> None:
    """CNT-01: windows that are 70%+ silent never reach the content LLM."""
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data"))
    conn = db.open_db(cfg.db_path)
    out = asyncio.run(run_detect(BY_ID["S14"], cfg, conn, tmp_path))  # one 4 s line in 2 min
    conn.close()
    assert out.llm_calls == 0
