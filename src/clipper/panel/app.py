"""FastAPI panel (ROADMAP 4.8). Thin HTTP layer over `panel.api`.

All endpoints are `async def`, so they run on the event-loop thread and can share the
single sqlite3 connection with the workers (same thread, autocommit, no transaction
is held across an await).
"""

from __future__ import annotations

import base64
import ipaddress
import json
import secrets as _secrets
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from clipper import __version__, jobs
from clipper.config import Config, Secrets
from clipper.feedback import REJECT_REASONS
from clipper.log import get_logger, ring_buffer
from clipper.panel import api
from clipper.panel.api import PanelError

log = get_logger("panel")
STATIC_DIR = Path(__file__).parent / "static"
LOOPBACK_NAMES = {"localhost"}


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class PanelConfigError(Exception):
    pass


def check_bind(cfg: Config, secrets: Secrets) -> None:
    """Refuse to listen beyond loopback without a password (ROADMAP 4.8)."""
    if not is_loopback(cfg.panel.host) and secrets.panel_password is None:
        raise PanelConfigError(
            f"panel.host={cfg.panel.host} dışarıya açık; önce .env'e PANEL_PASSWORD ekle"
        )


@dataclass
class PanelDeps:
    conn: sqlite3.Connection
    cfg: Config
    secrets: Secrets
    # Facecam suggestion needs an LLM; None hides the button's backend.
    facecam_suggest: Callable[[int], Awaitable[dict[str, Any]]] | None = None


