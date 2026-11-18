"""Detector-level evaluation of a synthetic scenario (TESTPLAN DET-01..18).

Runs the reactive/viewer-clip detection and the content path with an oracle scanner (the
scenario's ground-truth content scores stand in for Haiku), then compares the session's
candidates with the scenario's expected result.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipper.config import Config
from clipper.content.batch import scan_session
from clipper.content.scanner import ContentScanner, ScanOutcome
from clipper.content.transcript import TLine
from clipper.detect.pipeline import analyze_session
from clipper.evals.scenarios import Scenario, build, load_session
from clipper.models import ChatMessage


class OracleScanner(ContentScanner):
    """ContentScanner whose LLM answer comes from the scenario's ground truth."""

    def __init__(self, cfg: Config, truth: Sequence[tuple[float, float, int, str]]) -> None:
        super().__init__(cfg.content_scan, None, streamer="sentetik", category=None)
        self.truth = list(truth)
        self.calls = 0

    async def scan(
        self, lines: Sequence[TLine], chat: Sequence[ChatMessage], win_start: float,
        win_end: float,
    ) -> ScanOutcome:  # fmt: skip
        if self.is_silent(lines, win_start, win_end):
            return ScanOutcome(win_start, win_end, skipped=True)
        self.calls += 1
        for a, b, score, kind in self.truth:
            lo, hi = max(a, win_start), min(b, win_end)
            if hi - lo >= 0.5 * (win_end - win_start) or (hi - lo >= 20):
                return ScanOutcome(win_start, win_end, skipped=False, score=score, kind=kind,
                                   reason="oracle", best_start=lo, best_end=hi)  # fmt: skip
        return ScanOutcome(win_start, win_end, skipped=False, score=20, kind="none",
                           reason="oracle", best_start=win_start, best_end=win_end)  # fmt: skip


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DetectOutcome:
    scenario: str
    candidates: list[dict[str, Any]]
    checks: list[Check] = field(default_factory=list)
    llm_calls: int = 0

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def _candidates(conn: sqlite3.Connection, session_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, peak_ts, win_start, win_end, det_score, sources, features"
        " FROM candidates WHERE session_id = ? ORDER BY win_start",
        (session_id,),
    ).fetchall()
    out = []
    for r in rows:
        feats = json.loads(r["features"])
        out.append(
            {"id": r["id"], "peak_ts": r["peak_ts"], "win_start": r["win_start"],
             "win_end": r["win_end"], "det_score": r["det_score"],
             "sources": json.loads(r["sources"]) if r["sources"] else ["reactive"],
             "event_ts": feats.get("event_ts")}
        )  # fmt: skip
    return out


def compare(s: Scenario, cands: list[dict[str, Any]]) -> list[Check]:
    exp = s.expected
    checks = [Check("aday sayısı", len(cands) == exp.count, f"{len(cands)} / beklenen {exp.count}")]
    for at, tol, sources in exp.events:
        hit = [c for c in cands if c["win_start"] - tol <= at <= c["win_end"] + tol]
        if not hit:
            checks.append(Check(f"t={at:.0f} adayı", False, "bu ana denk gelen aday yok"))
            continue
        c = min(hit, key=lambda c: abs((c["event_ts"] or c["peak_ts"]) - at))
        missing = [x for x in sources if x not in c["sources"]]
        detail = f"{c['sources']} (eksik: {missing})" if missing else str(c["sources"])
        checks.append(Check(f"t={at:.0f} kaynakları", not missing, detail))
        if "reactive" in sources and c["event_ts"] is not None:
            err = abs(float(c["event_ts"]) - at)
            checks.append(Check(f"t={at:.0f} zaman hatası", err <= tol, f"{err:.1f} sn ≤ {tol}"))
    if exp.max_seconds is not None:
        longest = max((c["win_end"] - c["win_start"] for c in cands), default=0.0)
        checks.append(Check("süre sınırı", longest <= exp.max_seconds + 0.5,
                            f"{longest:.0f} sn ≤ {exp.max_seconds:.0f}"))  # fmt: skip
    return checks


async def run_detect(
    s: Scenario, cfg: Config, conn: sqlite3.Connection, work: Path
) -> DetectOutcome:
    built = build(s, work / "scenarios", ffmpeg=cfg.paths.ffmpeg)
    session_id = load_session(conn, cfg.paths.data_dir, s, built)
    await analyze_session(conn, cfg, session_id, with_audio=True)
    oracle = OracleScanner(cfg, s.content)
    if cfg.content_scan.enabled and s.transcript:
        await scan_session(conn, cfg, session_id, lambda _sess, _base: oracle)
    cands = _candidates(conn, session_id)
    return DetectOutcome(s.id, cands, compare(s, cands), oracle.calls)
