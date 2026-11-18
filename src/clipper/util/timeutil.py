"""Time helpers. Internally: unix seconds (float) and ISO-8601 UTC strings."""

from __future__ import annotations

import time
from datetime import UTC, datetime


def now_ts() -> float:
    return time.time()


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return to_iso(time.time())


def to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")


def parse_iso(value: str) -> float:
    """Parse ISO-8601 (also with trailing `Z`, as in HLS PROGRAM-DATE-TIME) to unix seconds."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def fmt_duration(seconds: float) -> str:
    """`3725.4` -> `1:02:05`; `65` -> `1:05`."""
    total = round(seconds)
    sign = "-" if total < 0 else ""
    total = abs(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}" if h else f"{sign}{m}:{s:02d}"
