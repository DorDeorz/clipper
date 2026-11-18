"""Run the detector over a stored session: signals -> signal_seconds, candidates, cut jobs."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper import chatlog, jobs
from clipper.config import Config, DetectorConfig
from clipper.detect.audio import segments_db
from clipper.detect.detector import Detection, Detector, Observation
from clipper.detect.keywords_tr import KeywordScorer
from clipper.detect.signals import bin_messages
from clipper.log import get_logger
from clipper.models import ChatMessage
from clipper.render.tr_text import tr_casefold
from clipper.sessions import CHAT_FILE
from clipper.util.timeutil import iso_now

log = get_logger("detect")
TOP_MESSAGES = 20


class DetectError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class DetectResult:
    session_id: int
    seconds: int
    candidate_ids: list[int]


def detector_config(base: DetectorConfig, overrides_json: str | None) -> DetectorConfig:
    overrides = json.loads(overrides_json) if overrides_json else {}
    if not overrides:
        return base
    return DetectorConfig.model_validate({**base.model_dump(), **overrides})


def top_messages(
    messages: list[ChatMessage], t0: float, t1: float, n: int = TOP_MESSAGES
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    sample: dict[str, str] = {}
    for m in messages:
        if t0 <= m.ts <= t1 and m.text.strip():
            key = tr_casefold(" ".join(m.text.split()))
            counts[key] += 1
            sample.setdefault(key, m.text.strip())
    return [{"text": sample[k], "count": c} for k, c in counts.most_common(n)]


async def analyze_session(
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    *,
    force: bool = False,
    with_audio: bool = True,
) -> DetectResult:
    sess = conn.execute(
        "SELECT s.*, st.keywords, st.detector_overrides FROM sessions s"
        " JOIN streamers st ON st.id = s.streamer_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if sess is None:
        raise DetectError(f"session #{session_id} bulunamadı")
    existing = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    if existing and not force:
        raise DetectError(
            f"session #{session_id} için zaten {existing} aday var (yeniden için --force)"
        )
    used = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE session_id = ? AND status != 'new'", (session_id,)
    ).fetchone()[0]
    if used:
        raise DetectError(f"session #{session_id} adayları işlenmeye başlamış; silinemez")

    session_dir = Path(sess["dir"])
    chat_path = session_dir / CHAT_FILE
    messages = list(chatlog.read_jsonl(chat_path)) if chat_path.is_file() else []
    segs = conn.execute(
        "SELECT path, pdt_start, duration FROM segments WHERE session_id = ? AND deleted = 0"
        " AND is_ad = 0 ORDER BY seq",
        (session_id,),
    ).fetchall()
    if not segs:
        raise DetectError(f"session #{session_id} için segment yok")
    start = math.floor(min(s["pdt_start"] for s in segs))
    end = math.ceil(max(s["pdt_start"] + s["duration"] for s in segs))

    dcfg = detector_config(cfg.detector, sess["detector_overrides"])
    extra = json.loads(sess["keywords"]) if sess["keywords"] else {}
    bins = bin_messages(messages, KeywordScorer(extra={k: float(v) for k, v in extra.items()}))
    audio: dict[int, float] = {}
    if with_audio:
        audio = await segments_db(
            [(Path(s["path"]), float(s["pdt_start"])) for s in segs], cfg.paths.ffmpeg
        )

    det = Detector(dcfg)
    detections: list[Detection] = []
    for t in range(start, end):
        b = bins.get(t)
        detections += det.feed(
            Observation(
                t=t,
                msgs=b.msgs if b else 0,
                uniq=b.uniq if b else 0,
                emote=b.emote if b else 0.0,
                audio_db=audio.get(t),
            )
        )
    detections += det.flush()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM signal_seconds WHERE session_id = ?", (session_id,))
        old = [
            r[0]
            for r in conn.execute("SELECT id FROM candidates WHERE session_id = ?", (session_id,))
        ]
        if old:
            conn.execute(
                "DELETE FROM jobs WHERE type = 'cut' AND status = 'queued' AND"
                f" json_extract(payload, '$.candidate_id') IN ({','.join('?' * len(old))})",
                old,
            )
            conn.execute("DELETE FROM candidates WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO signal_seconds (session_id, t, msgs, uniq, emote, kw, audio_db, score)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    row.t,
                    bins[row.t].msgs if row.t in bins else 0,
                    bins[row.t].uniq if row.t in bins else 0,
                    round(bins[row.t].emote, 3) if row.t in bins else 0.0,
                    bins[row.t].kw if row.t in bins else 0,
                    round(audio[row.t], 2) if row.t in audio else None,
                    round(row.score, 4),
                )
                for row in det.history
            ],
        )
        ids: list[int] = []
        for d in detections:
            d.win_start = max(d.win_start, float(start))
            d.win_end = min(d.win_end, float(end))
            d.features["top_messages"] = top_messages(messages, d.event_ts - 5, d.peak_ts + 5)
            cur = conn.execute(
                "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score,"
                " features, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'new', ?)",
                (
                    session_id,
                    d.peak_ts,
                    d.win_start,
                    d.win_end,
                    round(d.det_score, 4),
                    json.dumps(d.features, ensure_ascii=False),
                    iso_now(),
                ),
            )
            cid = cur.lastrowid
            assert cid is not None
            ids.append(cid)
            jobs.enqueue(
                conn,
                "cut",
                {"candidate_id": cid, "session_id": session_id, "t0": d.win_start, "t1": d.win_end},
                "cpu",
                priority=max(1, 100 - int(d.det_score * 10)),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    log.info(
        "session analysed",
        extra={"session_id": session_id, "seconds": end - start, "candidates": len(ids)},
    )
    return DetectResult(session_id, end - start, ids)
