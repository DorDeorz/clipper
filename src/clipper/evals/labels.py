"""Human "good moment" labels for the golden set (TESTPLAN §4, `clipper label`).

Labels live in `eval/labels/<session>.json` (or `<session>_<labeler>.json` for a second
labeler). Times are stored in session time (the same clock as `candidates.peak_ts`),
plus the proxy-video position the labeler actually saw, so nothing depends on how the
proxy was cut. The labeling UI never sees the system's candidates.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper.ffmpeg_probe import run_tool
from clipper.media import _concat_escape
from clipper.util.fs import atomic_write_text

TYPES = ("komik", "şok", "hikâye", "atışma", "oyun anı", "diğer")
QUALITIES = (1, 2, 3)
MAX_LABEL_SECONDS = 600.0


class LabelError(ValueError):
    pass


@dataclass(frozen=True)
class Piece:
    """One segment of the proxy video: [video_t, video_t + duration) ↔ session time t0."""

    video_t: float
    t0: float
    duration: float


def pieces(conn: sqlite3.Connection, session_id: int) -> list[tuple[Path, Piece]]:
    rows = conn.execute(
        "SELECT path, pdt_start, duration FROM segments WHERE session_id = ? AND deleted = 0"
        " ORDER BY seq",
        (session_id,),
    ).fetchall()
    out: list[tuple[Path, Piece]] = []
    v = 0.0
    for r in rows:
        out.append((Path(r[0]), Piece(round(v, 3), float(r[1]), float(r[2]))))
        v += float(r[2])
    return out


def video_to_session(t: float, ps: list[Piece]) -> float:
    if not ps:
        return t
    for p in ps:
        if t < p.video_t + p.duration:
            return round(p.t0 + max(0.0, t - p.video_t), 2)
    last = ps[-1]
    return round(last.t0 + last.duration, 2)


def label_path(labels_dir: Path, session_id: int, labeler: str = "") -> Path:
    safe = re.sub(r"[^\w-]+", "_", labeler.strip(), flags=re.UNICODE).strip("_")
    return labels_dir / (f"{session_id}_{safe}.json" if safe else f"{session_id}.json")


def validate(raw: Any, ps: list[Piece], video_len: float) -> list[dict[str, Any]]:
    """Check what the browser sent; return labels with session times filled in."""
    if not isinstance(raw, list):
        raise LabelError("etiketler bir liste olmalı")
    out = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise LabelError(f"{i}. etiket geçersiz")
        try:
            vs = float(item["video_start"])
            ve = float(item["video_end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LabelError(f"{i}. etiket: başlangıç/bitiş eksik") from exc
        if not 0 <= vs < ve <= video_len + 1:
            raise LabelError(f"{i}. etiket: başlangıç bitişten önce ve video içinde olmalı")
        if ve - vs > MAX_LABEL_SECONDS:
            raise LabelError(f"{i}. etiket {MAX_LABEL_SECONDS / 60:.0f} dakikadan uzun olamaz")
        kind = item.get("type")
        if kind not in TYPES:
            raise LabelError(f"{i}. etiket: tür şunlardan biri olmalı: {', '.join(TYPES)}")
        quality = item.get("quality")
        if quality not in QUALITIES:
            raise LabelError(f"{i}. etiket: kalite 1, 2 ya da 3 olmalı")
        note = str(item.get("note") or "")[:500]
        out.append(
            {
                "start": video_to_session(vs, ps),
                "end": video_to_session(ve, ps),
                "video_start": round(vs, 2),
                "video_end": round(ve, 2),
                "type": kind,
                "quality": quality,
                "note": note,
            }
        )
    out.sort(key=lambda x: x["video_start"])
    return out


def load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save(
    path: Path,
    session: dict[str, Any],
    labeler: str,
    labels: list[dict[str, Any]],
) -> dict[str, Any]:
    prev = load(path) or {}
    doc = {
        "session_id": session["id"],
        "streamer": session.get("streamer"),
        "title": session.get("title"),
        "labeler": labeler,
        "created": prev.get("created", time.time()),
        "updated": time.time(),
        "types": list(TYPES),
        "labels": labels,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=1))
    return doc


def session_info(conn: sqlite3.Connection, session_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT s.id, s.title, s.category, s.started_at, s.source, st.display_name AS streamer"
        " FROM sessions s JOIN streamers st ON st.id = s.streamer_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise LabelError(f"session #{session_id} bulunamadı")
    return dict(
        zip(("id", "title", "category", "started_at", "source", "streamer"), row, strict=True)
    )


async def build_proxy(
    conn: sqlite3.Connection,
    session_id: int,
    out: Path,
    ffmpeg: str = "ffmpeg",
    *,
    height: int = 540,
) -> Path:
    """A small browser-playable MP4 (H.264/AAC, faststart) of every kept segment.

    Ad segments stay in, so the proxy timeline maps 1:1 onto the segment list."""
    ps = pieces(conn, session_id)
    if not ps:
        raise LabelError(f"session #{session_id} için video segmenti yok")
    if out.exists() and out.stat().st_mtime >= max(p.stat().st_mtime for p, _ in ps):
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    list_file = out.with_suffix(".txt")
    list_file.write_text(
        "".join(f"file '{_concat_escape(p.resolve())}'\n" for p, _ in ps), encoding="utf-8"
    )
    tmp = out.with_name(out.stem + ".part.mp4")
    try:
        res = await run_tool(
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(tmp),
            timeout=6 * 3600,
        )  # fmt: skip
        if res.returncode != 0:
            raise LabelError(f"önizleme videosu üretilemedi: {res.stderr.strip()[:400]}")
        tmp.replace(out)
    finally:
        list_file.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
    return out


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Intersection over the shorter span (TESTPLAN: "çakışma ≥ %50")."""
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    return inter / max(1e-9, min(a[1] - a[0], b[1] - b[0]))


def agreement(a: list[dict[str, Any]], b: list[dict[str, Any]], min_overlap: float = 0.5) -> float:
    """Labels (from both people) that overlap ≥ min_overlap with one of the other's, over
    all labels. 1.0 when both are empty."""
    total = len(a) + len(b)
    if total == 0:
        return 1.0

    def hits(xs: list[dict[str, Any]], ys: list[dict[str, Any]]) -> int:
        return sum(
            any(overlap((x["start"], x["end"]), (y["start"], y["end"])) >= min_overlap for y in ys)
            for x in xs
        )

    return round((hits(a, b) + hits(b, a)) / total, 3)
