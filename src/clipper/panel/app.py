"""FastAPI panel (ROADMAP 4.8). Thin HTTP layer over `panel.api`.

All endpoints are `async def`, so they run on the event-loop thread and can share the
single sqlite3 connection with the workers (same thread, autocommit, no transaction
is held across an await).
"""

from __future__ import annotations

import asyncio
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from clipper import __version__, jobs
from clipper.config import Config, Secrets, load_secrets
from clipper.feedback import REJECT_REASONS
from clipper.log import get_logger, ring_buffer
from clipper.panel import api, setup
from clipper.panel.api import PanelError
from clipper.panel.vodjobs import VodError, VodTasks, detect_source, kick_vods
from clipper.publish import scheduler
from clipper.publish.oauth import PLATFORMS, OAuthError
from clipper.publish.wiring import PublishKit

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
    # Publishing (Faz 7): OAuth + publishers. None → approve only records the decision.
    publish: PublishKit | None = None
    # Faz 9 (kullanıcı dostu panel): .env path, VOD imports, restart/shutdown hooks.
    env_path: Path | None = None
    config_path: Path | None = None
    vod: VodTasks | None = None
    system: Callable[[str], None] | None = None  # "restart" | "shutdown"
    llm_active: bool = False
    restart_needed: bool = False


