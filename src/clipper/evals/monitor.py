"""Resource monitor for eval runs: one CSV row every `interval` seconds.

Columns: wall time, RSS (MB), CPU % (this process + finished children since the last
sample), GPU VRAM used (MB, via nvidia-smi when present), free disk (GB), open file
handles, and job counts per pool/status. Standard library only (no psutil).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import os
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipper.util.proc import no_window

FIELDS = [
    "t",
    "rss_mb",
    "cpu_pct",
    "gpu_mem_mb",
    "disk_free_gb",
    "open_files",
    "queued",
    "running",
    "dead",
    "queued_cpu",
    "queued_gpu",
    "queued_io",
]


def rss_mb() -> float | None:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        ok = psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return round(pmc.WorkingSetSize / 2**20, 1) if ok else None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # macOS fallback: peak
        return round(peak / (2**20 if sys.platform == "darwin" else 1024), 1)
    except (ImportError, OSError):
        return None


def open_files() -> int | None:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        n = wintypes.DWORD()
        if k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n)):
            return int(n.value)
        return None
    for d in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(d))
        except OSError:
            continue
    return None


def gpu_mem_mb(nvidia_smi: str | None) -> float | None:
    if not nvidia_smi:
        return None
    import subprocess

    try:
        out = subprocess.run(
            [nvidia_smi, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            **no_window(),
        ).stdout
        return float(sum(float(x) for x in out.split() if x.strip()))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def queue_counts(db_path: Path | None) -> dict[str, int]:
    keys = {k: 0 for k in FIELDS[6:]}
    if db_path is None or not db_path.exists():
        return keys
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=1)
        try:
            rows = conn.execute(
                "SELECT status, pool, COUNT(*) FROM jobs GROUP BY status, pool"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return keys
    for status, pool, n in rows:
        if status in ("queued", "running", "dead"):
            keys[status] += n
        if status == "queued" and f"queued_{pool}" in keys:
            keys[f"queued_{pool}"] += n
    return keys


class ResourceMonitor:
    """`async with ResourceMonitor(csv_path, disk=..., db=lambda: path):` samples in the
    background. `db` is a callable so a multi-scenario run can point it at the current DB."""

    def __init__(
        self,
        out: Path,
        *,
        disk: Path,
        db: Callable[[], Path | None] = lambda: None,
        interval: float = 10.0,
    ) -> None:
        self.out = out
        self.disk = disk
        self.db = db
        self.interval = interval
        self.nvidia_smi = shutil.which("nvidia-smi")
        self.rows = 0
        self._task: asyncio.Task[None] | None = None
        self._last: tuple[float, float] | None = None

    def _cpu_pct(self) -> float | None:
        t = os.times()
        cpu = t.user + t.system + t.children_user + t.children_system
        now = time.monotonic()
        prev, self._last = self._last, (now, cpu)
        if prev is None or now <= prev[0]:
            return None
        return round(100 * (cpu - prev[1]) / (now - prev[0]), 1)

    def sample(self) -> dict[str, Any]:
        try:
            free = round(shutil.disk_usage(self.disk).free / 2**30, 2)
        except OSError:
            free = None
        return {
            "t": round(time.time(), 1),
            "rss_mb": rss_mb(),
            "cpu_pct": self._cpu_pct(),
            "gpu_mem_mb": gpu_mem_mb(self.nvidia_smi),
            "disk_free_gb": free,
            "open_files": open_files(),
            **queue_counts(self.db()),
        }

    def _write(self, row: dict[str, Any]) -> None:
        new = not self.out.exists()
        self.out.parent.mkdir(parents=True, exist_ok=True)
        with self.out.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: "" if v is None else v for k, v in row.items()})
        self.rows += 1

    async def _loop(self) -> None:
        while True:
            row = await asyncio.to_thread(self.sample)
            await asyncio.to_thread(self._write, row)
            await asyncio.sleep(self.interval)

    async def __aenter__(self) -> ResourceMonitor:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await asyncio.to_thread(self._write, self.sample())


def summarize(path: Path) -> dict[str, float | None]:
    """Peaks for the report: max RSS, max CPU %, max VRAM, min free disk, max handles."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    def col(name: str) -> list[float]:
        return [float(r[name]) for r in rows if r.get(name)]

    def pick(name: str, fn: Callable[[list[float]], float]) -> float | None:
        v = col(name)
        return fn(v) if v else None

    return {
        "samples": float(len(rows)),
        "rss_mb_max": pick("rss_mb", max),
        "cpu_pct_max": pick("cpu_pct", max),
        "gpu_mem_mb_max": pick("gpu_mem_mb", max),
        "disk_free_gb_min": pick("disk_free_gb", min),
        "open_files_max": pick("open_files", max),
    }
