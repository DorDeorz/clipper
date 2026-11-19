"""Job handlers for the clip pipeline after detection:

cut (cpu) -> transcribe (gpu) -> prefilter (io, Haiku) -> judge (io, Sonnet/Opus)
-> render (cpu, ffmpeg). r3 session jobs: session_transcribe (gpu) -> content_scan (io).

Every handler is idempotent on its candidate: it re-reads state from the DB and skips work
that is already done, so a retried or recovered job does not duplicate rows.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from clipper import chatlog, jobs, settings
from clipper.config import Config
from clipper.content.batch import (
    CONTENT_SCAN_PRIORITY,
    scan_session,
    transcribe_session,
)
from clipper.content.scanner import ContentScanner
from clipper.learn.service import gate_model, gate_probability
from clipper.llm.client import LLMBudgetExceeded, LLMClient, LLMRefused
from clipper.llm.prompts import (
    PREFILTER_SYSTEM,
    FeedbackExamples,
    apply_caption_fixes,
    audio_profile,
    candidate_context,
    chat_timeline,
    feedback_block,
    judge_system,
    judge_user,
    prefilter_user,
    snap_to_words,
    transcript_lines,
    transcript_text,
    words_from_transcript,
)
from clipper.llm.schemas import JudgeDecision, PrefilterDecision
from clipper.log import get_logger
from clipper.media import MediaError
from clipper.models import ChatMessage
from clipper.recorder.cutter import cut
from clipper.render.clip import render_clip
from clipper.render.ffmpeg import RenderError
from clipper.render.layouts import Rect, effective_layout
from clipper.sessions import CHAT_FILE, LIVE_CHAT_FILE
from clipper.transcribe.whisper import Transcriber
from clipper.util.timeutil import iso_now

log = get_logger("stages")

PREFILTER_PRIORITY = 20
JUDGE_PRIORITY_BASE = 100
# Judge output is a small JSON plus adaptive thinking; this caps a single answer at
# about $0.08 on Sonnet 5.5 (16000 would allow ~$0.16).
JUDGE_MAX_TOKENS = 8000
RECENT_TITLES = 30
RENDER_PRIORITY_BASE = 50
FEEDBACK_EXAMPLES = 20


class StageError(Exception):
    """User-facing (Turkish). `retry=False` means retrying cannot help."""

    def __init__(self, message: str, *, retry: bool = True) -> None:
        super().__init__(message)
        self.retry = retry


def clip_dir(session_dir: Path, candidate_id: int) -> Path:
    return session_dir / "clips" / str(candidate_id)


def hour_bucket(peak_ts: float) -> int:
    return math.floor(peak_ts / 3600)


def _read_chat(path: Path) -> Iterator[ChatMessage]:
    """Chat of a session; a live file may end in a half-written line, which is skipped."""
    try:
        yield from chatlog.read_jsonl(path)
    except chatlog.ChatFormatError:
        if path.suffix == ".gz":
            raise


class Stages:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        *,
        transcriber: Transcriber | None = None,
        llm: LLMClient | None = None,
        on_rendered: Callable[[int], object] | None = None,
        extra: Callable[[jobs.Job], Awaitable[None]] | None = None,
        extra_types: frozenset[str] = frozenset(),
    ) -> None:
        self.on_rendered = on_rendered
        self.extra = extra
        self.extra_types = extra_types
        self.conn = conn
        self.cfg = cfg
        self.transcriber = transcriber
        self.llm = llm
        self._chat_cache: tuple[int, float, list[ChatMessage]] | None = None

    # ---- helpers -------------------------------------------------------------------------

    def _candidate(self, candidate_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT c.*, s.dir AS session_dir, s.category, s.streamer_id,"
            " st.display_name AS streamer FROM candidates c"
            " JOIN sessions s ON s.id = c.session_id"
            " JOIN streamers st ON st.id = s.streamer_id WHERE c.id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise StageError(f"aday #{candidate_id} bulunamadı", retry=False)
        return row

    def _clip(self, candidate_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM clips WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()

    def _set_status(self, candidate_id: int, status: str) -> None:
        self.conn.execute("UPDATE candidates SET status = ? WHERE id = ?", (status, candidate_id))

    def _llm_json(self, clip: sqlite3.Row) -> dict[str, Any]:
        return json.loads(clip["llm_json"]) if clip["llm_json"] else {}

    def _save_llm_json(self, candidate_id: int, data: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE clips SET llm_json = ? WHERE candidate_id = ?",
            (json.dumps(data, ensure_ascii=False), candidate_id),
        )

    def _chat(self, session_id: int, session_dir: Path) -> list[ChatMessage]:
        path = session_dir / CHAT_FILE
        if not path.is_file():  # live session still running: plain file, still growing
            path = session_dir / LIVE_CHAT_FILE
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        if self._chat_cache is None or self._chat_cache[:2] != (session_id, mtime):
            msgs = list(_read_chat(path)) if path.is_file() else []
            self._chat_cache = (session_id, mtime, msgs)
        return self._chat_cache[2]

    def _transcript(self, clip: sqlite3.Row) -> list[dict[str, Any]]:
        path = clip["transcript_path"]
        if not path or not Path(path).is_file():
            raise StageError("transkript dosyası yok", retry=False)
        data: list[dict[str, Any]] = json.loads(Path(path).read_text(encoding="utf-8"))
        return data

    def _require_llm(self) -> LLMClient:
        if self.llm is None:
            raise StageError("ANTHROPIC_API_KEY tanımlı değil (.env)")
        return self.llm

    # ---- handlers ------------------------------------------------------------------------

    async def handle(self, job: jobs.Job) -> None:
        if self.extra is not None and job.type in self.extra_types:
            await self.extra(job)
            return
        session_handler = {
            "session_transcribe": self.handle_session_transcribe,
            "content_scan": self.handle_content_scan,
        }.get(job.type)
        if session_handler is not None:
            await session_handler(int(job.payload["session_id"]), job.payload)
            return
        handler = {
            "cut": self.handle_cut,
            "transcribe": self.handle_transcribe,
            "prefilter": self.handle_prefilter,
            "judge": self.handle_judge,
            "render": self.handle_render,
        }.get(job.type)
        if handler is None:
            raise StageError(f"bilinmeyen iş tipi: {job.type}", retry=False)
        await handler(int(job.payload["candidate_id"]), job.payload)

    async def handle_session_transcribe(self, session_id: int, payload: dict[str, Any]) -> None:
        """r3 content path, VOD mode: transcribe the whole session, then queue the scan."""
        if self.transcriber is None:
            raise StageError("transkript motoru hazır değil")
        try:
            await transcribe_session(self.conn, self.cfg, session_id, self.transcriber)
        except MediaError as exc:
            raise StageError(str(exc), retry=False) from exc
        jobs.enqueue(
            self.conn, "content_scan", {"session_id": session_id}, "io", CONTENT_SCAN_PRIORITY
        )

    async def handle_content_scan(self, session_id: int, payload: dict[str, Any]) -> None:
        llm = self._require_llm()
        cfg = self.cfg

        def factory(sess: sqlite3.Row, baseline: float | None) -> ContentScanner:
            return ContentScanner(
                cfg.content_scan,
                llm,
                streamer=sess["streamer"],
                category=sess["category"],
                chat_rate_baseline=baseline,
            )

        await scan_session(self.conn, cfg, session_id, factory)

    async def handle_cut(self, candidate_id: int, payload: dict[str, Any]) -> None:
        cand = self._candidate(candidate_id)
        clip = self._clip(candidate_id)
        if clip is not None and clip["raw_path"] and Path(clip["raw_path"]).is_file():
            log.info("cut already done", extra={"candidate_id": candidate_id})
        else:
            t0 = float(payload.get("t0", cand["win_start"]))
            t1 = float(payload.get("t1", cand["win_end"]))
            out = clip_dir(Path(cand["session_dir"]), candidate_id)
            res = await cut(self.conn, cand["session_id"], t0, t1, out, self.cfg.paths.ffmpeg)
            meta = {"cut": {"t0": res.t0, "duration": res.duration}}
            if clip is None:
                self.conn.execute(
                    "INSERT INTO clips (candidate_id, raw_path, llm_json, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (candidate_id, str(res.raw_mp4), json.dumps(meta), iso_now()),
                )
            else:
                data = {**self._llm_json(clip), **meta}
                self.conn.execute(
                    "UPDATE clips SET raw_path = ?, llm_json = ? WHERE candidate_id = ?",
                    (str(res.raw_mp4), json.dumps(data, ensure_ascii=False), candidate_id),
                )
        self._set_status(candidate_id, "cut")
        jobs.enqueue(self.conn, "transcribe", {"candidate_id": candidate_id}, "gpu")

    async def handle_transcribe(self, candidate_id: int, payload: dict[str, Any]) -> None:
        clip = self._clip(candidate_id)
        if clip is None or not clip["raw_path"]:
            raise StageError(f"aday #{candidate_id} için kesim yok", retry=False)
        if self.transcriber is None:
            raise StageError("transkript motoru hazır değil")
        raw = Path(clip["raw_path"])
        wav = raw.with_name("raw.wav")
        out = raw.with_name("transcript.json")
        if not out.is_file():
            origin = self._llm_json(clip).get("cut", {}).get("t0")
            await self.transcriber.transcribe(wav, out, origin=origin)
        self.conn.execute(
            "UPDATE clips SET transcript_path = ? WHERE candidate_id = ?", (str(out), candidate_id)
        )
        self._set_status(candidate_id, "transcribed")
        cand = self._candidate(candidate_id)
        sources = json.loads(cand["sources"]) if cand["sources"] else ["reactive"]
        if len(sources) >= 2:
            # r3: found by several paths -> strong enough to skip the Haiku prefilter.
            clip = self._clip(candidate_id)
            assert clip is not None
            data = self._llm_json(clip)
            data["prefilter"] = {"keep": True, "skipped": "multi_source", "sources": sources}
            self._save_llm_json(candidate_id, data)
            self._set_status(candidate_id, "prefiltered")
            jobs.enqueue(
                self.conn, "judge", {"candidate_id": candidate_id}, "io", JUDGE_PRIORITY_BASE
            )
            return
        jobs.enqueue(
            self.conn, "prefilter", {"candidate_id": candidate_id}, "io", PREFILTER_PRIORITY
        )

    async def handle_prefilter(self, candidate_id: int, payload: dict[str, Any]) -> None:
        llm = self._require_llm()
        cand = self._candidate(candidate_id)
        clip = self._clip(candidate_id)
        assert clip is not None
        features = json.loads(cand["features"])
        if self._learned_gate(candidate_id, cand, clip, features):
            return
        transcript = self._transcript(clip)
        user = prefilter_user(
            cand["streamer"],
            cand["category"],
            transcript_text(transcript),
            features.get("top_messages", []),
            features,
        )
        try:
            res = await llm.structured(
                purpose="prefilter",
                model=self.cfg.llm.prefilter_model,
                system=PREFILTER_SYSTEM,
                user=user,
                schema=PrefilterDecision,
                effort=self.cfg.llm.prefilter_effort,
                max_tokens=2000,
            )
        except (LLMRefused, LLMBudgetExceeded) as exc:
            self._reject(candidate_id, clip, "prefilter", str(exc))
            return
        dec = res.value
        data = self._llm_json(clip)
        data["prefilter"] = {**dec.model_dump(), "model": res.model, "usd": round(res.usd, 6)}
        self._save_llm_json(candidate_id, data)
        if not dec.keep:
            self._set_status(candidate_id, "rejected")
            return
        self._set_status(candidate_id, "prefiltered")
        # Higher rough_score -> runs earlier, so the hourly cap keeps the best ones.
        jobs.enqueue(
            self.conn,
            "judge",
            {"candidate_id": candidate_id},
            "io",
            JUDGE_PRIORITY_BASE + (100 - dec.rough_score),
        )

    def _learned_gate(
        self,
        candidate_id: int,
        cand: sqlite3.Row,
        clip: sqlite3.Row,
        features: dict[str, Any],
    ) -> bool:
        """4.9 Kademe 2, only after the user applied a learned model: very unlikely
        candidates never reach the LLM, very likely ones skip the prefilter. Returns True
        when the candidate was handled here."""
        model = gate_model(self.conn, self.cfg, int(cand["streamer_id"]))
        if model is None:
            return False
        snap = {
            "features": features,
            "det_score": cand["det_score"],
            "sources": json.loads(cand["sources"]) if cand["sources"] else ["reactive"],
            "content_score": cand["content_score"],
            "window": [cand["win_start"], cand["win_end"]],
        }
        p = gate_probability(model, snap)
        lc = self.cfg.learn
        if p < lc.gate_skip_below:
            self._reject(
                candidate_id,
                clip,
                "prefilter",
                f"öğrenilmiş model: kabul olasılığı düşük ({p:.2f})",
            )
            return True
        if p > lc.gate_bypass_above:
            data = self._llm_json(clip)
            data["prefilter"] = {
                "keep": True,
                "rough_score": round(p * 100),
                "reason": f"öğrenilmiş model ön elemeyi atladı ({p:.2f})",
                "model": "learned",
            }
            self._save_llm_json(candidate_id, data)
            self._set_status(candidate_id, "prefiltered")
            jobs.enqueue(
                self.conn, "judge", {"candidate_id": candidate_id}, "io", JUDGE_PRIORITY_BASE
            )
            return True
        return False

    def _reject(self, candidate_id: int, clip: sqlite3.Row, stage: str, reason: str) -> None:
        data = self._llm_json(clip)
        data[stage] = {"keep": False, "reason": reason}
        self._save_llm_json(candidate_id, data)
        self._set_status(candidate_id, "rejected")

    def judged_in_hour(self, session_id: int, peak_ts: float) -> int:
        lo = hour_bucket(peak_ts) * 3600
        row = self.conn.execute(
            "SELECT COUNT(*) FROM candidates c JOIN clips k ON k.candidate_id = c.id"
            " WHERE c.session_id = ? AND c.peak_ts >= ? AND c.peak_ts < ?"
            " AND json_extract(k.llm_json, '$.judge.model') IS NOT NULL",
            (session_id, lo, lo + 3600),
        ).fetchone()
        return int(row[0])

    def recent_titles(self, streamer_id: int, exclude: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT k.title FROM clips k JOIN candidates c ON c.id = k.candidate_id"
            " JOIN sessions s ON s.id = c.session_id WHERE s.streamer_id = ?"
            " AND k.title IS NOT NULL AND c.status != 'rejected' AND k.candidate_id != ?"
            " ORDER BY k.id DESC LIMIT ?",
            (streamer_id, exclude, RECENT_TITLES),
        ).fetchall()
        return [r[0] for r in rows]

    def feedback_examples(self, streamer_id: int) -> FeedbackExamples:
        """4.9 Kademe 1: the streamer's latest approvals (titles) and rejection reasons."""
        approved = self.conn.execute(
            "SELECT k.title FROM feedback f JOIN clips k ON k.id = f.clip_id"
            " JOIN candidates c ON c.id = k.candidate_id JOIN sessions s ON s.id = c.session_id"
            " WHERE s.streamer_id = ? AND f.decision IN ('approved', 'edited')"
            " AND k.title IS NOT NULL ORDER BY f.created_at DESC LIMIT ?",
            (streamer_id, FEEDBACK_EXAMPLES),
        ).fetchall()
        rejected = self.conn.execute(
            "SELECT COALESCE(f.reason, 'other') AS reason, COUNT(*) FROM ("
            " SELECT f.reason FROM feedback f JOIN clips k ON k.id = f.clip_id"
            " JOIN candidates c ON c.id = k.candidate_id JOIN sessions s ON s.id = c.session_id"
            " WHERE s.streamer_id = ? AND f.decision = 'rejected'"
            " ORDER BY f.created_at DESC LIMIT ?) f GROUP BY 1 ORDER BY 2 DESC",
            (streamer_id, FEEDBACK_EXAMPLES),
        ).fetchall()
        return FeedbackExamples([r[0] for r in approved], [(r[0], int(r[1])) for r in rejected])

    async def handle_judge(self, candidate_id: int, payload: dict[str, Any]) -> None:
        llm = self._require_llm()
        cand = self._candidate(candidate_id)
        clip = self._clip(candidate_id)
        assert clip is not None
        data = self._llm_json(clip)
        if self.judged_in_hour(cand["session_id"], cand["peak_ts"]) >= (
            self.cfg.llm.max_candidates_per_hour
        ):
            self._reject(candidate_id, clip, "judge", "saatlik aday sınırı doldu")
            return

        transcript = self._transcript(clip)
        cut_meta = data.get("cut", {})
        t0 = float(cut_meta.get("t0", cand["win_start"]))
        duration = float(cut_meta.get("duration", cand["win_end"] - cand["win_start"]))
        t1 = t0 + duration
        chat = chat_timeline(self._chat(cand["session_id"], Path(cand["session_dir"])), t0, t1)
        audio_rows = self.conn.execute(
            "SELECT t, audio_db FROM signal_seconds WHERE session_id = ? AND t BETWEEN ? AND ?"
            " AND audio_db IS NOT NULL",
            (cand["session_id"], math.floor(t0), math.ceil(t1)),
        ).fetchall()
        audio = audio_profile({int(r[0]): float(r[1]) for r in audio_rows}, t0, t1)
        rcfg = self.cfg.render
        user = judge_user(
            cand["streamer"],
            cand["category"],
            duration,
            transcript_lines(transcript),
            chat,
            audio,
            self.recent_titles(cand["streamer_id"], candidate_id),
            context=candidate_context(json.loads(cand["features"])),
            feedback=feedback_block(self.feedback_examples(cand["streamer_id"])),
        )
        model, downgraded = llm.judge_model()
        try:
            res = await llm.structured(
                purpose="judge",
                model=model,
                system=judge_system(rcfg.min_len, rcfg.max_len),
                user=user,
                schema=JudgeDecision,
                effort=self.cfg.llm.judge_effort,
                max_tokens=JUDGE_MAX_TOKENS,
            )
        except (LLMRefused, LLMBudgetExceeded) as exc:
            self._reject(candidate_id, clip, "judge", str(exc))
            return
        dec = res.value
        start, end = snap_to_words(
            dec.start,
            dec.end,
            words_from_transcript(transcript),
            duration,
            rcfg.min_len,
            rcfg.max_len,
        )
        data["judge"] = {
            **dec.model_dump(by_alias=True),
            "model": res.model,
            "downgraded": downgraded,
            "usd": round(res.usd, 6),
            "snapped": {"start": start, "end": end},
        }
        min_keep = settings.number(self.conn, settings.MIN_KEEP_SCORE, self.cfg.llm.min_keep_score)
        keep = dec.keep and dec.score >= min_keep
        self.conn.execute(
            "UPDATE clips SET llm_json = ?, llm_score = ?, start_rel = ?, end_rel = ?, title = ?"
            " WHERE candidate_id = ?",
            (
                json.dumps(data, ensure_ascii=False),
                dec.score,
                start,
                end,
                dec.title,
                candidate_id,
            ),
        )
        self._set_status(candidate_id, "judged" if keep else "rejected")
        if keep:
            jobs.enqueue(
                self.conn,
                "render",
                {"candidate_id": candidate_id},
                "cpu",
                RENDER_PRIORITY_BASE + (100 - dec.score),
            )

    async def handle_render(self, candidate_id: int, payload: dict[str, Any]) -> None:
        cand = self._candidate(candidate_id)
        clip = self._clip(candidate_id)
        if clip is None or not clip["raw_path"] or clip["start_rel"] is None:
            raise StageError(f"aday #{candidate_id} render için hazır değil", retry=False)
        streamer = self.conn.execute(
            "SELECT layout, facecam_rect FROM streamers WHERE id = ?", (cand["streamer_id"],)
        ).fetchone()
        data = self._llm_json(clip)
        judge = data.get("judge", {})
        fixes = [(f["from"], f["to"]) for f in judge.get("caption_fixes", [])]
        transcript = apply_caption_fixes(self._transcript(clip), fixes)
        words = [(w.w, w.s, w.e) for w in words_from_transcript(transcript)]
        start, end = float(clip["start_rel"]), float(clip["end_rel"])
        t0 = float(data.get("cut", {}).get("t0", cand["win_start"]))
        peak = self.conn.execute(
            "SELECT t FROM signal_seconds WHERE session_id = ? AND t BETWEEN ? AND ?"
            " AND audio_db IS NOT NULL ORDER BY audio_db DESC LIMIT 1",
            (cand["session_id"], math.floor(t0 + start), math.ceil(t0 + end)),
        ).fetchone()
        try:
            facecam = Rect.from_json(streamer["facecam_rect"]) if streamer else None
        except (ValueError, KeyError):
            facecam = None
        raw = Path(clip["raw_path"])
        try:
            res = await render_clip(
                self.cfg.render,
                raw,
                start,
                end,
                words,
                raw.parent,
                layout=streamer["layout"] if streamer else "blur",
                facecam=facecam,
                hook=judge.get("hook"),
                thumb_at=(float(peak[0]) - t0) if peak else None,
                ffmpeg=self.cfg.paths.ffmpeg,
                fonts_dir=self.cfg.paths.fonts_dir,
            )
        except RenderError as exc:
            raise StageError(str(exc)) from exc
        data["render"] = {
            "encoder": res.encoder,
            "duration": round(res.duration, 3),
            "layout": effective_layout(streamer["layout"] if streamer else "blur", facecam),
        }
        self.conn.execute(
            "UPDATE clips SET final_path = ?, thumb_path = ?, llm_json = ? WHERE candidate_id = ?",
            (str(res.final), str(res.thumb), json.dumps(data, ensure_ascii=False), candidate_id),
        )
        self._set_status(candidate_id, "rendered")
        if self.on_rendered is not None:
            try:
                self.on_rendered(int(clip["id"]))
            except Exception as exc:  # auto-publish must never fail the render
                log.warning("after-render hook failed", extra={"error": str(exc)})
