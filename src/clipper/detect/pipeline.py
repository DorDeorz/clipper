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
from clipper.config import Config, DetectorConfig, ViewerClipsConfig
from clipper.content.transcript import (
    TRANSCRIPT_FILE,
    covered_seconds,
    read_lines,
    speech_rate,
    voice_keywords,
)
from clipper.detect.audio import segments_db
from clipper.detect.detector import Detection, Detector, Observation
from clipper.detect.events import EventFilter, LiveEvent, events_near, read_events
from clipper.detect.keywords_tr import KeywordScorer, parse_extra_keywords
from clipper.detect.merge import Draft, merge_drafts
from clipper.detect.signals import SecondBin, bin_messages
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
    dcfg = dcfg.model_copy(update={"viewer_clip_bonus": cfg.viewer_clips.bonus})
    scorer = KeywordScorer(extra=parse_extra_keywords(sess["keywords"]))
    bins = bin_messages(messages, scorer, cfg.events.thanks_words)
    lines = read_lines(session_dir / TRANSCRIPT_FILE)
    span = covered_seconds(lines)
    words_per_sec = speech_rate(lines)
    voice_kw = voice_keywords(lines, KeywordScorer())
    events = read_events(conn, session_id)
    filt = EventFilter(cfg.events, events)
    viewer_clips = (
        [e for e in events if e.kind == "viewer_clip"] if cfg.viewer_clips.enabled else []
    )
    audio: dict[int, float] = {}
    if with_audio:
        audio = await segments_db(
            [(Path(s["path"]), float(s["pdt_start"])) for s in segs], cfg.paths.ffmpeg
        )

    det = Detector(dcfg)
    for vc in viewer_clips:
        det.add_viewer_clip(_clip_center(vc))
    detections: list[Detection] = []
    thanks = _thanks_dampening(bins, start, end, dcfg.burst_window, cfg.events.thanks_ratio)
    for t in range(start, end):
        b = bins.get(t)
        st = filt.state(t)
        emote = b.emote if b else 0.0
        msgs, uniq = (b.msgs, b.uniq) if b else (0, 0)
        if t in thanks and b is not None:
            # Welcome / thank-you waves after subs are not reactions: their messages do not
            # count towards the chat rate either.
            emote *= cfg.events.thanks_emote_factor
            if b.msgs:
                keep = max(b.msgs - b.thanks, 0) / b.msgs
                msgs, uniq = b.msgs - b.thanks, round(b.uniq * keep)
        detections += det.feed(
            Observation(
                t=t,
                msgs=msgs,
                uniq=uniq,
                emote=emote,
                audio_db=audio.get(t),
                threshold_mult=st.threshold_mult,
                suppressed=st.suppressed,
                speech=(words_per_sec.get(t, 0.0) if span and span[0] <= t <= span[1] else None),
                vkw=voice_kw.get(t, 0.0),
            )
        )
    detections += det.flush()

    drafts = [_reactive_draft(d, filt) for d in detections]
    if cfg.viewer_clips.as_candidate:
        drafts += [_viewer_clip_draft(vc, dcfg.threshold, cfg.viewer_clips) for vc in viewer_clips]
    merged = merge_drafts(drafts, cfg.merge.radius_seconds, cfg.content_scan.max_candidate_seconds)
    context = _context_features(sess)

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
        for d in merged:
            d.win_start = max(d.win_start, float(start))
            d.win_end = min(d.win_end, float(end))
            msg_from = float(d.features.get("event_ts", d.win_start))
            d.features["top_messages"] = top_messages(messages, msg_from - 5, d.peak_ts + 5)
            d.features["events"] = events_near(
                [e for e in events if e.kind != "viewer_clip"], d.win_start, d.win_end
            )
            d.features.update(context)
            cid = insert_candidate(conn, session_id, d)
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


def insert_candidate(conn: sqlite3.Connection, session_id: int, d: Draft) -> int:
    cur = conn.execute(
        "INSERT INTO candidates (session_id, peak_ts, win_start, win_end, det_score, features,"
        " status, created_at, sources, content_score, content_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)",
        (
            session_id,
            d.peak_ts,
            d.win_start,
            d.win_end,
            round(d.det_score, 4),
            json.dumps(d.features, ensure_ascii=False),
            iso_now(),
            json.dumps(d.sources),
            d.content_score,
            d.content_reason,
        ),
    )
    cid = cur.lastrowid
    assert cid is not None
    return cid


def _clip_center(e: LiveEvent) -> float:
    return e.ts + float(e.data.get("duration") or 0) / 2


def _reactive_draft(d: Detection, filt: EventFilter) -> Draft:
    feats = dict(d.features)
    feats["event_ts"] = d.event_ts
    st = filt.state(float(feats.get("trigger_ts", d.peak_ts)))
    feats["event_filter"] = st.reason
    if "viewer_clips" in feats:
        feats["viewer_clips_near"] = feats.pop("viewer_clips")
    return Draft(d.peak_ts, d.win_start, d.win_end, d.det_score, ["reactive"], feats)


def _viewer_clip_draft(e: LiveEvent, threshold: float, vcfg: ViewerClipsConfig) -> Draft:
    dur = float(e.data.get("duration") or 30)
    info = {k: e.data[k] for k in ("slug", "title", "views") if k in e.data}
    return Draft(
        peak_ts=_clip_center(e),
        win_start=e.ts - vcfg.pad_seconds,
        win_end=e.ts + dur + vcfg.pad_seconds,
        det_score=threshold + vcfg.bonus,
        sources=["viewer_clip"],
        features={"viewer_clips": [{"ts": e.ts, "duration": dur, **info}]},
    )


def _thanks_dampening(
    bins: dict[int, SecondBin], start: int, end: int, window: int, ratio: float
) -> set[int]:
    """Seconds whose trailing window is dominated by welcome / thanks-for-the-sub chat."""
    out: set[int] = set()
    msgs = thanks = 0
    for t in range(start, end):
        b = bins.get(t)
        msgs += b.msgs if b else 0
        thanks += b.thanks if b else 0
        old = bins.get(t - window)
        if old is not None:
            msgs -= old.msgs
            thanks -= old.thanks
        here = (b.msgs, b.thanks) if b else (0, 0)
        if (msgs >= 3 and thanks >= ratio * msgs) or (here[0] >= 3 and here[1] >= ratio * here[0]):
            out.add(t)
    return out


def _context_features(sess: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {"category": sess["category"]}
    started = str(sess["started_at"] or "")
    if len(started) >= 13 and started[11:13].isdigit():
        out["hour"] = int(started[11:13])
    return out
