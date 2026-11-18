"""Streamers, sessions and segments: DB helpers shared by offline and live input."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

from clipper.models import Segment
from clipper.util.timeutil import iso_now

PlatformName = Literal["twitch", "kick", "youtube"]
CHAT_FILE = "chat.jsonl.gz"


def get_or_create_streamer(
    conn: sqlite3.Connection, display_name: str, platform: PlatformName, channel: str
) -> int:
    row = conn.execute(
        "SELECT id FROM streamers WHERE platform = ? AND channel = ?", (platform, channel)
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO streamers (display_name, platform, channel, keywords, detector_overrides,"
        " publish_targets, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (display_name, platform, channel, "{}", "{}", "[]", iso_now()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def create_session(
    conn: sqlite3.Connection,
    data_dir: Path,
    streamer_id: int,
    source: Literal["live", "vod"],
    title: str | None = None,
    category: str | None = None,
) -> tuple[int, Path]:
    """Insert a session row and create `data/sessions/<id>/`. Returns (id, dir)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO sessions (streamer_id, source, title, category, started_at, dir, status)"
            " VALUES (?, ?, ?, ?, ?, '', 'recording')",
            (streamer_id, source, title, category, iso_now()),
        )
        session_id = cur.lastrowid
        assert session_id is not None
        session_dir = data_dir / "sessions" / str(session_id)
        conn.execute("UPDATE sessions SET dir = ? WHERE id = ?", (str(session_dir), session_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    (session_dir / "seg").mkdir(parents=True, exist_ok=True)
    return session_id, session_dir


def add_segment(conn: sqlite3.Connection, seg: Segment) -> int:
    cur = conn.execute(
        "INSERT INTO segments (session_id, seq, path, pdt_start, duration, is_ad)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (seg.session_id, seg.seq, seg.path, seg.pdt_start, seg.duration, int(seg.is_ad)),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def finish_session(
    conn: sqlite3.Connection, session_id: int, status: Literal["ended", "failed"] = "ended"
) -> None:
    conn.execute(
        "UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?",
        (status, iso_now(), session_id),
    )


def session_meta(conn: sqlite3.Connection, session_id: int) -> dict[str, object]:
    row = conn.execute(
        "SELECT s.*, st.display_name, st.platform, st.channel FROM sessions s"
        " JOIN streamers st ON st.id = s.streamer_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise KeyError(session_id)
    return dict(row)


def write_session_info(session_dir: Path, info: dict[str, object]) -> None:
    (session_dir / "session.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
