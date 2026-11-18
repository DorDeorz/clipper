"""Job workers: one asyncio task group per pool, claiming from the SQLite queue.

Pools: cpu (ffmpeg, N=2), gpu (whisper, N=1), io (LLM/upload, N=4). All share one sqlite3
connection; that is safe because every statement runs on the event-loop thread.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from clipper import jobs
from clipper.jobs import Job, Pool
from clipper.log import get_logger

log = get_logger("worker")

Handler = Callable[[Job], Awaitable[None]]
DEFAULT_CONCURRENCY: dict[Pool, int] = {"cpu": 2, "gpu": 1, "io": 4, "pub": 2}
HEARTBEAT_SECONDS = 60.0


@dataclass
class WorkStats:
    done: int = 0
    failed: int = 0
    dead: int = 0
    by_type: dict[str, int] = field(default_factory=dict)


def _retryable(exc: BaseException) -> bool:
    return bool(getattr(exc, "retry", True))


async def _run_one(conn: sqlite3.Connection, job: Job, handler: Handler, stats: WorkStats) -> None:
    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            jobs.heartbeat(conn, job.id)

    hb = asyncio.create_task(beat())
    try:
        await handler(job)
    except asyncio.CancelledError:
        jobs.fail(conn, job.id, "iptal edildi")
        raise
    except Exception as exc:
        status = jobs.fail(conn, job.id, f"{type(exc).__name__}: {exc}", retry=_retryable(exc))
        stats.failed += 1
        if status == "dead":
            stats.dead += 1
        log.warning(
            "job failed",
            extra={"job_id": job.id, "type": job.type, "status": status, "error": str(exc)},
        )
    else:
        jobs.complete(conn, job.id)
        stats.done += 1
        stats.by_type[job.type] = stats.by_type.get(job.type, 0) + 1
    finally:
        hb.cancel()


def _pending(conn: sqlite3.Connection, pools: tuple[Pool, ...], now: float) -> bool:
    marks = ",".join("?" * len(pools))
    row = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE pool IN ({marks}) AND"
        " (status = 'running' OR (status = 'queued' AND run_after <= ?))",
        (*pools, now),
    ).fetchone()
    return int(row[0]) > 0


async def run_until_idle(
    conn: sqlite3.Connection,
    handler: Handler,
    *,
    pools: tuple[Pool, ...] = ("cpu", "gpu", "io"),
    concurrency: dict[Pool, int] | None = None,
    poll: float = 0.2,
    on_progress: Callable[[WorkStats], None] | None = None,
) -> WorkStats:
    """Process jobs of `pools` until none is runnable or running. Jobs waiting on a
    future `run_after` (backoff) are left for a later run."""
    conc = {**DEFAULT_CONCURRENCY, **(concurrency or {})}
    jobs.recover_stale(conn)
    stats = WorkStats()
    running: dict[Pool, set[asyncio.Task[None]]] = {p: set() for p in pools}
    try:
        while True:
            started = False
            for pool in pools:
                running[pool] = {t for t in running[pool] if not t.done()}
                while len(running[pool]) < conc[pool]:
                    job = jobs.claim(conn, pool)
                    if job is None:
                        break
                    running[pool].add(asyncio.create_task(_run_one(conn, job, handler, stats)))
                    started = True
            active = [t for ts in running.values() for t in ts if not t.done()]
            if not active and not started and not _pending(conn, pools, time.time()):
                break
            if active:
                await asyncio.wait(active, timeout=poll, return_when=asyncio.FIRST_COMPLETED)
                if on_progress:
                    on_progress(stats)
            else:
                await asyncio.sleep(poll)
    finally:
        leftovers = [t for ts in running.values() for t in ts if not t.done()]
        for t in leftovers:
            t.cancel()
        if leftovers:
            await asyncio.gather(*leftovers, return_exceptions=True)
    return stats


async def run_forever(
    conn: sqlite3.Connection,
    handler: Handler,
    stop: asyncio.Event,
    *,
    pools: tuple[Pool, ...] = ("cpu", "gpu", "io"),
    concurrency: dict[Pool, int] | None = None,
    poll: float = 1.0,
    recover_every: float = 300.0,
) -> WorkStats:
    """`clipper run` workers: claim jobs until `stop` is set. Running jobs are cancelled on
    stop and go back to the queue (their `fail` marks them retryable)."""
    conc = {**DEFAULT_CONCURRENCY, **(concurrency or {})}
    jobs.recover_stale(conn)
    last_recover = time.monotonic()
    stats = WorkStats()
    running: dict[Pool, set[asyncio.Task[None]]] = {p: set() for p in pools}
    stopper = asyncio.create_task(stop.wait())
    try:
        while not stop.is_set():
            for pool in pools:
                running[pool] = {t for t in running[pool] if not t.done()}
                while len(running[pool]) < conc[pool]:
                    job = jobs.claim(conn, pool)
                    if job is None:
                        break
                    running[pool].add(asyncio.create_task(_run_one(conn, job, handler, stats)))
            if time.monotonic() - last_recover > recover_every:
                jobs.recover_stale(conn)
                last_recover = time.monotonic()
            active = [t for ts in running.values() for t in ts if not t.done()]
            await asyncio.wait(
                [*active, stopper], timeout=poll, return_when=asyncio.FIRST_COMPLETED
            )
    finally:
        stopper.cancel()
        leftovers = [t for ts in running.values() for t in ts if not t.done()]
        for t in leftovers:
            t.cancel()
        if leftovers:
            await asyncio.gather(*leftovers, return_exceptions=True)
    return stats
