"""Panel "Yeni yayın": the `clipper vod` flow as a background task with progress.

Each import runs in its own thread with its own event loop and sqlite connection, so
downloads and detection never block the panel. Progress lines are kept in memory.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from clipper import db
from clipper.config import Config
from clipper.log import get_logger

log = get_logger("panel.vod")

Source = Literal["kick", "twitch", "youtube", "file"]
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_TASKS = 30


class VodError(Exception):
    pass


def detect_source(text: str) -> Source | None:
    t = text.strip().lower()
    if "kick.com" in t or UUID_RE.fullmatch(t):
        return "kick"
    if "twitch.tv" in t or t.isdigit():
        return "twitch"
    if "youtu" in t or YT_ID_RE.fullmatch(text.strip()):
        return "youtube"
    return None


KICK_SLUG_RE = re.compile(r"kick\.com/([A-Za-z0-9_-]+)/videos/", re.IGNORECASE)


def kick_slug(text: str) -> str | None:
    """Channel slug from a kick.com/<slug>/videos/<id> link."""
    m = KICK_SLUG_RE.search(text)
    return m.group(1).lower() if m else None


def parse_ref(source: Source, text: str) -> str:
    """VOD id from a link or a bare id."""
    t = text.strip()
    if source == "kick":
        m = UUID_RE.search(t)
        if m:
            return m.group(0).lower()
        raise VodError("Kick VOD linki/uuid'si tanınmadı (kanal adı yazıp listeden seç)")
    if source == "twitch":
        m = re.search(r"twitch\.tv/videos/(\d+)", t) or re.fullmatch(r"v?(\d+)", t)
        if m:
            return m.group(1)
        raise VodError("Twitch VOD linki tanınmadı (örnek: https://www.twitch.tv/videos/123)")
    if source == "youtube":
        for pat in (r"[?&]v=([A-Za-z0-9_-]{11})", r"youtu\.be/([A-Za-z0-9_-]{11})",
                    r"/(?:live|shorts|embed)/([A-Za-z0-9_-]{11})"):  # fmt: skip
            m = re.search(pat, t)
            if m:
                return m.group(1)
        if YT_ID_RE.fullmatch(t):
            return t
        raise VodError("YouTube linki tanınmadı")
    return t


@dataclass
class VodTask:
    id: int
    source: Source
    ref: str
    status: Literal["running", "done", "failed"] = "running"
    lines: list[str] = field(default_factory=list)
    session_id: int | None = None
    candidates: int | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def say(self, msg: str) -> None:
        # "segment 12/340" style progress replaces the previous progress line.
        if self.lines and msg.startswith("  ") and self.lines[-1].startswith("  "):
            self.lines[-1] = msg
        else:
            self.lines.append(msg)
        del self.lines[:-50]

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "ref": self.ref, "status": self.status,
            "lines": self.lines[-8:], "session_id": self.session_id,
            "candidates": self.candidates, "error": self.error, "started": self.started,
            "finished": self.finished,
        }  # fmt: skip


Runner = Callable[[VodTask], Awaitable[None]]


class VodTasks:
    def __init__(self, cfg: Config, *, runner: Runner | None = None) -> None:
        self.cfg = cfg
        self.tasks: dict[int, VodTask] = {}
        self._next = 1
        self._runner = runner or self._ingest
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        return [t.view() for t in sorted(self.tasks.values(), key=lambda t: -t.id)]

    def start(
        self,
        source: Source,
        text: str,
        *,
        streamer: str | None = None,
        chat: str | None = None,
    ) -> VodTask:
        ref = parse_ref(source, text)
        if source == "file":
            if not Path(ref).is_file():
                raise VodError(f"dosya bulunamadı: {ref}")
            if not streamer:
                raise VodError("dosya için yayıncı adı gerekli")
        if any(t.status == "running" and t.ref == ref for t in self.tasks.values()):
            raise VodError("bu yayın zaten işleniyor")
        with self._lock:
            task = VodTask(self._next, source, ref)
            self._next += 1
            self.tasks[task.id] = task
            for old in sorted(self.tasks)[:-MAX_TASKS]:
                if self.tasks[old].status != "running":
                    del self.tasks[old]
        task.lines.append("Başladı")
        extra = {"streamer": streamer, "chat": chat}
        if source == "kick":
            extra["slug"] = kick_slug(text)
        threading.Thread(
            target=self._thread, args=(task, extra), name=f"vod-{task.id}", daemon=True
        ).start()
        return task

    def _thread(self, task: VodTask, extra: dict[str, Any]) -> None:
        try:
            asyncio.run(self._runner_with(task, extra))
            task.status = "done"
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc) or type(exc).__name__
            log.warning("vod import failed", extra={"task": task.id, "error": task.error})
        finally:
            task.finished = time.time()

    async def _runner_with(self, task: VodTask, extra: dict[str, Any]) -> None:
        if self._runner is self._ingest:
            await self._ingest(task, **extra)
        else:
            await self._runner(task)

    async def _ingest(
        self,
        task: VodTask,
        streamer: str | None = None,
        chat: str | None = None,
        slug: str | None = None,
    ) -> None:
        from clipper import ingest
        from clipper.content.batch import enqueue_content_path
        from clipper.detect.pipeline import analyze_session

        cfg = self.cfg
        conn = db.open_db(cfg.db_path)
        try:
            if task.source == "file":
                assert streamer
                result = await ingest.ingest_local(
                    conn, cfg.paths.data_dir, Path(task.ref), Path(chat) if chat else None,
                    streamer, ffprobe=cfg.paths.ffprobe,
                )  # fmt: skip
            else:
                async with httpx.AsyncClient(http2=False) as client:
                    if task.source == "kick":
                        result = await ingest.ingest_kick_vod(
                            conn, cfg, client, task.ref, say=task.say, slug=slug
                        )
                    else:
                        fn = {
                            "twitch": ingest.ingest_twitch_vod,
                            "youtube": ingest.ingest_youtube_vod,
                        }[task.source]
                        result = await fn(conn, cfg, client, task.ref, say=task.say)
            task.session_id = result.session_id
            task.say(f"Video ve chat hazır ({result.chat_count} mesaj). Anlar aranıyor...")
            res = await analyze_session(conn, cfg, result.session_id, with_audio=True)
            task.candidates = len(res.candidate_ids)
            enqueue_content_path(conn, cfg, result.session_id)
            task.say(
                f"{task.candidates} aday bulundu. Kesim, transkript ve puanlama arka planda"
                " sürüyor; hazır olanlar Klipler sekmesine düşer."
            )
        finally:
            conn.close()


async def kick_vods(cfg: Config, slug: str) -> list[dict[str, Any]]:
    from clipper import ingest

    async with httpx.AsyncClient() as client:
        videos = await ingest.make_kick_api(cfg, client).videos(slug.strip().lower())
    out = []
    for v in videos:
        if v.get("is_live") or not v.get("duration"):
            continue
        cat = ((v.get("categories") or [{}])[0] or {}).get("name", "")
        out.append(
            {
                "uuid": (v.get("video") or {}).get("uuid"),
                "title": v.get("session_title") or "",
                "duration": float(v["duration"]) / 1000.0,
                "start_time": v.get("start_time") or "",
                "category": cat,
            }
        )
    return [v for v in out if v["uuid"]]
