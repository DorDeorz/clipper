"""JSON-lines logging to a daily-rotated file plus an in-memory ring buffer."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RING_SIZE = 500
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

# Defensive redaction: anything that looks like a credential never reaches the log.
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(bearer|oauth)\s+[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(
        r"(?i)((?:access|refresh)_token|client_secret|api_key|password)([\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]+"
    ),
)


def redact(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub("***", text)
    text = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)} ***", text)
    return _SECRET_PATTERNS[2].sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


class RingBufferHandler(logging.Handler):
    """Keeps the last `capacity` formatted lines in memory (for the panel)."""

    def __init__(self, capacity: int = RING_SIZE) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=capacity)
        self._lock_ring = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock_ring:
            self._lines.append(line)

    def lines(self) -> list[str]:
        with self._lock_ring:
            return list(self._lines)


_ring = RingBufferHandler()


def ring_buffer() -> RingBufferHandler:
    return _ring


def setup_logging(log_dir: Path | None, level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger("clipper")
    root.setLevel(level)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    formatter = JsonFormatter()
    _ring.setFormatter(formatter)
    root.addHandler(_ring)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_dir / "clipper.jsonl", when="midnight", backupCount=14, encoding="utf-8", utc=True
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"clipper.{name}" if not name.startswith("clipper") else name)
