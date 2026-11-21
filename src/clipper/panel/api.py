"""Panel data and actions (no HTTP here, so they are easy to test and reuse)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipper import feedback, jobs
from clipper.publish.scheduler import APPROVE_AFTER_RENDER
from clipper.render.layouts import LAYOUTS, Rect
from clipper.stages import RENDER_PRIORITY_BASE
from clipper.util.timeutil import iso_now

RERENDER_PRIORITY = 5  # user is waiting
EDITABLE_TEXT_KEYS = ("title", "hook", "hashtags", "texts")
STREAMER_FIELDS = (
    "display_name", "platform", "channel", "enabled", "layout", "facecam_rect", "keywords",
    "detector_overrides", "publish_targets", "auto_publish", "permission_ok", "permission_note",
)  # fmt: skip


class PanelError(Exception):
    """User-facing (Turkish). `status` is the HTTP status the API should answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _mtime(path: str | None) -> int | None:
    """Cache-buster for media URLs: a re-render overwrites final.mp4 / thumb.jpg in place,
    so without a version the browser (or a CDN/tunnel cache) keeps showing the old video."""
    if not path:
        return None
    try:
        return Path(path).stat().st_mtime_ns // 1_000_000
    except OSError:
        return None


def _rel(data_dir: Path, path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(data_dir.resolve()).as_posix()
    except ValueError:
        return None


def _jl(raw: str | None, default: Any) -> Any:
    return json.loads(raw) if raw else default


# ---- streamers --------------------------------------------------------------------------


def list_streamers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT st.*, (SELECT MAX(id) FROM sessions s WHERE s.streamer_id = st.id) AS last_session"
        " FROM streamers st ORDER BY st.display_name COLLATE NOCASE"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("facecam_rect", "keywords", "detector_overrides"):
            d[k] = _jl(d[k], None)
        d["publish_targets"] = _jl(d["publish_targets"], [])
        out.append(d)
    return out


def _validate_streamer(data: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for k, v in data.items():
        if k not in STREAMER_FIELDS:
            continue
        if k == "platform" and v not in ("twitch", "kick", "youtube"):
            raise PanelError("platform twitch, kick ya da youtube olmalı")
        if k == "layout" and v not in LAYOUTS:
            raise PanelError(f"düzen şunlardan biri olmalı: {', '.join(LAYOUTS)}")
        if k == "facecam_rect":
            if v is None:
                clean[k] = None
                continue
            try:
                v = Rect(float(v["x"]), float(v["y"]), float(v["w"]), float(v["h"])).to_json()
            except (KeyError, TypeError, ValueError) as exc:
                raise PanelError(f"facecam dikdörtgeni geçersiz: {exc}") from exc
        elif k in ("keywords", "detector_overrides", "publish_targets"):
            v = json.dumps(v, ensure_ascii=False) if v is not None else None
        elif k in ("enabled", "auto_publish", "permission_ok"):
            v = 1 if v else 0
        elif k in ("display_name", "channel") and not str(v).strip():
            raise PanelError(f"{k} boş olamaz")
        clean[k] = v
    return clean


def save_streamer(
    conn: sqlite3.Connection, data: dict[str, Any], streamer_id: int | None = None
) -> int:
    clean = _validate_streamer(data)
    if streamer_id is None:
        for k in ("display_name", "platform", "channel"):
            if k not in clean:
                raise PanelError(f"{k} gerekli")
        cols = [*clean, "created_at"]
        cur = conn.execute(
            f"INSERT INTO streamers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [*clean.values(), iso_now()],
        )
        sid = cur.lastrowid
        assert sid is not None
        return sid
    if not clean:
        return streamer_id
    sets = ", ".join(f"{k} = ?" for k in clean)
    n = conn.execute(
        f"UPDATE streamers SET {sets} WHERE id = ?", [*clean.values(), streamer_id]
    ).rowcount
    if n == 0:
        raise PanelError("yayıncı bulunamadı", 404)
    return streamer_id


def streamer_video(conn: sqlite3.Connection, streamer_id: int) -> Path | None:
    row = conn.execute(
        "SELECT g.path FROM segments g JOIN sessions s ON s.id = g.session_id"
        " WHERE s.streamer_id = ? AND g.deleted = 0 ORDER BY s.id DESC, g.seq DESC LIMIT 1",
        (streamer_id,),
    ).fetchone()
    return Path(row[0]) if row and Path(row[0]).is_file() else None


# ---- sessions / signals -----------------------------------------------------------------


def list_sessions(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.id, s.title, s.category, s.status, s.started_at, s.ended_at, s.source,"
        " st.display_name AS streamer, st.platform,"
        " (SELECT COUNT(*) FROM candidates c WHERE c.session_id = s.id) AS candidates"
        " FROM sessions s JOIN streamers st ON st.id = s.streamer_id ORDER BY s.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def session_signals(
    conn: sqlite3.Connection, session_id: int, last: float | None = None
) -> dict[str, Any]:
    bounds = conn.execute(
        "SELECT MIN(t), MAX(t) FROM signal_seconds WHERE session_id = ?", (session_id,)
    ).fetchone()
    if bounds[0] is None:
        return {"t": [], "score": [], "msgs": [], "audio": [], "candidates": [], "scans": []}
    lo = bounds[1] - last if last else bounds[0]
    rows = conn.execute(
        "SELECT t, score, msgs, audio_db FROM signal_seconds WHERE session_id = ? AND t >= ?"
        " ORDER BY t",
        (session_id, lo),
    ).fetchall()
    cands = conn.execute(
        "SELECT id, peak_ts, win_start, win_end, det_score, status, sources FROM candidates"
        " WHERE session_id = ? AND win_end >= ? ORDER BY peak_ts",
        (session_id, lo),
    ).fetchall()
    scans = conn.execute(
        "SELECT win_start, win_end, score, kind FROM content_scans WHERE session_id = ?"
        " AND win_end >= ? ORDER BY win_start",
        (session_id, lo),
    ).fetchall()
    return {
        "t": [r[0] for r in rows],
        "score": [round(r[1] or 0, 3) for r in rows],
        "msgs": [r[2] or 0 for r in rows],
        "audio": [r[3] for r in rows],
        "candidates": [{**dict(c), "sources": _jl(c["sources"], ["reactive"])} for c in cands],
        "scans": [dict(s) for s in scans],
    }


# ---- clips ------------------------------------------------------------------------------

CLIP_VIEWS = {
    "review": "c.status = 'rendered' AND k.review = 'pending'",
    "approved": "k.review = 'approved'",
    "rejected_user": "k.review = 'rejected'",
    "rejected_llm": "c.status = 'rejected' AND k.review = 'pending'",
    "working": "c.status NOT IN ('rendered', 'rejected') AND k.review = 'pending'",
}


def clip_card(row: sqlite3.Row, data_dir: Path) -> dict[str, Any]:
    llm = _jl(row["llm_json"], {})
    feats = _jl(row["features"], {})
    judge = llm.get("judge") or {}
    pre = llm.get("prefilter") or {}
    content = feats.get("content") if isinstance(feats.get("content"), dict) else None
    reject_stage = None
    if row["status"] == "rejected":
        reject_stage = "judge" if judge else "prefilter"
    return {
        "clip_id": row["clip_id"],
        "candidate_id": row["id"],
        "session_id": row["session_id"],
        "streamer": row["streamer"],
        "streamer_id": row["streamer_id"],
        "permission_ok": bool(row["permission_ok"]),
        "status": row["status"],
        "review": row["review"],
        "peak_ts": row["peak_ts"],
        "det_score": row["det_score"],
        "sources": _jl(row["sources"], ["reactive"]),
        "content": content,
        "viewer_clips": feats.get("viewer_clips") or [],
        "top_messages": (feats.get("top_messages") or [])[:5],
        "llm_score": row["llm_score"],
        "reason": judge.get("reason") or pre.get("reason"),
        "reject_stage": reject_stage,
        "prefilter_score": pre.get("rough_score"),
        "title": row["title"],
        "hook": judge.get("hook"),
        "hashtags": judge.get("hashtags") or [],
        "texts": judge.get("texts") or {},
        "flags": judge.get("flags") or {},
        "start": row["start_rel"],
        "end": row["end_rel"],
        "raw_duration": (llm.get("cut") or {}).get("duration"),
        "video": _rel(data_dir, row["final_path"]),
        "media_v": _mtime(row["final_path"]),
        "approve_pending": APPROVE_AFTER_RENDER in llm,
        "raw_video": _rel(data_dir, row["raw_path"]),
        "thumb": _rel(data_dir, row["thumb_path"]),
        "publish_targets": llm.get("publish_targets") or [],
        "created_at": row["created_at"],
    }


def list_clips(
    conn: sqlite3.Connection, data_dir: Path, view: str = "review", limit: int = 100
) -> list[dict[str, Any]]:
    where = CLIP_VIEWS.get(view)
    if where is None:
        raise PanelError(f"bilinmeyen görünüm: {view}")
    rows = conn.execute(
        "SELECT c.*, k.id AS clip_id, k.llm_json, k.llm_score, k.title, k.start_rel,"
        " k.end_rel, k.final_path, k.raw_path, k.thumb_path, k.review, k.created_at,"
        " st.display_name AS streamer, st.id AS streamer_id, st.permission_ok"
        " FROM clips k JOIN candidates c ON c.id = k.candidate_id"
        " JOIN sessions s ON s.id = c.session_id JOIN streamers st ON st.id = s.streamer_id"
        f" WHERE {where} ORDER BY COALESCE(k.llm_score, 0) DESC, k.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [clip_card(r, data_dir) for r in rows]


def _clip(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT k.*, c.status AS cand_status, c.id AS cand_id FROM clips k"
        " JOIN candidates c ON c.id = k.candidate_id WHERE k.id = ?",
        (clip_id,),
    ).fetchone()
    if row is None:
        raise PanelError("klip bulunamadı", 404)
    return row


def _remember_original(llm: dict[str, Any], row: sqlite3.Row) -> None:
    llm.setdefault(
        "original",
        {"start": row["start_rel"], "end": row["end_rel"], "title": row["title"]},
    )


def edit_clip(conn: sqlite3.Connection, clip_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Edit title / hook / hashtags / platform texts and trim (start, end in raw time).
    Returns {"needs_render": bool}."""
    row = _clip(conn, clip_id)
    llm = _jl(row["llm_json"], {})
    _remember_original(llm, row)
    judge = llm.setdefault("judge", {})
    old_hook = judge.get("hook")
    title = row["title"]
    for k in EDITABLE_TEXT_KEYS:
        if k in data:
            judge[k] = data[k]
            if k == "title":
                title = str(data[k]).strip()
                if not title or len(title) > 100:
                    raise PanelError("başlık 1-100 karakter olmalı")
    start, end = row["start_rel"], row["end_rel"]
    needs_render = "hook" in data and (data["hook"] or None) != (old_hook or None)
    if "start" in data or "end" in data:
        dur = float((llm.get("cut") or {}).get("duration") or 1e9)
        start = float(data.get("start", start if start is not None else 0.0))
        end = float(data.get("end", end if end is not None else dur))
        start = max(0.0, start)
        end = min(dur, end)
        if end - start < 3:
            raise PanelError("klip en az 3 saniye olmalı")
        needs_render = needs_render or start != row["start_rel"] or end != row["end_rel"]
    conn.execute(
        "UPDATE clips SET title = ?, start_rel = ?, end_rel = ?, llm_json = ? WHERE id = ?",
        (title, start, end, json.dumps(llm, ensure_ascii=False), clip_id),
    )
    return {"needs_render": bool(needs_render)}


def rerender(conn: sqlite3.Connection, clip_id: int) -> None:
    row = _clip(conn, clip_id)
    if not row["raw_path"] or row["start_rel"] is None:
        raise PanelError("bu klip için ham kesit ya da kırpma yok")
    conn.execute("UPDATE candidates SET status = 'judged' WHERE id = ?", (row["cand_id"],))
    conn.execute("UPDATE clips SET review = 'pending' WHERE id = ?", (clip_id,))
    jobs.enqueue(conn, "render", {"candidate_id": row["cand_id"]}, "cpu", RERENDER_PRIORITY)


def _deltas(llm: dict[str, Any], row: sqlite3.Row) -> tuple[float | None, float | None, bool]:
    orig = llm.get("original") or {}
    sd = ed = None
    if orig.get("start") is not None and row["start_rel"] is not None:
        sd = round(float(row["start_rel"]) - float(orig["start"]), 3) or None
    if orig.get("end") is not None and row["end_rel"] is not None:
        ed = round(float(row["end_rel"]) - float(orig["end"]), 3) or None
    title_edited = bool(orig) and orig.get("title") != row["title"]
    return sd, ed, title_edited


def approve(
    conn: sqlite3.Connection,
    clip_id: int,
    platforms: list[str],
    on_approved: Callable[[int, list[str]], None] | None = None,
) -> str:
    """Approve a rendered clip. If the clip is being re-rendered right now (an edit to the
    hook / trim queued a new render), the approval is stored and finished by
    `scheduler.after_render` when the new video lands: the clip is never published with
    the old render. Returns the feedback decision."""
    row = _clip(conn, clip_id)
    rerendering = row["cand_status"] == "judged" and bool(row["final_path"])
    if not rerendering and (row["cand_status"] != "rendered" or not row["final_path"]):
        raise PanelError("klip henüz render edilmedi")
    if row["review"] != "pending":
        raise PanelError("bu klip zaten incelendi")
    llm = _jl(row["llm_json"], {})
    sd, ed, title_edited = _deltas(llm, row)
    decision: feedback.Decision = "edited" if (sd or ed or title_edited) else "approved"
    feedback.record(
        conn, clip_id, decision, start_delta=sd, end_delta=ed, title_edited=title_edited
    )
    llm["publish_targets"] = platforms
    llm["reviewed_at"] = time.time()
    if rerendering:
        llm[APPROVE_AFTER_RENDER] = platforms
        conn.execute(
            "UPDATE clips SET llm_json = ? WHERE id = ?",
            (json.dumps(llm, ensure_ascii=False), clip_id),
        )
        return decision
    conn.execute(
        "UPDATE clips SET review = 'approved', llm_json = ? WHERE id = ?",
        (json.dumps(llm, ensure_ascii=False), clip_id),
    )
    if on_approved is not None:
        on_approved(clip_id, platforms)
    return decision


def is_deferred(conn: sqlite3.Connection, clip_id: int) -> bool:
    return APPROVE_AFTER_RENDER in _jl(_clip(conn, clip_id)["llm_json"], {})


def reject(conn: sqlite3.Connection, clip_id: int, reason: str, note: str | None = None) -> None:
    if reason not in feedback.REJECT_REASONS:
        raise PanelError(f"geçersiz red nedeni: {reason}")
    _clip(conn, clip_id)
    feedback.record(conn, clip_id, "rejected", reason=reason)
    conn.execute(
        "UPDATE clips SET review = 'rejected', reviewer_note = ? WHERE id = ?", (note, clip_id)
    )


def rescue(conn: sqlite3.Connection, clip_id: int, max_len: float = 59.0) -> None:
    """'This was actually good': an LLM-rejected candidate is rendered after all. The
    most valuable training signal (feedback approved / rescued)."""
    row = _clip(conn, clip_id)
    if row["cand_status"] != "rejected":
        raise PanelError("sadece LLM'in elediği adaylar kurtarılabilir")
    if not row["raw_path"]:
        raise PanelError("bu adayın ham kesiti yok")
    llm = _jl(row["llm_json"], {})
    snap = feedback.features_snapshot(conn, clip_id)
    feedback.record(conn, clip_id, "approved", reason="rescued", snapshot=snap)
    start, end = row["start_rel"], row["end_rel"]
    if start is None or end is None:
        dur = float((llm.get("cut") or {}).get("duration") or max_len)
        start, end = 0.0, min(dur, max_len)
    title = row["title"] or (llm.get("judge") or {}).get("title") or "Kurtarılan klip"
    llm["rescued"] = True
    conn.execute(
        "UPDATE clips SET start_rel = ?, end_rel = ?, title = ?, llm_json = ? WHERE id = ?",
        (start, end, title, json.dumps(llm, ensure_ascii=False), clip_id),
    )
    conn.execute("UPDATE candidates SET status = 'judged' WHERE id = ?", (row["cand_id"],))
    jobs.enqueue(conn, "render", {"candidate_id": row["cand_id"]}, "cpu", RENDER_PRIORITY_BASE)


# ---- publications / accounts / costs ----------------------------------------------------


def list_publications(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.*, k.title, st.display_name AS streamer FROM publications p"
        " JOIN clips k ON k.id = p.clip_id JOIN candidates c ON c.id = k.candidate_id"
        " JOIN sessions s ON s.id = c.session_id JOIN streamers st ON st.id = s.streamer_id"
        " ORDER BY p.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT platform, account, updated_at FROM oauth_tokens ORDER BY platform, account"
    ).fetchall()
    return [dict(r) for r in rows]


def costs(conn: sqlite3.Connection, now: float | None = None) -> dict[str, Any]:
    from clipper.llm.client import month_spend

    now = now or time.time()
    days = conn.execute(
        "SELECT date(ts, 'unixepoch', 'localtime') AS day, ROUND(SUM(usd), 4) AS usd,"
        " COUNT(*) AS calls FROM llm_usage WHERE ts >= ? GROUP BY day ORDER BY day",
        (now - 30 * 86400,),
    ).fetchall()
    purposes = conn.execute(
        "SELECT purpose, model, ROUND(SUM(usd), 4) AS usd, COUNT(*) AS calls,"
        " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens"
        " FROM llm_usage WHERE ts >= ? GROUP BY purpose, model ORDER BY usd DESC",
        (now - 30 * 86400,),
    ).fetchall()
    return {
        "month_usd": round(month_spend(conn, now), 4),
        "days": [dict(r) for r in days],
        "purposes": [dict(r) for r in purposes],
    }


def queue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT type || ':' || status, COUNT(*) FROM jobs WHERE status IN"
        " ('queued', 'running', 'dead') GROUP BY 1"
    ).fetchall()
    return {r[0]: r[1] for r in rows}