def _auth_ok(header: str | None, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    _user, _, given = raw.partition(":")
    return _secrets.compare_digest(given.encode(), password.encode())


def safe_media_path(data_dir: Path, rel: str) -> Path:
    """Resolve `rel` inside `data_dir`; anything escaping it (.., absolute, symlink out)
    is a 404, never a file."""
    root = data_dir.resolve()
    if not rel or "\x00" in rel:
        raise HTTPException(404, "dosya yok")
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(404, "dosya yok")
    return candidate


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PanelError("geçersiz JSON gövdesi") from exc
    if not isinstance(data, dict):
        raise PanelError("JSON gövdesi bir nesne olmalı")
    return data


def create_app(deps: PanelDeps) -> FastAPI:
    app = FastAPI(title="clipper paneli", version=__version__, docs_url=None, redoc_url=None)
    conn, cfg = deps.conn, deps.cfg
    data_dir = cfg.paths.data_dir
    password = deps.secrets.panel_password

    @app.exception_handler(PanelError)
    async def _panel_error(_req: Request, exc: PanelError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    if password is not None:
        pw = password.get_secret_value()

        @app.middleware("http")
        async def _basic_auth(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            if not _auth_ok(request.headers.get("authorization"), pw):
                return Response(
                    "Parola gerekli",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="clipper"'},
                )
            return await call_next(request)

    # ---- static / media -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/media/{rel:path}", include_in_schema=False)
    async def media(rel: str) -> FileResponse:
        return FileResponse(safe_media_path(data_dir, rel))

    # ---- status / logs / costs ----------------------------------------------------------

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {
            "version": __version__,
            "queue": api.queue_counts(conn),
            "jobs": jobs.counts(conn),
            "reject_reasons": list(REJECT_REASONS),
            "layouts": ["stack", "blur", "center"],
            "facecam_suggest": deps.facecam_suggest is not None,
            "review_count": len(api.list_clips(conn, data_dir, "review")),
        }

    @app.get("/api/logs")
    async def logs(limit: int = 500) -> list[dict[str, Any]]:
        out = []
        for line in ring_buffer().lines()[-max(1, min(limit, 500)) :]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"msg": line})
        return out

    @app.get("/api/costs")
    async def costs() -> dict[str, Any]:
        return api.costs(conn)

    # ---- streamers ----------------------------------------------------------------------

    @app.get("/api/streamers")
    async def streamers() -> list[dict[str, Any]]:
        return api.list_streamers(conn)

    @app.post("/api/streamers", status_code=201)
    async def create_streamer(request: Request) -> dict[str, Any]:
        sid = api.save_streamer(conn, await _body(request))
        return {"id": sid}

    @app.put("/api/streamers/{streamer_id}")
    async def update_streamer(streamer_id: int, request: Request) -> dict[str, Any]:
        api.save_streamer(conn, await _body(request), streamer_id)
        return {"id": streamer_id}

    @app.post("/api/streamers/{streamer_id}/frame")
    async def streamer_frame(streamer_id: int) -> dict[str, Any]:
        """A still from the streamer's latest recording, for the facecam rectangle picker."""
        from clipper.llm.facecam_vision import grab_frames
        from clipper.media import MediaError, probe

        video = api.streamer_video(conn, streamer_id)
        if video is None:
            raise PanelError("bu yayıncının kaydedilmiş videosu yok (önce VOD/yayın)", 404)
        try:
            info = await probe(video, cfg.paths.ffprobe)
        except MediaError as exc:
            raise PanelError(f"video okunamadı: {exc}", 500) from exc
        out_dir = data_dir / "facecam" / str(streamer_id)
        frames = await grab_frames(cfg.paths.ffmpeg, video, [info.duration * 0.45], out_dir)
        if not frames:
            raise PanelError("videodan kare alınamadı", 500)
        frames[0].replace(out_dir / "frame.jpg")
        return {"frame": f"facecam/{streamer_id}/frame.jpg", "width": info.width,
                "height": info.height}  # fmt: skip

    @app.post("/api/streamers/{streamer_id}/facecam/suggest")
    async def facecam_suggest(streamer_id: int) -> dict[str, Any]:
        if deps.facecam_suggest is None:
            raise PanelError("facecam önerisi için ANTHROPIC_API_KEY gerekli", 503)
        return await deps.facecam_suggest(streamer_id)

    # ---- sessions -----------------------------------------------------------------------

    @app.get("/api/sessions")
    async def sessions(limit: int = 50) -> list[dict[str, Any]]:
        return api.list_sessions(conn, limit)

    @app.get("/api/sessions/{session_id}/signals")
    async def signals(session_id: int, last: float | None = 1800.0) -> dict[str, Any]:
        return api.session_signals(conn, session_id, last)

    @app.get("/api/live")
    async def live() -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT st.id AS streamer_id, st.display_name, st.platform, st.channel,"
            " s.id AS session_id, s.started_at, s.title, s.category,"
            " (SELECT COUNT(*) FROM candidates c WHERE c.session_id = s.id) AS candidates"
            " FROM sessions s JOIN streamers st ON st.id = s.streamer_id"
            " WHERE s.source = 'live' AND s.status = 'recording' ORDER BY s.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/live/{streamer_id}/mark")
    async def live_mark(streamer_id: int) -> dict[str, Any]:
        from clipper.live.calibrate import CalibrateError, add_mark

        try:
            ts = add_mark(conn, streamer_id)
        except CalibrateError as exc:
            raise PanelError(str(exc), 404) from exc
        return {"ts": ts}

    # ---- clips --------------------------------------------------------------------------

    @app.get("/api/clips")
    async def clips(view: str = "review", limit: int = 100) -> list[dict[str, Any]]:
        return api.list_clips(conn, data_dir, view, limit)

    @app.put("/api/clips/{clip_id}")
    async def edit(clip_id: int, request: Request) -> dict[str, Any]:
        body = await _body(request)
        res = api.edit_clip(conn, clip_id, body)
        if res["needs_render"] and body.get("render", True):
            api.rerender(conn, clip_id)
            res["rendering"] = True
        return res

    @app.post("/api/clips/{clip_id}/rerender")
    async def do_rerender(clip_id: int) -> dict[str, Any]:
        api.rerender(conn, clip_id)
        return {"ok": True}

    @app.post("/api/clips/{clip_id}/approve")
    async def do_approve(clip_id: int, request: Request) -> dict[str, Any]:
        body = await _body(request)
        platforms = body.get("platforms") or []
        if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
            raise PanelError("platforms bir metin listesi olmalı")
        decision = api.approve(conn, clip_id, platforms)
        return {"ok": True, "decision": decision}

    @app.post("/api/clips/{clip_id}/reject")
    async def do_reject(clip_id: int, request: Request) -> dict[str, Any]:
        body = await _body(request)
        api.reject(conn, clip_id, str(body.get("reason") or ""), body.get("note"))
        return {"ok": True}

    @app.post("/api/clips/{clip_id}/rescue")
    async def do_rescue(clip_id: int) -> dict[str, Any]:
        api.rescue(conn, clip_id, cfg.render.max_len)
        return {"ok": True}

    # ---- publishing / accounts ----------------------------------------------------------

    @app.get("/api/publications")
    async def publications() -> list[dict[str, Any]]:
        return api.list_publications(conn)

    @app.post("/api/publications/{pub_id}/retry")
    async def retry_publication(pub_id: int) -> dict[str, Any]:
        raise PanelError("yayınlama henüz yok (Faz 7)", 501)

    @app.get("/api/accounts")
    async def accounts() -> list[dict[str, Any]]:
        return api.list_accounts(conn)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
