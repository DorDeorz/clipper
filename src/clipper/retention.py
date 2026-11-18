"""Disk retention (ROADMAP Faz 8).

- Segments of ended sessions are deleted `recorder.buffer_minutes` after the session
  ended, unless a cut job is still waiting for them. (Running live sessions prune their
  own ring buffer.)
- Raw cuts after `raw_days`, final clips after `final_days`. A final clip is kept while
  any publication of it is still pending.
- When the data disk is above `disk_high_percent`, the aggressive ages are used and a
  warning is logged (and shown in the panel status).
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from clipper.config import Config
from clipper.log import get_logger

log = get_logger("retention")

PENDING_PUBS = ("scheduled", "uploading", "processing", "retrying")
DiskUsage = Callable[[Path], tuple[int, int]]  # (used, total) bytes


@dataclass
class CleanupReport:
    segments: int = 0
    raw: int = 0
    final: int = 0
    freed_bytes: int = 0
    disk_percent: float = 0.0
    aggressive: bool = False
    paths: list[str] = field(default_factory=list)

    @property
    def files(self) -> int:
        return self.segments + self.raw + self.final


def disk_usage(path: Path) -> tuple[int, int]:
    path.mkdir(parents=True, exist_ok=True)
    u = shutil.disk_usage(path)
    return u.used, u.total


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _unlink(path: Path, report: CleanupReport, dry_run: bool) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if not dry_run:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("delete failed", extra={"path": str(path), "error": str(exc)})
            return False
    report.freed_bytes += size
    report.paths.append(str(path))
    return True


def _sessions_with_waiting_cuts(conn: sqlite3.Connection) -> set[int]:
    """Sessions whose segments a queued job still needs (cuts, session transcription)."""
    rows = conn.execute(
        "SELECT DISTINCT c.session_id FROM jobs j"
        " JOIN candidates c ON c.id = json_extract(j.payload, '$.candidate_id')"
        " WHERE j.type = 'cut' AND j.status IN ('queued', 'running', 'failed')"
        " UNION SELECT DISTINCT json_extract(payload, '$.session_id') FROM jobs"
        " WHERE type IN ('session_transcribe', 'content_scan')"
        " AND status IN ('queued', 'running', 'failed')"
    ).fetchall()
    return {int(r[0]) for r in rows if r[0] is not None}


def cleanup(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    now: float | None = None,
    dry_run: bool = False,
    usage: DiskUsage = disk_usage,
) -> CleanupReport:
    now = time.time() if now is None else now
    rc = cfg.retention
    used, total = usage(cfg.paths.data_dir)
    report = CleanupReport(disk_percent=round(100.0 * used / total, 1) if total else 0.0)
    report.aggressive = report.disk_percent >= rc.disk_high_percent
    raw_days = rc.aggressive_raw_days if report.aggressive else rc.raw_days
    final_days = rc.aggressive_final_days if report.aggressive else rc.final_days
    if report.aggressive:
        log.warning(
            "disk almost full, aggressive cleanup",
            extra={"percent": report.disk_percent, "limit": rc.disk_high_percent},
        )

    # 1) segments of ended sessions
    busy = _sessions_with_waiting_cuts(conn)
    buffer = cfg.recorder.buffer_minutes * 60.0
    for s in conn.execute(
        "SELECT id, ended_at FROM sessions WHERE status != 'recording' AND ended_at IS NOT NULL"
    ).fetchall():
        ended = _ts(s["ended_at"])
        if ended is None or now - ended < buffer or s["id"] in busy:
            continue
        segs = conn.execute(
            "SELECT id, path FROM segments WHERE session_id = ? AND deleted = 0", (s["id"],)
        ).fetchall()
        for seg in segs:
            if _unlink(Path(seg["path"]), report, dry_run) or not Path(seg["path"]).exists():
                report.segments += 1
                if not dry_run:
                    conn.execute("UPDATE segments SET deleted = 1 WHERE id = ?", (seg["id"],))

    # 2) raw cuts, 3) final clips
    marks = ",".join("?" * len(PENDING_PUBS))
    for k in conn.execute(
        "SELECT id, raw_path, final_path, thumb_path, created_at FROM clips"
        " WHERE raw_path IS NOT NULL OR final_path IS NOT NULL"
    ).fetchall():
        created = _ts(k["created_at"])
        if created is None:
            continue
        age_days = (now - created) / 86400.0
        if k["raw_path"] and age_days >= raw_days:
            if _unlink(Path(k["raw_path"]), report, dry_run):
                report.raw += 1
            if not dry_run:
                conn.execute("UPDATE clips SET raw_path = NULL WHERE id = ?", (k["id"],))
        if k["final_path"] and age_days >= final_days:
            pending = conn.execute(
                f"SELECT 1 FROM publications WHERE clip_id = ? AND status IN ({marks})",
                (k["id"], *PENDING_PUBS),
            ).fetchone()
            if pending:
                continue
            if _unlink(Path(k["final_path"]), report, dry_run):
                report.final += 1
            if k["thumb_path"]:
                _unlink(Path(k["thumb_path"]), report, dry_run)
            if not dry_run:
                conn.execute(
                    "UPDATE clips SET final_path = NULL, thumb_path = NULL WHERE id = ?",
                    (k["id"],),
                )
    if report.files:
        log.info(
            "cleanup",
            extra={
                "segments": report.segments,
                "raw": report.raw,
                "final": report.final,
                "freed_mb": round(report.freed_bytes / 1e6, 1),
                "dry_run": dry_run,
            },
        )
    return report
