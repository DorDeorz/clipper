"""SQLite connection (WAL, foreign keys) and versioned migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

SCHEMA_V1 = """
CREATE TABLE streamers (
  id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  platform TEXT NOT NULL CHECK (platform IN ('twitch','kick','youtube')),
  channel TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  layout TEXT NOT NULL DEFAULT 'stack',
  facecam_rect TEXT,
  keywords TEXT,
  detector_overrides TEXT,
  publish_targets TEXT,
  auto_publish INTEGER NOT NULL DEFAULT 0,
  permission_ok INTEGER NOT NULL DEFAULT 0,
  permission_note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE sessions (
  id INTEGER PRIMARY KEY,
  streamer_id INTEGER NOT NULL REFERENCES streamers(id),
  source TEXT NOT NULL,
  title TEXT, category TEXT,
  started_at TEXT NOT NULL, ended_at TEXT,
  dir TEXT NOT NULL,
  status TEXT NOT NULL
);

CREATE TABLE segments (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  seq INTEGER NOT NULL,
  path TEXT NOT NULL,
  pdt_start REAL NOT NULL,
  duration REAL NOT NULL,
  is_ad INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(session_id, seq)
);

CREATE TABLE signal_seconds (
  session_id INTEGER NOT NULL, t INTEGER NOT NULL,
  msgs INTEGER, uniq INTEGER, emote REAL, kw REAL, audio_db REAL, score REAL,
  PRIMARY KEY (session_id, t)
);

CREATE TABLE candidates (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  peak_ts REAL NOT NULL, win_start REAL NOT NULL, win_end REAL NOT NULL,
  det_score REAL NOT NULL, features TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE clips (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL UNIQUE REFERENCES candidates(id),
  raw_path TEXT, transcript_path TEXT, llm_json TEXT,
  llm_score INTEGER, start_rel REAL, end_rel REAL,
  title TEXT, final_path TEXT, thumb_path TEXT,
  review TEXT NOT NULL DEFAULT 'pending',
  reviewer_note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE publications (
  id INTEGER PRIMARY KEY,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  platform TEXT NOT NULL, account TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'direct',
  status TEXT NOT NULL,
  remote_id TEXT, url TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  scheduled_at TEXT, published_at TEXT,
  UNIQUE(clip_id, platform, account)
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL, pool TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  priority INTEGER NOT NULL DEFAULT 100,
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 4,
  run_after REAL NOT NULL, locked_until REAL, error TEXT,
  created_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX jobs_pick ON jobs(status, pool, run_after, priority);

CREATE TABLE oauth_tokens (
  platform TEXT NOT NULL, account TEXT NOT NULL,
  data_enc BLOB NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (platform, account)
);

CREATE TABLE llm_usage (
  id INTEGER PRIMARY KEY, ts REAL, model TEXT, purpose TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, usd REAL
);
"""

# r3: multi-signal detection (candidate sources, platform events, content scans),
# feedback logging and published clip stats.
SCHEMA_V2 = """
ALTER TABLE candidates ADD COLUMN sources TEXT NOT NULL DEFAULT '["reactive"]';
ALTER TABLE candidates ADD COLUMN content_score REAL;
ALTER TABLE candidates ADD COLUMN content_reason TEXT;

CREATE TABLE live_events (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  ts REAL NOT NULL, kind TEXT NOT NULL,
  amount REAL, data TEXT
);
CREATE INDEX live_events_ts ON live_events(session_id, ts);

CREATE TABLE content_scans (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  win_start REAL NOT NULL, win_end REAL NOT NULL,
  score INTEGER, kind TEXT, reason TEXT, created_at REAL NOT NULL
);

CREATE TABLE feedback (
  id INTEGER PRIMARY KEY,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  decision TEXT NOT NULL,
  reason TEXT,
  start_delta REAL, end_delta REAL,
  title_edited INTEGER NOT NULL DEFAULT 0,
  features_snapshot TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE clip_stats (
  publication_id INTEGER NOT NULL REFERENCES publications(id),
  fetched_at REAL NOT NULL, views INTEGER, likes INTEGER, comments INTEGER,
  PRIMARY KEY (publication_id, fetched_at)
);
"""

# Faz 7: resumable publishing state and a numeric schedule time for the queue.
SCHEMA_V3 = """
ALTER TABLE publications ADD COLUMN state TEXT;
ALTER TABLE publications ADD COLUMN run_at REAL;
ALTER TABLE publications ADD COLUMN created_at REAL;
"""

# Faz 8: small key/value store for values applied from the panel (learned model).
SCHEMA_V4 = """
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""

# Append new migrations to the end; never edit an applied one.
MIGRATIONS: Sequence[str] = (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4)


def connect(
    path: Path | str, *, timeout: float = 30.0, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Open a connection with WAL journaling, foreign keys and Row factory.

    Connections run in autocommit mode (`isolation_level=None`); use explicit
    `BEGIN IMMEDIATE` / `COMMIT` for multi-statement transactions.
    """
    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), timeout=timeout, isolation_level=None, check_same_thread=check_same_thread
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def migrate(conn: sqlite3.Connection, migrations: Sequence[str] = MIGRATIONS) -> int:
    """Apply pending migrations atomically. Returns the resulting schema version."""
    current = schema_version(conn)
    for version, script in enumerate(migrations, start=1):
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript would COMMIT implicitly; run statements one by one instead.
            for stmt in _split_sql(script):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        current = version
    return current


def open_db(path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = connect(path, check_same_thread=check_same_thread)
    migrate(conn)
    return conn


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        stripped = line.split("--", 1)[0]
        buf += stripped
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements
