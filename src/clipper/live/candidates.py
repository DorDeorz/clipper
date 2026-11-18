"""Live candidate bookkeeping: drafts from the three paths arrive at different times, so
each one is merged into an overlapping candidate that has not been cut yet (ROADMAP 4.3b
"Birleştirme"), otherwise inserted with a delayed `cut` job (the segments up to its end
must exist first)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from clipper import jobs
from clipper.detect.merge import Draft, merge_drafts
from clipper.detect.pipeline import insert_candidate

CUT_DELAY = 20.0  # wall seconds after win_end before cutting (HLS latency + last segment)


def _as_draft(row: sqlite3.Row) -> Draft:
    feats: dict[str, Any] = json.loads(row["features"]) if row["features"] else {}
    return Draft(
        peak_ts=float(row["peak_ts"]),
        win_start=float(row["win_start"]),
        win_end=float(row["win_end"]),
        det_score=float(row["det_score"]),
        sources=json.loads(row["sources"]) if row["sources"] else ["reactive"],
        features=feats,
        content_score=row["content_score"],
        content_reason=row["content_reason"],
    )


def _cut_priority(d: Draft) -> int:
    return max(1, 100 - int(d.det_score * 10))


def upsert(
    conn: sqlite3.Connection,
    session_id: int,
    draft: Draft,
    *,
    t0: float,
    radius: float,
    max_seconds: float,
    now: float,
) -> tuple[int, bool]:
    """Returns (candidate id, merged?). `t0` maps session time to wall clock for the cut
    job's `run_after`."""
    rows = conn.execute(
        "SELECT * FROM candidates WHERE session_id = ? AND status = 'new'"
        " AND win_start <= ? AND win_end >= ?",
        (session_id, draft.win_end + radius, draft.win_start - radius),
    ).fetchall()
    for row in rows:
        existing = _as_draft(row)
        if set(draft.sources) <= set(existing.sources):
            continue  # same path twice: keep separate (merge_drafts would too)
        merged = merge_drafts([existing, draft], radius, max_seconds)
        if len(merged) != 1:
            continue
        m = merged[0]
        conn.execute(
            "UPDATE candidates SET peak_ts = ?, win_start = ?, win_end = ?, det_score = ?,"
            " features = ?, sources = ?, content_score = ?, content_reason = ? WHERE id = ?",
            (
                m.peak_ts, m.win_start, m.win_end, round(m.det_score, 4),
                json.dumps(m.features, ensure_ascii=False), json.dumps(m.sources),
                m.content_score, m.content_reason, row["id"],
            ),
        )  # fmt: skip
        conn.execute(
            "UPDATE jobs SET payload = ?, run_after = MAX(run_after, ?), priority = ?"
            " WHERE type = 'cut' AND status = 'queued'"
            " AND json_extract(payload, '$.candidate_id') = ?",
            (
                json.dumps(
                    {"candidate_id": row["id"], "session_id": session_id,
                     "t0": m.win_start, "t1": m.win_end}
                ),
                t0 + m.win_end + CUT_DELAY,
                _cut_priority(m),
                row["id"],
            ),
        )  # fmt: skip
        return int(row["id"]), True
    cid = insert_candidate(conn, session_id, draft)
    jobs.enqueue(
        conn,
        "cut",
        {"candidate_id": cid, "session_id": session_id, "t0": draft.win_start, "t1": draft.win_end},
        "cpu",
        priority=_cut_priority(draft),
        run_after=max(now, t0 + draft.win_end + CUT_DELAY),
    )
    return cid, False
