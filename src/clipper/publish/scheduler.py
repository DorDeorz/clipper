"""Publication scheduling and the auto-publish rule (ROADMAP 4.7, Faz 7).

- Each platform keeps `min_gap_minutes` between posts and at most `daily_cap` posts
  per local day.
- The same clip is not pushed everywhere at once: each next platform is at least
  `cross_platform_gap_minutes` after the previous one.
- **Never** schedules a clip of a streamer with `permission_ok=0` (and the publish job
  checks again right before uploading).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from clipper import jobs, settings
from clipper.config import Config
from clipper.log import get_logger

log = get_logger("scheduler")

ORDER = ("youtube", "instagram", "x", "tiktok")
ACTIVE = ("scheduled", "uploading", "processing", "retrying", "published", "inbox")
NO_ACCOUNT = "-"
PUBLISH_PRIORITY = 50
PUBLISH_MAX_ATTEMPTS = 6
Accounts = Callable[[str], list[str]]
CLEAN_FLAGS = ("music_risk", "nsfw", "sensitive_topic")


class PermissionDenied(Exception):
    def __init__(self, streamer: str) -> None:
        super().__init__(f"{streamer} için yayın izni işaretli değil; klip yayınlanmaz")


def clip_owner(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT st.id AS streamer_id, st.display_name, st.permission_ok, st.auto_publish,"
        " st.publish_targets, k.llm_score, k.llm_json, k.review, k.final_path, c.status"
        " FROM clips k JOIN candidates c ON c.id = k.candidate_id"
        " JOIN sessions s ON s.id = c.session_id JOIN streamers st ON st.id = s.streamer_id"
        " WHERE k.id = ?",
        (clip_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"klip #{clip_id} yok")
    return row


def permitted(conn: sqlite3.Connection, clip_id: int) -> bool:
    return bool(clip_owner(conn, clip_id)["permission_ok"])


def _iso_local(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


def _day_bounds(ts: float) -> tuple[float, float]:
    d = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def next_slot(conn: sqlite3.Connection, cfg: Config, platform: str, earliest: float) -> float:
    """First time ≥ `earliest` that keeps the platform's gap and daily cap."""
    gap = cfg.publish.min_gap_minutes.get(platform, 60) * 60.0
    cap = cfg.publish.daily_cap.get(platform, 5)
    marks = ",".join("?" * len(ACTIVE))
    times = sorted(
        float(r[0])
        for r in conn.execute(
            f"SELECT run_at FROM publications WHERE platform = ? AND status IN ({marks})"
            " AND run_at IS NOT NULL",
            (platform, *ACTIVE),
        )
    )
    slot = earliest
    for _ in range(400):
        clash = [t for t in times if abs(t - slot) < gap]
        if clash:
            slot = max(clash) + gap
            continue
        lo, hi = _day_bounds(slot)
        if cap > 0 and sum(1 for t in times if lo <= t < hi) >= cap:
            slot = hi
            continue
        return slot
    return slot


def plan(
    conn: sqlite3.Connection,
    cfg: Config,
    clip_id: int,
    platforms: list[str],
    accounts: Accounts,
    *,
    now: float | None = None,
) -> list[int]:
    """Create `publications` rows + delayed `publish` jobs. Returns new publication ids."""
    owner = clip_owner(conn, clip_id)
    if not owner["permission_ok"]:
        raise PermissionDenied(owner["display_name"])
    now = time.time() if now is None else now
    wanted = [p for p in ORDER if p in set(platforms)]
    created: list[int] = []
    earliest = now
    for platform in wanted:
        exists = conn.execute(
            "SELECT 1 FROM publications WHERE clip_id = ? AND platform = ?", (clip_id, platform)
        ).fetchone()
        if exists:
            continue
        accs = accounts(platform)
        if not accs:
            conn.execute(
                "INSERT INTO publications (clip_id, platform, account, mode, status, error,"
                " created_at) VALUES (?, ?, ?, ?, 'failed', ?, ?)",
                (
                    clip_id,
                    platform,
                    NO_ACCOUNT,
                    _mode(cfg, platform),
                    "hesap bağlı değil (Hesaplar sekmesinden bağla, sonra 'tekrar dene')",
                    now,
                ),
            )
            continue
        slot = next_slot(conn, cfg, platform, earliest)
        cur = conn.execute(
            "INSERT INTO publications (clip_id, platform, account, mode, status, scheduled_at,"
            " run_at, created_at) VALUES (?, ?, ?, ?, 'scheduled', ?, ?, ?)",
            (clip_id, platform, accs[0], _mode(cfg, platform), _iso_local(slot), slot, now),
        )
        assert cur.lastrowid is not None
        enqueue_publish(conn, cur.lastrowid, slot)
        created.append(cur.lastrowid)
        earliest = slot + cfg.publish.cross_platform_gap_minutes * 60.0
    log.info("publications planned", extra={"clip_id": clip_id, "count": len(created)})
    return created


