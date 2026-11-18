"""Review decisions -> `feedback` table (ROADMAP 4.9 Kademe 0).

Every decision freezes a snapshot of all candidate features and LLM scores at that
moment, so later changes to feature computation do not change the training data."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Literal

Decision = Literal["approved", "rejected", "edited"]
REJECT_REASONS = (
    "not_funny", "no_context", "bad_cut", "duplicate", "music", "sensitive", "low_quality",
    "other",
)  # fmt: skip


class FeedbackError(Exception):
    pass


def features_snapshot(conn: sqlite3.Connection, clip_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT c.*, k.llm_json, k.llm_score, k.start_rel, k.end_rel, k.title,"
        " s.category, s.streamer_id FROM clips k JOIN candidates c ON c.id = k.candidate_id"
        " JOIN sessions s ON s.id = c.session_id WHERE k.id = ?",
        (clip_id,),
    ).fetchone()
    if row is None:
        raise FeedbackError(f"klip #{clip_id} bulunamadı")
    feats = json.loads(row["features"]) if row["features"] else {}
    llm = json.loads(row["llm_json"]) if row["llm_json"] else {}
    feats.pop("top_messages", None)  # large and not a feature
    pre = llm.get("prefilter") or {}
    judge = llm.get("judge") or {}
    return {
        "candidate_id": row["id"],
        "streamer_id": row["streamer_id"],
        "category": row["category"],
        "det_score": row["det_score"],
        "sources": json.loads(row["sources"]) if row["sources"] else ["reactive"],
        "content_score": row["content_score"],
        "window": [row["win_start"], row["win_end"]],
        "peak_ts": row["peak_ts"],
        "features": feats,
        "prefilter_score": pre.get("rough_score"),
        "judge_score": judge.get("score", row["llm_score"]),
        "judge_flags": judge.get("flags"),
        "judge_model": judge.get("model"),
        "start_rel": row["start_rel"],
        "end_rel": row["end_rel"],
        "title": row["title"],
        "candidate_status": row["status"],
    }


def record(
    conn: sqlite3.Connection,
    clip_id: int,
    decision: Decision,
    *,
    reason: str | None = None,
    start_delta: float | None = None,
    end_delta: float | None = None,
    title_edited: bool = False,
    snapshot: dict[str, Any] | None = None,
) -> int:
    if decision not in ("approved", "rejected", "edited"):
        raise FeedbackError(f"geçersiz karar: {decision}")
    if decision == "rejected" and reason is not None and reason not in REJECT_REASONS:
        raise FeedbackError(f"geçersiz red nedeni: {reason}")
    snap = snapshot if snapshot is not None else features_snapshot(conn, clip_id)
    cur = conn.execute(
        "INSERT INTO feedback (clip_id, decision, reason, start_delta, end_delta, title_edited,"
        " features_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            clip_id,
            decision,
            reason,
            start_delta,
            end_delta,
            int(title_edited),
            json.dumps(snap, ensure_ascii=False),
            time.time(),
        ),
    )
    fid = cur.lastrowid
    assert fid is not None
    return fid
