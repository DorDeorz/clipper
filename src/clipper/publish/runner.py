"""`publish` / `publish_status` job handlers: one publication row → one upload."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipper import jobs
from clipper.config import Config
from clipper.log import get_logger
from clipper.publish.base import ClipForPublish, Publisher, PublishError, PublishResult
from clipper.publish.scheduler import NO_ACCOUNT, permitted

log = get_logger("publish")

STATUS_PRIORITY = 60
MAX_STATUS_CHECKS = 30


class PublishJobError(PublishError):
    pass


def _tags_text(tags: list[str]) -> str:
    return " ".join(t if t.startswith("#") else f"#{t}" for t in tags if t)


def clip_for(row: sqlite3.Row, platform: str) -> ClipForPublish:
    """Platform texts from the judge output; a title the user edited wins."""
    llm: dict[str, Any] = json.loads(row["llm_json"]) if row["llm_json"] else {}
    judge = llm.get("judge") or {}
    texts = judge.get("texts") or {}
    tags = [str(t).lstrip("#") for t in judge.get("hashtags") or []]
    title = row["title"] or judge.get("title") or "Klip"
    orig = (llm.get("original") or {}).get("title")
    yt = texts.get("youtube") or {}
    yt_title = title if (orig is not None and orig != title) else (yt.get("title") or title)
    tag_line = _tags_text(tags)

    def with_tags(text: str) -> str:
        return text if not tag_line or "#" in text else f"{text}\n\n{tag_line}".strip()

    caption = (
        ((texts.get(platform) or {}).get("caption") or title)
        if platform in ("instagram", "tiktok")
        else title
    )
    return ClipForPublish(
        clip_id=row["id"],
        path=Path(row["final_path"]),
        title=yt_title,
        description=with_tags(yt.get("description") or title),
        caption=with_tags(caption),
        x_text=(texts.get("x") or {}).get("text") or title,
        tags=tags,
    )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


class Publishing:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        publishers: dict[str, Publisher],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.publishers = publishers
        self.clock = clock

    def handles(self, job_type: str) -> bool:
        return job_type in ("publish", "publish_status")

    def _pub(self, pub_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM publications WHERE id = ?", (pub_id,)).fetchone()
        if row is None:
            raise PublishError(f"yayın #{pub_id} yok", retry=False)
        return row

    def _set(self, pub_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE publications SET {cols} WHERE id = ?", (*fields.values(), pub_id)
        )

    def _save_state(self, pub_id: int) -> Callable[[dict[str, Any]], None]:
        def save(state: dict[str, Any]) -> None:
            self._set(pub_id, state=json.dumps(state, ensure_ascii=False))

        return save

    def _apply(self, pub_id: int, res: PublishResult, checks: int = 0) -> None:
        if res.status == "processing":
            self._set(pub_id, status="processing", remote_id=res.remote_id, error=None)
            if checks >= MAX_STATUS_CHECKS:
                self._set(pub_id, status="failed", error="platform işlemeyi bitirmedi")
                return
            jobs.enqueue(
                self.conn,
                "publish_status",
                {"publication_id": pub_id, "checks": checks + 1},
                "pub",
                STATUS_PRIORITY,
                run_after=self.clock() + self.cfg.publish.status_poll_seconds,
            )
            return
        self._set(
            pub_id,
            status=res.status,
            remote_id=res.remote_id,
            url=res.url,
            error=res.error,
            published_at=_iso(self.clock()),
        )
        log.info("published", extra={"publication_id": pub_id, "status": res.status})

    def _fail(self, job: jobs.Job, pub_id: int, exc: PublishError) -> None:
        final = not exc.retry or job.attempts >= job.max_attempts
        self._set(pub_id, status="failed" if final else "retrying", error=str(exc))
        if not final:
            raise exc  # the worker reschedules with backoff

    async def handle(self, job: jobs.Job) -> None:
        pub_id = int(job.payload["publication_id"])
        pub = self._pub(pub_id)
        if pub["status"] in ("published", "inbox", "failed") and job.type == "publish_status":
            return
        if pub["status"] in ("published", "inbox"):
            return
        publisher = self.publishers.get(pub["platform"])
        if publisher is None:
            self._set(
                pub_id,
                status="failed",
                error=f"{pub['platform']} yayıncısı hazır değil (uygulama bilgileri .env'de yok)",
            )
            return
        # Hard guard, checked again right before any upload.
        if not permitted(self.conn, pub["clip_id"]):
            self._set(pub_id, status="failed", error="yayıncı izni yok; yayınlanmadı")
            log.warning("publish blocked: no permission", extra={"publication_id": pub_id})
            return
        if pub["account"] == NO_ACCOUNT:
            self._set(pub_id, status="failed", error="hesap bağlı değil")
            return
        state: dict[str, Any] = json.loads(pub["state"]) if pub["state"] else {}
        try:
            if job.type == "publish_status":
                res = await publisher.check_status(pub["account"], pub["remote_id"] or "", state)
                self._save_state(pub_id)(state)
                self._apply(pub_id, res, int(job.payload.get("checks") or 0))
                return
            clip = self.conn.execute(
                "SELECT * FROM clips WHERE id = ?", (pub["clip_id"],)
            ).fetchone()
            if clip is None or not clip["final_path"] or not Path(clip["final_path"]).is_file():
                raise PublishError("klip videosu bulunamadı", retry=False)
            self._set(pub_id, status="uploading", attempts=pub["attempts"] + 1, error=None)
            res = await publisher.upload(
                clip_for(clip, pub["platform"]), pub["account"], state, self._save_state(pub_id)
            )
        except PublishError as exc:
            self._fail(job, pub_id, exc)
            return
        self._apply(pub_id, res)
