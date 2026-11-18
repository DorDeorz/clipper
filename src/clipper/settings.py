"""Runtime settings applied from the panel (stored in the `settings` table, JSON values)."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

MIN_KEEP_SCORE = "llm.min_keep_score"
AUTO_PUBLISH_MIN_SCORE = "publish.auto_publish_min_score"
LEARN_APPLIED = "learn.applied"


def get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row is not None else default


def put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), time.time()),
    )


def delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def number(conn: sqlite3.Connection, key: str, default: float) -> float:
    v = get(conn, key)
    return float(v) if isinstance(v, int | float) else default
