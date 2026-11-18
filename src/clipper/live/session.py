"""One live session (ROADMAP 3.1, Faz 6): recorder + chat + streaming detector + viewer
clips + ring buffer, all in one asyncio task group.

Time base: session time = wall clock − `t0` (the moment the session opened), the same
base the VOD path uses, so cutting, judging and rendering are shared.

Chat arrives a little late and audio arrives with HLS segments (latency of several
seconds), so the detector runs `feed_delay` seconds behind real time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sqlite3
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from clipper import chatlog
from clipper.config import Config
from clipper.detect.audio import segment_db
from clipper.detect.detector import Detection, Detector, Observation
from clipper.detect.events import EventFilter, LiveEvent, events_near, write_events
from clipper.detect.keywords_tr import KeywordScorer
from clipper.detect.merge import Draft
from clipper.detect.pipeline import (
    _context_features,
    _reactive_draft,
    _viewer_clip_draft,
    detector_config,
    top_messages,
)
from clipper.detect.signals import ChatBinner, SecondBin
from clipper.live import candidates
from clipper.live.chat import ChatSink, Session, reconnecting
from clipper.log import get_logger
from clipper.models import ChatMessage, Segment
from clipper.recorder.hls_live import LiveRecorder, Resolver, prune_segments
from clipper.sessions import (
    CHAT_FILE,
    LIVE_CHAT_FILE,
    create_session,
    finish_session,
    write_session_info,
)
from clipper.util.timeutil import parse_iso

log = get_logger("live.session")
RECENT_CHAT_SECONDS = 600


@dataclass
class StreamSource:
    """What the watcher hands to a session: platform-specific callables, nothing else."""

    platform: str
    channel: str
    resolve: Resolver
    chat: Session | None
    title: str | None = None
    category: str | None = None
    started_at: float | None = None
    viewers: int | None = None
    poll_clips: Callable[[float], Awaitable[list[LiveEvent]]] | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class LiveStats:
    messages: int = 0
    events: int = 0
    candidates: int = 0
    merged: int = 0
    gaps: int = 0
    last_t: int | None = None


class LiveSession(ChatSink):
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        streamer: sqlite3.Row,
        source: StreamSource,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
        feed_delay: float = 8.0,
        tick: float = 1.0,
        analyse_audio: bool = True,
        recorder_offline_after: float = 120.0,
        stop_grace: float = 10.0,
        extra_tasks: list[Callable[[LiveSession, asyncio.Event], Awaitable[None]]] | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.streamer = streamer
        self.source = source
        self.client = client
        self.clock = clock
        self.feed_delay = feed_delay
        self.tick = tick
        self.analyse_audio = analyse_audio
        self.recorder_offline_after = recorder_offline_after
        self.stop_grace = stop_grace
        self.extra_tasks = extra_tasks or []
        self.stats = LiveStats()
        self.session_id: int | None = None
        self.session_dir: Path | None = None
        self.t0 = 0.0
        self.stop = asyncio.Event()
        self.recorder: LiveRecorder | None = None

        dcfg = detector_config(cfg.detector, streamer["detector_overrides"])
        self.dcfg = dcfg.model_copy(update={"viewer_clip_bonus": cfg.viewer_clips.bonus})
        extra = json.loads(streamer["keywords"]) if streamer["keywords"] else {}
        self.binner = ChatBinner(
            KeywordScorer(extra={k: float(v) for k, v in extra.items()}), cfg.events.thanks_words
        )
        self.detector = Detector(self.dcfg)
        self.filter = EventFilter(cfg.events)
        self.events: list[LiveEvent] = []
        self.recent: deque[ChatMessage] = deque()
        self.gaps: list[tuple[float, float]] = []
        self._chat_fh: Any = None
        self._fed_until: int | None = None  # last session second fed to the detector
        self._audio: dict[int, float] = {}
        self._bins: dict[int, SecondBin] = {}
        self._thanks: deque[tuple[int, int]] = deque()  # (msgs, thanks) of last window secs
        self._audio_tasks: set[asyncio.Task[None]] = set()
        self._sig_rows: list[tuple[int, int, int, float, int, float | None]] = []

    # ---- ChatSink (wall-clock in, session time stored) ---------------------------------

    def rel(self, wall: float) -> float:
        return wall - self.t0

    def message(self, msg: ChatMessage) -> None:
        ts = self.rel(msg.ts)
        if ts < 0:
            return
        m = ChatMessage(ts, msg.user_id, msg.text, msg.emotes, msg.platform, msg.weight)
        if self._chat_fh is not None:
            self._chat_fh.write(json.dumps(m.to_json(), ensure_ascii=False) + "\n")
        self.stats.messages += 1
        self.recent.append(m)
        if self._fed_until is not None and math.floor(ts) <= self._fed_until:
            return  # too late for the detector (already past that second)
        self.binner.add(m)

    def event(self, ev: LiveEvent) -> None:
        e = LiveEvent(self.rel(ev.ts), ev.kind, ev.amount, ev.data)
        self.stats.events += 1
        self.events.append(e)
        assert self.session_id is not None
        write_events(self.conn, self.session_id, [e])
        if e.kind == "viewer_clip":
            if not self.cfg.viewer_clips.enabled:
                return
            self.detector.add_viewer_clip(e.ts + float(e.data.get("duration") or 0) / 2)
            if self.cfg.viewer_clips.as_candidate:
                self.submit(_viewer_clip_draft(e, self.dcfg.threshold, self.cfg.viewer_clips))
        else:
            self.filter.add(e)

    def gap(self, start: float, end: float) -> None:
        self.stats.gaps += 1
        self.gaps.append((self.rel(start), self.rel(end)))

    def _chat_ok(self, t: int) -> bool:
        return not any(a <= t < b for a, b in self.gaps[-20:])

    # ---- lifecycle ---------------------------------------------------------------------

    def open(self) -> int:
        self.t0 = self.clock()
        sid, sdir = create_session(
            self.conn,
            self.cfg.paths.data_dir,
            self.streamer["id"],
            "live",
            title=self.source.title,
            category=self.source.category,
        )
        self.session_id, self.session_dir = sid, sdir
        write_session_info(
            sdir,
            {
                "session_id": sid,
                "streamer": self.streamer["display_name"],
                "platform": self.source.platform,
                "channel": self.source.channel,
                "time_base": "wall",
                "t0": self.t0,
                "stream_started_at": self.source.started_at,
            },
        )
        self._chat_fh = (sdir / LIVE_CHAT_FILE).open("a", encoding="utf-8", newline="\n")
        log.info("live session opened", extra={"session_id": sid, "channel": self.source.channel})
        return sid

    async def run(self, stop: asyncio.Event | None = None) -> LiveStats:
        """Run until `stop` (watcher: stream offline) or the recorder gives up."""
        if self.session_id is None:
            self.open()
        assert self.session_id is not None and self.session_dir is not None
        outer = stop or asyncio.Event()
        self.recorder = LiveRecorder(
            self.client,
            self.conn,
            self.session_id,
            self.session_dir / "seg",
            self.source.resolve,
            self.cfg.recorder.quality_order,
            self.t0,
            on_segment=self._on_segment,
            offline_after=self.recorder_offline_after,
            clock=self.clock,
            headers=self.source.headers,
        )
        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self._record(), name="recorder"),
            asyncio.create_task(self._detect_loop(), name="detector"),
            asyncio.create_task(self._housekeeping(), name="housekeeping"),
        ]
        if self.source.chat is not None:
            tasks.append(
                asyncio.create_task(
                    reconnecting(
                        f"{self.source.platform}:{self.source.channel}",
                        self.source.chat,
                        self,
                        self.stop,
                        clock=self.clock,
                    ),
                    name="chat",
                )
            )
        if self.source.poll_clips is not None and self.cfg.viewer_clips.enabled:
            tasks.append(asyncio.create_task(self._poll_clips(), name="clips"))
        tasks += [asyncio.create_task(fn(self, self.stop)) for fn in self.extra_tasks]
        outer_wait = asyncio.create_task(outer.wait())
        status: str = "ended"
        try:
            done, _ = await asyncio.wait(
                [tasks[0], outer_wait, *tasks[1:]], return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                if t is not outer_wait and not t.cancelled() and t.exception() is not None:
                    status = "failed"
                    log.error(
                        "live task failed",
                        extra={"session_id": self.session_id, "task": t.get_name(),
                               "error": repr(t.exception())},
                    )  # fmt: skip
        finally:
            self.stop.set()
            outer_wait.cancel()
            # Tasks watch `stop`; one stuck in network I/O is cancelled after a grace.
            _, pending = await asyncio.wait(tasks, timeout=self.stop_grace)
            for t in pending:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._audio_tasks:
                await asyncio.gather(*self._audio_tasks, return_exceptions=True)
            self._finish(status)  # type: ignore[arg-type]
        return self.stats

    def _finish(self, status: str) -> None:
        # Detections still open (peak window not finished) are flushed as they are.
        for d in self.detector.flush():
            self._submit_detection(d)
        self._write_signals()
        if self._chat_fh is not None:
            self._chat_fh.close()
            self._chat_fh = None
        assert self.session_dir is not None and self.session_id is not None
        live = self.session_dir / LIVE_CHAT_FILE
        if live.is_file():
            chatlog.write_jsonl(self.session_dir / CHAT_FILE, chatlog.read_jsonl(live))
            live.unlink()
        finish_session(self.conn, self.session_id, "failed" if status == "failed" else "ended")
        log.info(
            "live session closed",
            extra={"session_id": self.session_id, "status": status, **self.stats.__dict__},
        )

    async def _record(self) -> None:
        assert self.recorder is not None
        await self.recorder.run(self.stop)

    # ---- segments / audio --------------------------------------------------------------

    def _on_segment(self, seg: Segment) -> None:
        if not self.analyse_audio or not seg.path:
            return
        task = asyncio.create_task(self._segment_audio(seg))
        self._audio_tasks.add(task)
        task.add_done_callback(self._audio_tasks.discard)

    async def _segment_audio(self, seg: Segment) -> None:
        try:
            dbs = await segment_db(Path(seg.path), seg.pdt_start, self.cfg.paths.ffmpeg)
        except Exception as exc:  # a bad segment only loses its audio
            log.warning("segment audio failed", extra={"seq": seg.seq, "error": str(exc)})
            return
        for t, db in dbs.items():
            self._audio[t] = db
            self.detector.add_audio(t, db)

    # ---- detector ----------------------------------------------------------------------

    async def _detect_loop(self) -> None:
        while not self.stop.is_set():
            self.step(self.clock())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), self.tick)

    def step(self, now: float) -> list[Detection]:
        """Feed every finished second up to `now − feed_delay` (public for tests)."""
        upto = math.floor(self.rel(now) - self.feed_delay)
        if upto < 0:
            return []
        start = 0 if self._fed_until is None else self._fed_until + 1
        if start > upto:
            return []
        for b in self.binner.pop_until(upto + 1):
            self._bins[b.t] = b
        out: list[Detection] = []
        window = self.dcfg.burst_window
        for t in range(start, upto + 1):
            b = self._bins.pop(t, None)
            msgs, thanks = (b.msgs, b.thanks) if b else (0, 0)
            self._thanks.append((msgs, thanks))
            while len(self._thanks) > window:
                self._thanks.popleft()
            wm = sum(x[0] for x in self._thanks)
            wt = sum(x[1] for x in self._thanks)
            emote = b.emote if b else 0.0
            if wm >= 3 and wt >= self.cfg.events.thanks_ratio * wm:
                emote *= self.cfg.events.thanks_emote_factor
            st = self.filter.state(t)
            out += self.detector.feed(
                Observation(
                    t=t,
                    msgs=msgs,
                    uniq=b.uniq if b else 0,
                    emote=emote,
                    audio_db=self._audio.get(t),
                    chat_ok=self._chat_ok(t),
                    threshold_mult=st.threshold_mult,
                    suppressed=st.suppressed,
                )
            )
            self._last_bins_for_signals(t, b)
        self._fed_until = upto
        self.stats.last_t = upto
        for d in out:
            self._submit_detection(d)
        self._write_signals()
        return out

    def _last_bins_for_signals(self, t: int, b: SecondBin | None) -> None:
        self._sig_rows.append(
            (t, b.msgs if b else 0, b.uniq if b else 0, round(b.emote, 3) if b else 0.0,
             b.kw if b else 0, self._audio.get(t))
        )  # fmt: skip

    def _write_signals(self) -> None:
        rows = self._sig_rows
        scores = {r.t: r.score for r in self.detector.history}
        self.detector.history.clear()
        if rows and self.session_id is not None:
            self.conn.executemany(
                "INSERT OR REPLACE INTO signal_seconds (session_id, t, msgs, uniq, emote, kw,"
                " audio_db, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (self.session_id, t, m, u, e, k,
                     round(a, 2) if a is not None else None, round(scores.get(t, 0.0), 4))
                    for t, m, u, e, k, a in rows
                ],
            )  # fmt: skip
        self._sig_rows = []
        # audio older than the detector can still use is dropped
        if self._fed_until is not None:
            old = self._fed_until - int(self.dcfg.lag_seconds) - self.dcfg.burst_window - 30
            for t in [t for t in self._audio if t < old]:
                del self._audio[t]

    def _submit_detection(self, d: Detection) -> None:
        self.submit(_reactive_draft(d, self.filter))

    def submit(self, draft: Draft) -> None:
        assert self.session_id is not None
        draft.win_start = max(draft.win_start, 0.0)
        msg_from = float(draft.features.get("event_ts", draft.win_start))
        cutoff = self.rel(self.clock()) - RECENT_CHAT_SECONDS
        while self.recent and self.recent[0].ts < cutoff:
            self.recent.popleft()
        draft.features.setdefault(
            "top_messages", top_messages(list(self.recent), msg_from - 5, draft.peak_ts + 5)
        )
        draft.features.setdefault(
            "events",
            events_near(
                [e for e in self.events if e.kind != "viewer_clip"],
                draft.win_start,
                draft.win_end,
            ),
        )
        sess = self.conn.execute(
            "SELECT category, started_at FROM sessions WHERE id = ?", (self.session_id,)
        ).fetchone()
        for k, v in _context_features(sess).items():
            draft.features.setdefault(k, v)
        if self.source.viewers is not None:
            draft.features.setdefault("viewers", self.source.viewers)
        _, merged = candidates.upsert(
            self.conn,
            self.session_id,
            draft,
            t0=self.t0,
            radius=self.cfg.merge.radius_seconds,
            max_seconds=self.cfg.content_scan.max_candidate_seconds,
            now=self.clock(),
        )
        if merged:
            self.stats.merged += 1
        else:
            self.stats.candidates += 1
        log.info(
            "live candidate",
            extra={"session_id": self.session_id, "peak": round(draft.peak_ts, 1),
                   "sources": draft.sources, "merged": merged},
        )  # fmt: skip

    # ---- side tasks --------------------------------------------------------------------

    async def _poll_clips(self, every: float = 60.0) -> None:
        assert self.source.poll_clips is not None
        since = self.t0
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), every)
            if self.stop.is_set():
                return
            try:
                clips = await self.source.poll_clips(since)
            except Exception as exc:
                log.warning("viewer clip poll failed", extra={"error": str(exc)})
                continue
            for ev in clips:
                created = ev.data.get("created_at")
                self.event(ev)
                if isinstance(created, str):
                    since = max(since, parse_iso(created))

    async def _housekeeping(self, every: float = 60.0) -> None:
        assert self.session_id is not None
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), every)
            if self._chat_fh is not None:
                self._chat_fh.flush()
            n = prune_segments(
                self.conn,
                self.session_id,
                self.rel(self.clock()),
                self.cfg.recorder.buffer_minutes,
            )
            if n:
                log.info("ring buffer pruned", extra={"session_id": self.session_id, "files": n})
