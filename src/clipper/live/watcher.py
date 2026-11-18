"""LiveWatcher (ROADMAP 3.1): every `check_every_seconds`, ask each platform which enabled
streamers are live; open a LiveSession for a stream that just started, and close one whose
stream stayed offline for `offline_grace_seconds`."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from clipper.config import Config
from clipper.live.session import LiveSession, StreamSource
from clipper.log import get_logger

log = get_logger("live.watcher")

SessionTask = Callable[[LiveSession, asyncio.Event], Awaitable[None]]


@dataclass
class Running:
    session: LiveSession
    task: asyncio.Task[Any]
    stop: asyncio.Event
    offline_since: float | None = None


@dataclass
class WatchState:
    checks: int = 0
    live: dict[int, bool] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


Prober = Callable[[list[sqlite3.Row]], Awaitable[dict[int, StreamSource | None]]]


class LiveWatcher:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        client: httpx.AsyncClient,
        probe: Prober,
        *,
        extra_tasks: Callable[[], list[SessionTask]] | None = None,
        clock: Callable[[], float] = time.time,
        session_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.client = client
        self.probe = probe
        self.extra_tasks = extra_tasks or (lambda: [])
        self.clock = clock
        self.session_kwargs = session_kwargs or {}
        self.running: dict[int, Running] = {}
        self.state = WatchState()

    def streamers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM streamers WHERE enabled = 1 ORDER BY id").fetchall()

    async def check(self) -> None:
        """One round: probe, start new sessions, close long-offline ones."""
        self.state.checks += 1
        rows = self.streamers()
        try:
            found = await self.probe(rows)
        except Exception as exc:  # one bad round must not kill the watcher
            log.warning("live probe failed", extra={"error": str(exc)})
            self.state.errors["probe"] = str(exc)
            return
        now = self.clock()
        for row in rows:
            sid = int(row["id"])
            src = found.get(sid)
            self.state.live[sid] = src is not None
            run = self.running.get(sid)
            if run is not None and run.task.done():
                self.running.pop(sid)
                run = None
            if src is not None:
                if run is not None:
                    run.offline_since = None
                    if src.viewers is not None:
                        run.session.source.viewers = src.viewers
                elif len(self.running) < self.cfg.monitoring.max_concurrent_sessions:
                    self._start(row, src)
                else:
                    log.warning("session limit reached", extra={"streamer": row["display_name"]})
            elif run is not None and sid in found:  # probed and offline
                run.offline_since = run.offline_since or now
                if now - run.offline_since >= self.cfg.monitoring.offline_grace_seconds:
                    log.info("stream offline, closing", extra={"streamer": row["display_name"]})
                    run.stop.set()

    def _start(self, row: sqlite3.Row, src: StreamSource) -> None:
        sess = LiveSession(
            self.conn,
            self.cfg,
            row,
            src,
            self.client,
            clock=self.clock,
            extra_tasks=self.extra_tasks(),
            **self.session_kwargs,
        )
        sess.open()
        stop = asyncio.Event()
        task = asyncio.create_task(sess.run(stop), name=f"live:{row['display_name']}")
        self.running[int(row["id"])] = Running(sess, task, stop)
        log.info(
            "live session started",
            extra={"streamer": row["display_name"], "session_id": sess.session_id},
        )

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                await self.check()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.cfg.monitoring.check_every_seconds)
        finally:
            for r in self.running.values():
                r.stop.set()
            if self.running:
                await asyncio.gather(
                    *(r.task for r in self.running.values()), return_exceptions=True
                )
