"""`clipper run`: panel + continuous job workers in one asyncio process (ROADMAP 3.1).

Live watching (LiveWatcher, recorder, chat clients) joins this supervisor in Faz 6.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sqlite3
import sys
import threading
import webbrowser
from collections.abc import Iterator
from typing import Any

import httpx

from clipper import db
from clipper.config import Config, Secrets
from clipper.log import get_logger
from clipper.panel.app import PanelDeps, check_bind, create_app

log = get_logger("run")


def make_llm(conn: sqlite3.Connection, cfg: Config, secrets: Secrets) -> Any | None:
    if secrets.anthropic_api_key is None:
        return None
    import anthropic

    from clipper.llm.client import LLMClient

    return LLMClient(
        conn,
        cfg.llm,
        anthropic.AsyncAnthropic(api_key=secrets.anthropic_api_key.get_secret_value()),
    )


def facecam_suggester(conn: sqlite3.Connection, cfg: Config, llm: Any) -> Any:
    from clipper.llm.facecam_vision import grab_frames, preview, suggest_facecam
    from clipper.media import probe
    from clipper.panel import api
    from clipper.panel.api import PanelError

    async def suggest(streamer_id: int) -> dict[str, Any]:
        video = api.streamer_video(conn, streamer_id)
        if video is None:
            raise PanelError("bu yayıncının kaydedilmiş videosu yok", 404)
        info = await probe(video, cfg.paths.ffprobe)
        work = cfg.paths.data_dir / "facecam" / str(streamer_id)
        frames = await grab_frames(
            cfg.paths.ffmpeg, video, [info.duration * f for f in (0.2, 0.45, 0.7)], work
        )
        if not frames:
            raise PanelError("videodan kare alınamadı", 500)
        sug = await suggest_facecam(llm, frames, cfg.llm.judge_model)
        out: dict[str, Any] = {
            "found": sug.rect is not None,
            "confidence": sug.confidence,
            "reasons": sug.reasons,
            "usd": sug.usd,
        }
        if sug.rect is not None:
            await preview(cfg.paths.ffmpeg, frames[0], sug.rect, work / "preview.jpg")
            out["rect"] = {"x": sug.rect.x, "y": sug.rect.y, "w": sug.rect.w, "h": sug.rect.h}
            out["preview"] = f"facecam/{streamer_id}/preview.jpg"
        return out

    return suggest


def _quiet_server_cls() -> Any:
    import uvicorn

    class QuietServer(uvicorn.Server):
        """Ctrl+C stops the panel and then the workers cleanly. uvicorn's own capture
        re-raises the signal after shutdown, which would skip our cleanup."""

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            if threading.current_thread() is not threading.main_thread():
                yield
                return
            sigs = [signal.SIGINT, signal.SIGTERM]
            if sys.platform == "win32":
                sigs.append(signal.SIGBREAK)
            old = {s: signal.signal(s, self.handle_exit) for s in sigs}
            try:
                yield
            finally:
                for s, h in old.items():
                    signal.signal(s, h)

    return QuietServer


def live_extra_tasks(cfg: Config, llm: Any, live_transcriber: Any) -> Any:
    """Content path tasks for each live session (ROADMAP 4.3b), when available."""
    from clipper.content.scanner import ContentScanner
    from clipper.live.content import live_scanner_task, live_transcriber_task

    def factory() -> list[Any]:
        tasks: list[Any] = []
        if live_transcriber is not None:
            tasks.append(live_transcriber_task(live_transcriber))
            if llm is not None and cfg.content_scan.enabled:
                tasks.append(
                    live_scanner_task(
                        lambda s: ContentScanner(
                            cfg.content_scan,
                            llm,
                            streamer=s.streamer["display_name"],
                            category=s.source.category,
                        )
                    )
                )
        return tasks

    return factory


async def run(
    cfg: Config, secrets: Secrets, *, open_browser: bool = True, live: bool = True
) -> int:
    import uvicorn

    from clipper.stages import Stages
    from clipper.transcribe.whisper import Transcriber
    from clipper.worker import run_forever

    check_bind(cfg, secrets)
    server_cls = _quiet_server_cls()
    conn = db.open_db(cfg.db_path, check_same_thread=False)
    llm = make_llm(conn, cfg, secrets)
    pools: tuple[Any, ...] = ("cpu", "gpu", "io") if llm is not None else ("cpu", "gpu")
    if llm is None:
        print("Uyarı: ANTHROPIC_API_KEY yok; LLM işleri beklemede kalacak.", file=sys.stderr)
    transcriber = Transcriber(cfg.transcribe, cfg.model_dir)
    stages = Stages(conn, cfg, transcriber=transcriber, llm=llm)
    deps = PanelDeps(
        conn, cfg, secrets, facecam_suggester(conn, cfg, llm) if llm is not None else None
    )
    app = create_app(deps)
    server = server_cls(
        uvicorn.Config(app, host=cfg.panel.host, port=cfg.panel.port, log_level="warning")
    )
    url = f"http://{cfg.panel.host}:{cfg.panel.port}/"
    stop = asyncio.Event()

    async def announce() -> None:
        while not server.started:
            if stop.is_set():
                return
            await asyncio.sleep(0.1)
        print(f"Panel: {url}  (durdurmak için Ctrl+C)", file=sys.stderr, flush=True)
        log.info("panel started", extra={"url": url})
        if open_browser:
            await asyncio.to_thread(webbrowser.open, url)

    async def serve() -> None:
        try:
            await server.serve()
        finally:
            stop.set()

    workers = asyncio.create_task(run_forever(conn, stages.handle, stop, pools=pools))
    live_transcriber = None
    watcher_task: asyncio.Task[None] | None = None
    client = httpx.AsyncClient(timeout=30, follow_redirects=True)
    if live:
        from clipper.live.sources import make_prober
        from clipper.live.watcher import LiveWatcher

        if cfg.live_transcribe.enabled:
            live_transcriber = Transcriber(
                cfg.transcribe.model_copy(
                    update={
                        "model": cfg.live_transcribe.model,
                        "beam_size": cfg.live_transcribe.beam_size,
                    }
                ),
                cfg.model_dir,
            )
        watcher = LiveWatcher(
            conn,
            cfg,
            client,
            make_prober(cfg, client),
            extra_tasks=live_extra_tasks(cfg, llm, live_transcriber),
        )
        watcher_task = asyncio.create_task(watcher.run(stop), name="watcher")
    announcer = asyncio.create_task(announce())
    try:
        await serve()
    finally:
        stop.set()
        announcer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await announcer
        if watcher_task is not None:
            await asyncio.gather(watcher_task, return_exceptions=True)
        stats = await workers
        await client.aclose()
        transcriber.close()
        if live_transcriber is not None:
            live_transcriber.close()
        conn.close()
    print(f"Durduruldu. Bu oturumda {stats.done} iş bitti, {stats.failed} hata.", file=sys.stderr)
    return 0
