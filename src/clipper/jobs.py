"""Our own SQLite-backed job queue.

Statuses: queued -> running -> done | failed(retry -> queued) | dead.
Each worker should use its own connection; SQLite serialises writers, and
`claim` is a single `UPDATE ... RETURNING` statement, so two workers can never
take the same job.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

Pool = Literal["cpu", "gpu", "io"]
POOLS: tuple[Pool, ...] = ("cpu", "gpu", "io")

DEFAULT_LEASE_SECONDS = 600.0
BACKOFF_BASE_SECONDS = 10.0
BACKOFF_MAX_SECONDS = 3600.0

Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    type: str
    pool: str
    payload: dict[str, Any]
    status: str
    priority: int
    attempts: int
    max_attempts: int
    run_after: float
    locked_until: float | None
    error: str | None
    created_at: float
    finished_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            type=row["type"],
            pool=row["pool"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            priority=row["priority"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            run_after=row["run_after"],
            locked_until=row["locked_until"],
            error=row["error"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
        )


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff after the `attempts`-th failed attempt (1-based)."""
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS)


def enqueue(
    conn: sqlite3.Connection,
    type: str,
    payload: dict[str, Any],
    pool: Pool,
    priority: int = 100,
    run_after: float | None = None,
    max_attempts: int = 4,
    *,
    clock: Clock = time.time,
) -> int:
    if pool not in POOLS:
        raise ValueError(f"unknown pool: {pool}")
    now = clock()
    cur = conn.execute(
        "INSERT INTO jobs (type, pool, payload, priority, run_after, max_attempts, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            type,
            pool,
            json.dumps(payload, ensure_ascii=False),
            priority,
            now if run_after is None else run_after,
            max_attempts,
            now,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def claim(
    conn: sqlite3.Connection,
    pool: Pool,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    *,
    clock: Clock = time.time,
) -> Job | None:
    """Atomically take the next runnable job of `pool`, or return None."""
    now = clock()
    row = conn.execute(
        """
        UPDATE jobs
           SET status = 'running', locked_until = ?, attempts = attempts + 1
         WHERE id = (
               SELECT id FROM jobs
                WHERE status = 'queued' AND pool = ? AND run_after <= ?
                ORDER BY priority, run_after, id
                LIMIT 1)
           AND status = 'queued'
        RETURNING *
        """,
        (now + lease_seconds, pool, now),
    ).fetchone()
    return Job.from_row(row) if row is not None else None


def heartbeat(
    conn: sqlite3.Connection,
    job_id: int,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    *,
    clock: Clock = time.time,
) -> bool:
    """Extend the lock of a running job. Returns False if the job is no longer ours."""
    cur = conn.execute(
        "UPDATE jobs SET locked_until = ? WHERE id = ? AND status = 'running'",
        (clock() + lease_seconds, job_id),
    )
    return cur.rowcount == 1


def complete(conn: sqlite3.Connection, job_id: int, *, clock: Clock = time.time) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', locked_until = NULL, error = NULL, finished_at = ?"
        " WHERE id = ? AND status = 'running'",
        (clock(), job_id),
    )


def fail(
    conn: sqlite3.Connection,
    job_id: int,
    error: str,
    *,
    retry: bool = True,
    clock: Clock = time.time,
) -> str:
    """Record a failure. Retries with exponential backoff until `max_attempts`,
    then marks the job `dead`. Returns the new status."""
    now = clock()
    row = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    attempts, max_attempts = row["attempts"], row["max_attempts"]
    if retry and attempts < max_attempts:
        conn.execute(
            "UPDATE jobs SET status = 'queued', locked_until = NULL, error = ?, run_after = ?"
            " WHERE id = ?",
            (error, now + backoff_seconds(attempts), job_id),
        )
        return "queued"
    conn.execute(
        "UPDATE jobs SET status = 'dead', locked_until = NULL, error = ?, finished_at = ?"
        " WHERE id = ?",
        (error, now, job_id),
    )
    return "dead"


def recover_stale(conn: sqlite3.Connection, *, clock: Clock = time.time) -> int:
    """Crash recovery: `running` jobs whose lock expired go back to `queued`
    (or `dead` if they already used all attempts). Returns how many were touched."""
    now = clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        dead = conn.execute(
            "UPDATE jobs SET status = 'dead', locked_until = NULL, finished_at = ?,"
            " error = COALESCE(error, 'lock expired')"
            " WHERE status = 'running' AND locked_until < ? AND attempts >= max_attempts",
            (now, now),
        ).rowcount
        requeued = conn.execute(
            # run_after is kept, so recovered jobs keep their place in line.
            "UPDATE jobs SET status = 'queued', locked_until = NULL,"
            " error = 'lock expired (recovered)'"
            " WHERE status = 'running' AND locked_until < ?",
            (now,),
        ).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return dead + requeued


def get(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row is not None else None


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
