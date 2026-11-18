"""`lag_seconds` calibration (ROADMAP Faz 6): during a live stream the user presses
"şimdi oldu" in the panel at the moment something happens; chat reacts some seconds
later. The median delay between marks and the following chat peak is the suggestion."""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from clipper.detect.events import LiveEvent, read_events, write_events

MARK = "mark"
SEARCH_SECONDS = 60


class CalibrateError(Exception):
    """User-facing (Turkish)."""


def recording_session(conn: sqlite3.Connection, streamer_id: int) -> tuple[int, float]:
    """(session id, wall-clock t0) of the streamer's running live session."""
    row = conn.execute(
        "SELECT id, dir FROM sessions WHERE streamer_id = ? AND source = 'live'"
        " AND status = 'recording' ORDER BY id DESC LIMIT 1",
        (streamer_id,),
    ).fetchone()
    if row is None:
        raise CalibrateError("bu yayıncının şu an kaydedilen canlı yayını yok")
    info = json.loads((Path(row["dir"]) / "session.json").read_text(encoding="utf-8"))
    return int(row["id"]), float(info["t0"])


def add_mark(conn: sqlite3.Connection, streamer_id: int, now: float | None = None) -> float:
    sid, t0 = recording_session(conn, streamer_id)
    ts = (now or time.time()) - t0
    write_events(conn, sid, [LiveEvent(ts, MARK)])
    return ts


@dataclass(frozen=True, slots=True)
class Calibration:
    marks: int
    delays: list[float]
    suggestion: float | None


def chat_peak_after(
    conn: sqlite3.Connection, session_id: int, t: float, span: float = SEARCH_SECONDS
) -> float | None:
    row = conn.execute(
        "SELECT t FROM signal_seconds WHERE session_id = ? AND t BETWEEN ? AND ?"
        " ORDER BY msgs DESC, score DESC, t LIMIT 1",
        (session_id, int(t), int(t + span)),
    ).fetchone()
    return float(row[0]) if row else None


def calibrate(conn: sqlite3.Connection, streamer_id: int, min_marks: int = 3) -> Calibration:
    sessions = [
        int(r[0])
        for r in conn.execute("SELECT id FROM sessions WHERE streamer_id = ?", (streamer_id,))
    ]
    delays: list[float] = []
    marks = 0
    for sid in sessions:
        for ev in read_events(conn, sid, [MARK]):
            marks += 1
            peak = chat_peak_after(conn, sid, ev.ts)
            if peak is not None:
                delays.append(peak - ev.ts)
    suggestion = round(statistics.median(delays), 1) if len(delays) >= min_marks else None
    return Calibration(marks, delays, suggestion)
