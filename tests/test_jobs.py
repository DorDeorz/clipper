from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from clipper import db, jobs


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_enqueue_and_claim_roundtrip(conn: sqlite3.Connection) -> None:
    jid = jobs.enqueue(conn, "cut", {"candidate_id": 7, "ad": "ğüş"}, "cpu")
    job = jobs.claim(conn, "cpu")
    assert job is not None and job.id == jid
    assert job.status == "running" and job.attempts == 1
    assert job.payload == {"candidate_id": 7, "ad": "ğüş"}
    assert jobs.claim(conn, "cpu") is None
    jobs.complete(conn, jid)
    done = jobs.get(conn, jid)
    assert done is not None and done.status == "done" and done.finished_at is not None


def test_claim_respects_pool_priority_and_run_after(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    jobs.enqueue(conn, "a", {}, "cpu", priority=100, clock=clock)
    hi = jobs.enqueue(conn, "b", {}, "cpu", priority=10, clock=clock)
    later = jobs.enqueue(conn, "c", {}, "cpu", priority=1, run_after=2000, clock=clock)
    jobs.enqueue(conn, "d", {}, "gpu", clock=clock)
    first = jobs.claim(conn, "cpu", clock=clock)
    assert first is not None and first.id == hi
    second = jobs.claim(conn, "cpu", clock=clock)
    assert second is not None and second.type == "a"
    assert jobs.claim(conn, "cpu", clock=clock) is None
    clock.t = 2000
    third = jobs.claim(conn, "cpu", clock=clock)
    assert third is not None and third.id == later


def test_unknown_pool(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        jobs.enqueue(conn, "x", {}, "tpu")  # type: ignore[arg-type]


def test_fail_backoff_then_dead(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    jid = jobs.enqueue(conn, "x", {}, "io", max_attempts=3, clock=clock)
    expected_delays = [10.0, 20.0]
    for delay in expected_delays:
        job = jobs.claim(conn, "io", clock=clock)
        assert job is not None
        assert jobs.fail(conn, jid, "boom", clock=clock) == "queued"
        assert jobs.claim(conn, "io", clock=clock) is None  # still backing off
        clock.t += delay
    job = jobs.claim(conn, "io", clock=clock)
    assert job is not None and job.attempts == 3
    assert jobs.fail(conn, jid, "final", clock=clock) == "dead"
    dead = jobs.get(conn, jid)
    assert dead is not None and dead.status == "dead" and dead.error == "final"


def test_fail_without_retry(conn: sqlite3.Connection) -> None:
    jid = jobs.enqueue(conn, "x", {}, "io")
    assert jobs.claim(conn, "io") is not None
    assert jobs.fail(conn, jid, "4xx", retry=False) == "dead"


def test_backoff_capped() -> None:
    assert jobs.backoff_seconds(1) == 10
    assert jobs.backoff_seconds(4) == 80
    assert jobs.backoff_seconds(100) == jobs.BACKOFF_MAX_SECONDS


def test_crash_recovery(db_path: Path) -> None:
    clock = FakeClock()
    worker = db.connect(db_path)
    a = jobs.enqueue(worker, "render", {}, "cpu", clock=clock)
    b = jobs.enqueue(worker, "render", {}, "cpu", max_attempts=1, clock=clock)
    jobs.enqueue(worker, "render", {}, "cpu", clock=clock)
    assert jobs.claim(worker, "cpu", lease_seconds=60, clock=clock) is not None
    assert jobs.claim(worker, "cpu", lease_seconds=60, clock=clock) is not None
    worker.close()  # the "crash"

    fresh = db.connect(db_path)
    clock.t += 30
    assert jobs.recover_stale(fresh, clock=clock) == 0  # leases still valid
    clock.t += 31
    assert jobs.recover_stale(fresh, clock=clock) == 2
    ja, jb = jobs.get(fresh, a), jobs.get(fresh, b)
    assert ja is not None and ja.status == "queued" and ja.locked_until is None
    assert jb is not None and jb.status == "dead"
    again = jobs.claim(fresh, "cpu", clock=clock)
    assert again is not None and again.id == a and again.attempts == 2
    fresh.close()


def test_heartbeat_extends_lease(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    jid = jobs.enqueue(conn, "x", {}, "gpu", clock=clock)
    jobs.claim(conn, "gpu", lease_seconds=10, clock=clock)
    clock.t += 9
    assert jobs.heartbeat(conn, jid, lease_seconds=10, clock=clock)
    clock.t += 9
    assert jobs.recover_stale(conn, clock=clock) == 0
    jobs.complete(conn, jid, clock=clock)
    assert not jobs.heartbeat(conn, jid, clock=clock)


def test_concurrent_claim_no_double_take(db_path: Path) -> None:
    setup = db.connect(db_path)
    n_jobs, n_workers = 300, 8
    for i in range(n_jobs):
        jobs.enqueue(setup, "t", {"i": i}, "cpu")
    setup.close()

    taken: list[list[int]] = [[] for _ in range(n_workers)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(n_workers)

    def worker(idx: int) -> None:
        c = db.connect(db_path)
        try:
            barrier.wait()
            while (job := jobs.claim(c, "cpu")) is not None:
                taken[idx].append(job.id)
                jobs.complete(c, job.id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    all_ids = [j for lst in taken for j in lst]
    assert len(all_ids) == n_jobs
    assert len(set(all_ids)) == n_jobs
    check = db.connect(db_path)
    assert jobs.counts(check) == {"done": n_jobs}
    check.close()
