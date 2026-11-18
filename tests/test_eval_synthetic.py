"""Level 3 smoke: one full-pipeline synthetic scenario with the oracle LLM."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from clipper.config import Config
from clipper.evals.scenarios import SCENARIOS
from clipper.evals.synthetic import run_scenario, write_report

pytestmark = pytest.mark.slow


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")
def test_s13_ad_free_clip_end_to_end(tmp_path: Path) -> None:
    s = next(x for x in SCENARIOS if x.id == "S13")
    r = asyncio.run(run_scenario(s, Config(), tmp_path / "syn", llm_mode="oracle"))
    assert r.error is None
    bad = [c for c in r.checks if not c.ok]
    assert not bad, bad
    assert len(r.clips) == 1

    report = write_report([r], tmp_path / "reports", "oracle")
    data = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["summary"]["passed"] == 1
    assert report.with_suffix(".html").exists()
