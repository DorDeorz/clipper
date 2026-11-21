"""Pinned downloads for optional local tools (face model, deep-filter binary): fetched once
into the data dir and accepted only when the SHA-256 matches."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path

import httpx

from clipper.log import get_logger
from clipper.util.fs import atomic_write_bytes

log = get_logger("download")

_LOCKS: dict[Path, asyncio.Lock] = {}


class DownloadError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


async def ensure_pinned(
    url: str,
    sha256: str,
    dest: Path,
    *,
    executable: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 120,
) -> Path:
    """Return `dest`, downloading it first when missing or when its hash is wrong."""
    lock = _LOCKS.setdefault(dest.resolve(), asyncio.Lock())
    async with lock:
        if dest.is_file() and await asyncio.to_thread(sha256_file, dest) == sha256:
            return dest
        log.info("downloading tool", extra={"url": url, "dest": str(dest)})
        try:
            async with httpx.AsyncClient(
                transport=transport, timeout=timeout, follow_redirects=True
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content
        except httpx.HTTPError as exc:
            raise DownloadError(f"indirilemedi: {url} ({exc})") from exc
        got = hashlib.sha256(data).hexdigest()
        if got != sha256:
            raise DownloadError(f"indirilen dosyanın özeti tutmuyor: {url}")
        await asyncio.to_thread(atomic_write_bytes, dest, data)
        if executable and os.name != "nt":
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return dest
