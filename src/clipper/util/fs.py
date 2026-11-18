"""Filesystem helpers: atomic writes and disk-usage checks (Windows + Linux)."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory + fsync + `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


@dataclass(frozen=True, slots=True)
class DiskUsage:
    total: int
    used: int
    free: int

    @property
    def used_ratio(self) -> float:
        return self.used / self.total if self.total else 0.0


def disk_usage(path: Path) -> DiskUsage:
    """Usage of the filesystem holding `path` (walks up to an existing parent)."""
    probe = path.resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    u = shutil.disk_usage(probe)
    return DiskUsage(total=u.total, used=u.used, free=u.free)


def is_disk_nearly_full(path: Path, threshold: float = 0.85) -> bool:
    return disk_usage(path).used_ratio >= threshold


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def safe_join(base: Path, *parts: str) -> Path:
    """Join and refuse anything that escapes `base` (path traversal guard)."""
    base_resolved = base.resolve()
    target = base_resolved.joinpath(*parts).resolve()
    if not target.is_relative_to(base_resolved):
        raise ValueError("path escapes base directory")
    return target
