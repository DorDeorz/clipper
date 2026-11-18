"""Content path in VOD (batch) mode: transcribe the whole session, then run the same
`ContentScanner` over it in 60 s steps and merge its candidates with the reactive ones."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from clipper import chatlog, jobs
from clipper.config import Config
from clipper.content.scanner import ContentAccumulator, ContentScanner, ScanOutcome, record_scan
from clipper.content.transcript import TRANSCRIPT_FILE, from_whisper, read_lines
from clipper.detect.merge import Draft
from clipper.detect.pipeline import insert_candidate, top_messages
from clipper.ffmpeg_probe import run_tool
from clipper.log import get_logger
from clipper.media import MediaError, _concat_escape
from clipper.models import ChatMessage
from clipper.sessions import CHAT_FILE
from clipper.transcribe.whisper import Transcriber
from clipper.util.fs import atomic_write_text

log = get_logger("content.batch")
SESSION_TRANSCRIBE_PRIORITY = 300  # after clip transcripts (ROADMAP 4.3b priority rule)
CONTENT_SCAN_PRIORITY = 60


def session_row(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT s.*, st.display_name AS streamer FROM sessions s"
        " JOIN streamers st ON st.id = s.streamer_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise MediaError(f"session #{session_id} bulunamadı")
    return row


async def extract_session_audio(
    conn: sqlite3.Connection, session_id: int, out_wav: Path, ffmpeg: str = "ffmpeg"
) -> float:
    """All non-ad segments as one 16 kHz mono WAV. Returns the session time of its 0."""
    segs = conn.execute(
        "SELECT path, pdt_start, duration FROM segments WHERE session_id = ? AND deleted = 0"
        " AND is_ad = 0 ORDER BY seq",
        (session_id,),
    ).fetchall()
    if not segs:
        raise MediaError(f"session #{session_id} için segment yok")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_wav.with_suffix(".txt")
    list_file.write_text(
        "".join(f"file '{_concat_escape(Path(s['path']).resolve())}'\n" for s in segs),
        encoding="utf-8",
    )
    try:
        res = await run_tool(
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav),
            timeout=4 * 3600,
        )  # fmt: skip
        if res.returncode != 0:
            raise MediaError(f"ses çıkarılamadı: {res.stderr.strip()[:400]}")
    finally:
        list_file.unlink(missing_ok=True)
    return float(segs[0]["pdt_start"])


async def transcribe_session(
    conn: sqlite3.Connection, cfg: Config, session_id: int, transcriber: Transcriber
) -> Path:
    """Write `live_transcript.jsonl` for a finished session (idempotent)."""
    sess = session_row(conn, session_id)
    session_dir = Path(sess["dir"])
    out = session_dir / TRANSCRIPT_FILE
    if out.is_file():
        return out
    work = session_dir / "work"
    wav = work / "session.wav"
    t0 = await extract_session_audio(conn, session_id, wav, cfg.paths.ffmpeg)
    raw_json = work / "session_transcript.json"
    try:
        segments = await transcriber.transcribe(wav, raw_json)
    finally:
        wav.unlink(missing_ok=True)
    lines = from_whisper(segments, offset=t0)
    atomic_write_text(
        out, "".join(json.dumps(ln.to_json(), ensure_ascii=False) + "\n" for ln in lines)
    )
    raw_json.unlink(missing_ok=True)
    log.info("session transcribed", extra={"session_id": session_id, "lines": len(lines)})
    return out


def scan_windows(
    start: float, end: float, every: float, window: float
) -> list[tuple[float, float]]:
    """Windows ending every `every` seconds, each `window` long, covering [start, end]."""
    out: list[tuple[float, float]] = []
    t = start + min(window, max(end - start, 0.0))
    while True:
        out.append((max(start, t - window), min(t, end)))
        if t >= end:
            break
        t = min(t + every, end)
    return out


def chat_baseline(chat: Sequence[ChatMessage], start: float, end: float) -> float | None:
    return len(chat) / (end - start) if chat and end > start else None


async def scan_session(
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    scanner_factory: Callable[[sqlite3.Row, float | None], ContentScanner],
    say: Callable[[str], None] = lambda _m: None,
) -> list[int]:
    """Run the content scanner over the whole transcript; returns the touched candidate ids
    (new ones and reactive ones that gained the `content` source)."""
    sess = session_row(conn, session_id)
    session_dir = Path(sess["dir"])
    lines = read_lines(session_dir / TRANSCRIPT_FILE)
    if not lines:
        say("Transkript yok ya da boş; içerik taraması atlandı.")
        return []
    chat_path = session_dir / CHAT_FILE
    chat = list(chatlog.read_jsonl(chat_path)) if chat_path.is_file() else []
    bounds = conn.execute(
        "SELECT MIN(pdt_start), MAX(pdt_start + duration) FROM segments WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    start, end = float(bounds[0]), float(bounds[1])
    scanner = scanner_factory(sess, chat_baseline(chat, start, end))
    acc = ContentAccumulator(cfg.content_scan, cfg.detector.threshold)
    conn.execute("DELETE FROM content_scans WHERE session_id = ?", (session_id,))
    drafts: list[Draft] = []
    usd = 0.0
    windows = scan_windows(
        start, end, cfg.content_scan.every_seconds, cfg.content_scan.window_seconds
    )
    for i, (a, b) in enumerate(windows, 1):
        o: ScanOutcome = await scanner.scan(lines, chat, a, b)
        record_scan(conn, session_id, o)
        usd += o.usd
        drafts += acc.feed(o)
        if i % 20 == 0:
            say(f"  içerik taraması {i}/{len(windows)} pencere (${usd:.3f})")
    drafts += acc.flush()
    ids = apply_content_drafts(conn, cfg, session_id, drafts, chat)
    say(f"İçerik yolu: {len(drafts)} aday, {len(windows)} pencere, ${usd:.3f}")
    return ids


def apply_content_drafts(
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    drafts: Sequence[Draft],
    chat: Sequence[ChatMessage] = (),
) -> list[int]:
    """Merge content drafts into the session's candidates (±radius): an overlapping
    candidate gains the `content` source; otherwise a new candidate + cut job is made."""
    radius = cfg.merge.radius_seconds
    touched: list[int] = []
    for d in drafts:
        row = conn.execute(
            "SELECT id, sources, features, content_score FROM candidates WHERE session_id = ?"
            " AND win_start - ? <= ? AND ? <= win_end + ? ORDER BY ABS(peak_ts - ?) LIMIT 1",
            (session_id, radius, d.win_end, d.win_start, radius, d.peak_ts),
        ).fetchone()
        if row is not None:
            sources: list[str] = json.loads(row["sources"])
            feats: dict[str, Any] = json.loads(row["features"])
            if "content" not in sources:
                sources.append("content")
            feats["content"] = d.features.get("content")
            feats["sources"] = sources
            best = max(row["content_score"] or 0, d.content_score or 0)
            conn.execute(
                "UPDATE candidates SET sources = ?, features = ?, content_score = ?,"
                " content_reason = ? WHERE id = ?",
                (
                    json.dumps(sources),
                    json.dumps(feats, ensure_ascii=False),
                    best,
                    d.content_reason if (d.content_score or 0) >= best else None,
                    row["id"],
                ),
            )
            touched.append(int(row["id"]))
            continue
        d.features["sources"] = list(d.sources)
        d.features["top_messages"] = top_messages(list(chat), d.win_start, d.win_end)
        cid = insert_candidate(conn, session_id, d)
        jobs.enqueue(
            conn,
            "cut",
            {"candidate_id": cid, "session_id": session_id, "t0": d.win_start, "t1": d.win_end},
            "cpu",
            priority=max(1, 100 - int(d.det_score * 10)),
        )
        touched.append(cid)
    return touched


def enqueue_content_path(conn: sqlite3.Connection, cfg: Config, session_id: int) -> bool:
    """Queue the batch content path for a session (VOD mode). No-op if disabled."""
    if not cfg.content_scan.enabled:
        return False
    jobs.enqueue(
        conn,
        "session_transcribe",
        {"session_id": session_id},
        "gpu",
        SESSION_TRANSCRIBE_PRIORITY,
    )
    return True