def _auth_ok(header: str | None, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    _user, _, given = raw.partition(":")
    return _secrets.compare_digest(given.encode(), password.encode())


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-csrf-token"


def _host_name(host_header: str) -> str:
    """Host header -> bare host name (no port, no IPv6 brackets), lowercased."""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1 : h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _origin_host(origin: str) -> str | None:
    """`scheme://host[:port]` of an Origin/Referer header -> `host[:port]`, lowercased."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return None
    return parts.netloc.lower() or None


def request_guard_error(
    method: str, headers: dict[str, str], *, loopback: bool, token: str
) -> tuple[int, str] | None:
    """Audit B-02. Returns (status, Turkish message) when the request must be refused.

    - Bound to loopback: the Host must be a loopback name (DNS rebinding sends the
      attacker's host name), for every request.
    - State-changing requests need the per-process CSRF token in `X-CSRF-Token` (a
      custom header also forces a CORS preflight, which the panel never grants), a
      same-origin `Origin`/`Referer` when the browser sends one, and a JSON body.
    """
    host = headers.get("host", "")
    if loopback and not is_loopback(_host_name(host)):
        return 403, "panel sadece yerel adresten (127.0.0.1 / localhost) açılabilir"
    if method not in UNSAFE_METHODS:
        return None
    source = headers.get("origin") or headers.get("referer")
    if source and _origin_host(source) != host.strip().lower():
        return 403, "başka bir siteden gelen istek reddedildi"
    given = headers.get(CSRF_HEADER, "")
    if not given or not _secrets.compare_digest(given.encode(), token.encode()):
        return 403, "CSRF anahtarı eksik ya da geçersiz; paneli yenile"
    has_body = headers.get("content-length", "0") not in ("", "0") or (
        "transfer-encoding" in headers
    )
    ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if has_body and ctype != "application/json":
        return 415, "istek gövdesi application/json olmalı"
    return None


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


def _callback_page(message: str, *, ok: bool = True) -> str:
    import html

    color = "#2fb67c" if ok else "#e5534b"
    return (
        '<!doctype html><html lang="tr"><meta charset="utf-8"><title>clipper</title>'
        '<body style="background:#111318;color:#e6e8ec;font:16px system-ui;padding:40px">'
        f'<p style="color:{color}">{html.escape(message)}</p>'
        '<p><a style="color:#4f8cff" href="/">Panele dön</a></p></body></html>'
    )


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

    # Registered after basic auth, so it runs first (outermost middleware).
    app.state.csrf_token = _secrets.token_urlsafe(32)
    loopback = is_loopback(cfg.panel.host)

    @app.middleware("http")
    async def _request_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        refused = request_guard_error(
            request.method,
            {k.lower(): v for k, v in request.headers.items()},
            loopback=loopback,
            token=app.state.csrf_token,
        )
        if refused is not None:
            status, message = refused
            log.warning(
                "panel request refused",
                extra={"method": request.method, "path": request.url.path, "status": status},
            )
            return JSONResponse({"error": message}, status_code=status)
        return await call_next(request)

    @app.get("/api/csrf", include_in_schema=False)
    async def csrf_token() -> dict[str, str]:
        # Readable only same-origin: no CORS headers, and the Host check above stops
        # DNS rebinding on loopback (an exposed panel is behind its password).
        return {"token": app.state.csrf_token}

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
            "disk": _disk(),
        }

    def _disk() -> dict[str, Any] | None:
        from clipper.retention import disk_usage

        try:
            used, total = disk_usage(data_dir)
        except OSError:
            return None
        pct = round(100.0 * used / total, 1) if total else 0.0
        return {"percent": pct, "high": pct >= cfg.retention.disk_high_percent}

    # ---- setup screen ------------------------------------------------------------------

    def _env_path() -> Path:
        if deps.env_path is None:
            raise PanelError(".env yolu bilinmiyor", 503)
        return deps.env_path

    def _reload_secrets() -> None:
        deps.secrets = load_secrets(_env_path())
        if deps.publish is not None:
            from clipper.publish.oauth import providers
            from clipper.util.crypto import SecretBox

            key = deps.secrets.clipper_secret_key
            deps.publish.oauth.box = SecretBox(key.get_secret_value()) if key else None
            deps.publish.oauth.providers = providers(cfg, deps.secrets)

    @app.get("/api/setup")
    async def setup_status() -> dict[str, Any]:
        sec = deps.secrets
        return {
            "env": setup.env_status(sec),
            "env_path": str(deps.env_path) if deps.env_path else None,
            "first_run": sec.anthropic_api_key is None or sec.clipper_secret_key is None,
            "llm_active": deps.llm_active,
            "restart_needed": deps.restart_needed,
            "can_restart": deps.system is not None,
        }

    @app.post("/api/setup/env")
    async def setup_env(request: Request) -> dict[str, Any]:
        body = await _body(request)
        values = body.get("values")
        if not isinstance(values, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in values.items()
        ):
            raise PanelError("values bir metin sözlüğü olmalı")
        try:
            changed = setup.update_env(_env_path(), values)
        except (setup.SetupError, ValueError) as exc:
            raise PanelError(str(exc)) from exc
        if changed:
            _reload_secrets()
            deps.restart_needed = True
            log.info("env updated", extra={"keys": changed})  # names only, never values
        return {"ok": True, "changed": changed}

    @app.post("/api/setup/secret-key")
    async def setup_secret_key() -> dict[str, Any]:
        n = len(api.list_accounts(conn))
        try:
            created = setup.generate_secret_key(_env_path(), deps.secrets, n)
        except setup.SetupError as exc:
            raise PanelError(str(exc)) from exc
        if created:
            _reload_secrets()
        return {"ok": True, "created": created}

    @app.get("/api/setup/checks")
    async def setup_checks() -> list[dict[str, str]]:
        return await setup.checks(cfg, deps.secrets)

    @app.post("/api/setup/shortcut")
    async def setup_shortcut() -> dict[str, Any]:
        import subprocess

        from clipper import autostart

        env = _env_path()
        config = deps.config_path or (env.parent / "config.yaml")
        plan = autostart.shortcut_plan(env.parent, config, env)
        try:
            await asyncio.to_thread(autostart.install, plan)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise PanelError(f"kısayol oluşturulamadı: {exc}", 500) from exc
        return {"ok": True, "message": plan.hint}

    @app.post("/api/system/{action}")
    async def system_action(action: str) -> dict[str, Any]:
        if action not in ("restart", "shutdown"):
            raise PanelError("bilinmeyen işlem", 404)
        if deps.system is None:
            raise PanelError("bu modda kullanılamıyor", 503)
        deps.system(action)
        return {"ok": True}

    # ---- new VOD import -----------------------------------------------------------------

    def _vod() -> VodTasks:
        if deps.vod is None:
            raise PanelError("VOD ekleme bu modda kapalı", 503)
        return deps.vod

    @app.get("/api/vod")
    async def vod_list() -> list[dict[str, Any]]:
        return _vod().list()

    @app.post("/api/vod")
    async def vod_start(request: Request) -> dict[str, Any]:
        body = await _body(request)
        text = str(body.get("text") or "").strip()
        if not text:
            raise PanelError("link, id ya da dosya yolu gerekli")
        source = body.get("source") or detect_source(text)
        if source not in ("kick", "twitch", "youtube", "file"):
            raise PanelError("platform anlaşılamadı; listeden seç")
        try:
            task = _vod().start(
                source,
                text,
                streamer=(str(body.get("streamer") or "").strip() or None),
                chat=(str(body.get("chat") or "").strip() or None),
            )
        except VodError as exc:
            raise PanelError(str(exc)) from exc
        return task.view()

    @app.get("/api/kick-vods")
    async def kick_vod_list(channel: str) -> list[dict[str, Any]]:
        if not channel.strip():
            raise PanelError("kanal adı gerekli")
        try:
            return await kick_vods(cfg, channel)
        except Exception as exc:  # network / Cloudflare / parse: show it, don't 500
            raise PanelError(f"Kick VOD listesi alınamadı: {exc}", 502) from exc

    # ---- learned model (4.9 Kademe 2) --------------------------------------------------

    @app.get("/api/learn")
    async def learn_status() -> dict[str, Any]:
        from clipper.learn.service import summaries

        return summaries(conn, cfg)

    @app.post("/api/learn/{version}/apply")
    async def learn_apply(version: int) -> dict[str, Any]:
        from clipper.learn.service import LearnError, apply

        try:
            apply(conn, cfg, version)
        except LearnError as exc:
            raise PanelError(str(exc), 404) from exc
        return {"ok": True}

    @app.post("/api/learn/undo")
    async def learn_undo() -> dict[str, Any]:
        from clipper.learn.service import undo

        version = undo(conn)
        if version is None:
            raise PanelError("uygulanmış bir öneri yok")
        return {"ok": True, "version": version}

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
        platforms = [p for p in platforms if p in PLATFORMS]
        kit = deps.publish
        if platforms and not scheduler.permitted(conn, clip_id):
            raise PanelError("bu yayıncı için izin işaretli değil; yayın platformu seçilemez", 403)
        decision = api.approve(conn, clip_id, platforms)
        planned = 0
        if platforms and kit is not None:
            planned = len(scheduler.plan(conn, cfg, clip_id, platforms, kit.accounts))
        return {"ok": True, "decision": decision, "planned": planned}

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

    def _kit() -> PublishKit:
        if deps.publish is None:
            raise PanelError("yayınlama modülü çalışmıyor", 503)
        return deps.publish

    @app.post("/api/publications/{pub_id}/retry")
    async def retry_publication(pub_id: int) -> dict[str, Any]:
        kit = _kit()
        try:
            scheduler.retry(conn, cfg, pub_id, kit.accounts)
        except LookupError as exc:
            raise PanelError(str(exc), 404) from exc
        except scheduler.PermissionDenied as exc:
            raise PanelError(str(exc), 403) from exc
        except ValueError as exc:
            raise PanelError(str(exc)) from exc
        return {"ok": True}

    @app.get("/api/accounts")
    async def accounts() -> dict[str, Any]:
        kit = deps.publish
        configured = sorted(kit.oauth.providers) if kit is not None else []
        return {
            "platforms": [{"platform": p, "configured": p in configured} for p in PLATFORMS],
            "accounts": api.list_accounts(conn),
            "secret_key": kit is not None and kit.oauth.box is not None,
        }

    @app.delete("/api/accounts/{platform}/{account}")
    async def remove_account(platform: str, account: str) -> dict[str, Any]:
        if not _kit().oauth.remove(platform, account):
            raise PanelError("hesap yok", 404)
        return {"ok": True}

    @app.post("/api/auth/{platform}/start")
    async def auth_start(platform: str, request: Request) -> dict[str, Any]:
        if platform not in PLATFORMS:
            raise PanelError("bilinmeyen platform", 404)
        body = await _body(request) if await request.body() else {}
        account = str(body.get("account") or "").strip() or None
        try:
            return {"url": _kit().oauth.start(platform, account)}
        except OAuthError as exc:
            raise PanelError(str(exc)) from exc

    @app.get("/callback/{platform}", include_in_schema=False)
    async def auth_callback(
        platform: str,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> HTMLResponse:
        try:
            account = await _kit().oauth.callback(platform, code, state, error_description or error)
        except (OAuthError, PanelError) as exc:
            return HTMLResponse(_callback_page(f"Bağlanamadı: {exc}", ok=False), status_code=400)
        return HTMLResponse(
            _callback_page(f"{platform} hesabı bağlandı: {account}. Bu sekmeyi kapatabilirsin.")
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