def _mode(cfg: Config, platform: str) -> str:
    if platform == "tiktok":
        return "direct" if cfg.publish.tiktok_mode == "direct" else "inbox"
    return "direct"


def enqueue_publish(conn: sqlite3.Connection, pub_id: int, run_at: float) -> int:
    return jobs.enqueue(
        conn,
        "publish",
        {"publication_id": pub_id},
        "pub",
        PUBLISH_PRIORITY,
        run_after=run_at,
        max_attempts=PUBLISH_MAX_ATTEMPTS,
    )


def retry(
    conn: sqlite3.Connection,
    cfg: Config,
    pub_id: int,
    accounts: Accounts,
    *,
    now: float | None = None,
) -> None:
    row = conn.execute("SELECT * FROM publications WHERE id = ?", (pub_id,)).fetchone()
    if row is None:
        raise LookupError(f"yayın #{pub_id} yok")
    if row["status"] in ("published", "inbox"):
        raise ValueError("bu yayın zaten tamamlandı")
    if row["status"] in ("scheduled", "uploading", "processing"):
        raise ValueError("bu yayın zaten sırada")
    if not permitted(conn, row["clip_id"]):
        raise PermissionDenied(clip_owner(conn, row["clip_id"])["display_name"])
    account = row["account"]
    if account == NO_ACCOUNT:
        accs = accounts(row["platform"])
        if not accs:
            raise ValueError(f"{row['platform']} hesabı bağlı değil")
        account = accs[0]
    now = time.time() if now is None else now
    conn.execute(
        "UPDATE publications SET status = 'scheduled', error = NULL, account = ?, run_at = ?,"
        " scheduled_at = ? WHERE id = ?",
        (account, now, _iso_local(now), pub_id),
    )
    enqueue_publish(conn, pub_id, now)


def auto_publish_ok(cfg: Config, owner: sqlite3.Row, min_score: float | None = None) -> bool:
    """`auto_publish=1` and score ≥ `auto_publish_min_score` and clean flags."""
    if not (owner["auto_publish"] and owner["permission_ok"]):
        return False
    floor = cfg.publish.auto_publish_min_score if min_score is None else min_score
    if owner["llm_score"] is None or owner["llm_score"] < floor:
        return False
    llm: dict[str, Any] = json.loads(owner["llm_json"]) if owner["llm_json"] else {}
    flags = (llm.get("judge") or {}).get("flags") or {}
    return not any(bool(flags.get(k, True)) for k in CLEAN_FLAGS)


def maybe_auto_publish(
    conn: sqlite3.Connection,
    cfg: Config,
    clip_id: int,
    accounts: Accounts,
    *,
    now: float | None = None,
) -> bool:
    """After a render: auto-approve and schedule when the rule holds. Auto approvals are
    not written to `feedback` (they are not a human decision)."""
    owner = clip_owner(conn, clip_id)
    floor = settings.number(
        conn, settings.AUTO_PUBLISH_MIN_SCORE, cfg.publish.auto_publish_min_score
    )
    if owner["review"] != "pending" or not auto_publish_ok(cfg, owner, floor):
        return False
    targets = json.loads(owner["publish_targets"]) if owner["publish_targets"] else []
    if not targets:
        return False
    llm = json.loads(owner["llm_json"]) if owner["llm_json"] else {}
    llm["publish_targets"] = targets
    llm["auto_approved_at"] = time.time() if now is None else now
    conn.execute(
        "UPDATE clips SET review = 'approved', llm_json = ? WHERE id = ?",
        (json.dumps(llm, ensure_ascii=False), clip_id),
    )
    plan(conn, cfg, clip_id, targets, accounts, now=now)
    log.info("clip auto-approved", extra={"clip_id": clip_id})
    return True
