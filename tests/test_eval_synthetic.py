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


@pytest.mark.slow
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


def test_resource_monitor_writes_rows(tmp_path: Path) -> None:
    from clipper import db
    from clipper.evals.monitor import FIELDS, ResourceMonitor, summarize

    dbp = tmp_path / "c.db"
    conn = db.open_db(dbp)
    conn.execute(
        "INSERT INTO jobs(type, pool, payload, run_after, created_at) VALUES('x','cpu','{}',0,0)"
    )
    conn.commit()
    conn.close()
    out = tmp_path / "m" / "metrics.csv"

    async def go() -> None:
        async with ResourceMonitor(out, disk=tmp_path, db=lambda: dbp, interval=0.05):
            await asyncio.sleep(0.3)

    asyncio.run(go())
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == FIELDS
    assert len(lines) >= 3
    import csv

    row = next(csv.DictReader(lines))
    assert float(row["rss_mb"]) > 0
    assert row["queued"] == "1" and row["queued_cpu"] == "1"
    s = summarize(out)
    assert s["rss_mb_max"] and s["disk_free_gb_min"] is not None
