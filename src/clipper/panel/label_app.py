"""Labeling screen (`clipper label <session>`), a separate small app on its own port.

It has no route that returns candidates, clips, signals or scores (TESTPLAN PNL-05), so
what the system found cannot leak to the labeler, even through the browser's dev tools.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from clipper import __version__
from clipper.evals import labels as L
from clipper.panel.app import STATIC_DIR


def create_label_app(
    conn: sqlite3.Connection,
    session_id: int,
    proxy: Path,
    out: Path,
    labeler: str = "",
) -> FastAPI:
    app = FastAPI(title="clipper etiketleme", version=__version__, docs_url=None, redoc_url=None)
    session = L.session_info(conn, session_id)
    ps = [p for _, p in L.pieces(conn, session_id)]
    video_len = sum(p.duration for p in ps)

    @app.exception_handler(L.LabelError)
    async def _label_error(_req: Request, exc: L.LabelError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "label.html")

    @app.get("/static/{name}", include_in_schema=False)
    async def static(name: str) -> Response:
        if name not in ("label.js", "style.css"):
            return JSONResponse({"error": "yok"}, status_code=404)
        return FileResponse(STATIC_DIR / name)

    @app.get("/video.mp4", include_in_schema=False)
    async def video() -> FileResponse:
        return FileResponse(proxy, media_type="video/mp4")

    @app.get("/api/label")
    async def get_label() -> dict[str, Any]:
        doc = L.load(out) or {}
        return {
            "session": {k: session[k] for k in ("id", "title", "category", "streamer")},
            "labeler": labeler,
            "file": str(out),
            "duration": round(video_len, 2),
            "types": list(L.TYPES),
            "labels": [
                {k: x[k] for k in ("video_start", "video_end", "type", "quality", "note")}
                for x in doc.get("labels", [])
            ],
        }

    @app.put("/api/label")
    async def put_label(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError as exc:
            raise L.LabelError("geçersiz JSON") from exc
        items = L.validate(body.get("labels") if isinstance(body, dict) else None, ps, video_len)
        L.save(out, session, labeler, items)
        return {"ok": True, "count": len(items)}

    return app
