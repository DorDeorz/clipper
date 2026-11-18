from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from clipper import db

TABLES = {
    "streamers",
    "sessions",
    "segments",
    "signal_seconds",
    "candidates",
    "clips",
    "publications",
    "jobs",
    "oauth_tokens",
    "llm_usage",
    "schema_version",
}


def test_schema_created(conn: sqlite3.Connection) -> None:
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= TABLES
    assert db.schema_version(conn) == len(db.MIGRATIONS)


def test_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_idempotent(db_path: Path) -> None:
    c = db.connect(db_path)
    assert db.migrate(c) == len(db.MIGRATIONS)
    assert db.migrate(c) == len(db.MIGRATIONS)
    rows = c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert rows == len(db.MIGRATIONS)


def test_new_migration_applied_and_failed_one_rolled_back(tmp_path: Path) -> None:
    c = db.connect(tmp_path / "m.db")
    extra = (*db.MIGRATIONS, "CREATE TABLE extra (x INTEGER);")
    assert db.migrate(c, extra) == len(extra)
    broken = (*extra, "CREATE TABLE ok2 (x INTEGER);\nCREATE TABLE broken (;")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(c, broken)
    assert db.schema_version(c) == len(extra)
    names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master")}
    assert "ok2" not in names


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions (streamer_id, source, started_at, dir, status)"
            " VALUES (999, 'vod', 'x', 'd', 'ended')"
        )


def test_platform_check(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO streamers (display_name, platform, channel, created_at)"
            " VALUES ('a', 'myspace', 'a', 'x')"
        )
