"""ContentScanner (ROADMAP 4.3b): every minute, the last 90 s of transcript goes to Haiku,
which says whether something clip-worthy is being *said* (a story, a joke, a rant...).
The same class serves the live path (Faz 6) and the VOD batch mode; only the time source
differs. `ContentAccumulator` joins consecutive high-scoring windows into one candidate.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass

from clipper.config import ContentScanConfig
from clipper.content.transcript import TLine, speech_ratio, window_text
from clipper.detect.merge import Draft
from clipper.llm.client import LLMBudgetExceeded, LLMClient, LLMRefused
from clipper.llm.schemas import ContentScanDecision
from clipper.log import get_logger
from clipper.models import ChatMessage

log = get_logger("content")

CONTENT_SYSTEM = """\
Sen canlı yayınlardan kısa dikey klip çıkaran bir ekibin "içerik" editörüsün. Sana bir \
yayının son ~90 saniyesinin transkripti (saniye ofsetli), aynı aralıkta chat'ten örnek \
mesajlar ve chat hızı özeti veriliyor. Chat sakin olabilir; senin işin chat'in kaçırdığı \
ama SÖYLENENLER yüzünden klip olmaya değer anları bulmak.

Değerli olanlar: kendi başına anlaşılır, sonu olan kısa bir hikâye (story); gerçekten \
komik bir laf, espri ya da taklit (joke); tutkulu, eğlenceli bir sitem/kızgınlık (rant); \
biriyle atışma, tartışma (argument); bir olaya güçlü sözlü tepki (reaction); ilginç, \
şaşırtıcı bir bilgi ya da itiraf (info).
Değersiz olanlar: oyun içi rutin konuşma, chat'e selam/abone/bağış teşekkürü, reklam veya \
sponsor okuması, yayın başı/sonu konuşmaları, yarım kalmış ya da önceki bağlamı bilmeden \
anlaşılmayan konuşma, uzun sessizlik. Bunlara düşük puan ver ve kind="none" kullan.

Puan (score, 0-100): 0-40 değersiz, 40-70 zayıf/orta, 70-85 iyi, 85+ çok iyi. Emin \
değilsen düşük ver; burada fazla aday üretmek pahalıdır. Konuşma yarıda kesiliyorsa \
(hikâye pencerenin sonunda devam ediyorsa) yine puanla, best_end'i pencerenin sonuna koy.
best_start ve best_end: pencere başına göre saniye; anın asıl başladığı ve bittiği yer.
reason: Türkçe, tek cümle, ne söylendiğini özetle. Transkriptte olmayan şey uydurma."""


def content_user(
    streamer: str,
    category: str | None,
    window_seconds: float,
    transcript: str,
    chat_samples: Sequence[str],
    chat_rate: float,
    chat_rate_baseline: float | None,
) -> str:
    samples = "\n".join(f"- {m}" for m in chat_samples) or "(chat yok)"
    base = f", yayın ortalaması {chat_rate_baseline:.1f}" if chat_rate_baseline else ""
    return (
        f"Yayıncı: {streamer}\nKategori: {category or 'bilinmiyor'}\n"
        f"Pencere: {window_seconds:.0f} sn\n"
        f"Chat hızı: {chat_rate:.1f} mesaj/sn{base}\n\n"
        f"## Transkript\n{transcript or '(konuşma yok)'}\n\n## Chat örnekleri\n{samples}"
    )


BUDGET_REASON = "bütçe doldu"


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    win_start: float
    win_end: float
    skipped: bool
    score: int | None = None
    kind: str | None = None
    reason: str | None = None
    best_start: float | None = None  # absolute (session time)
    best_end: float | None = None
    usd: float = 0.0


def chat_samples(messages: Sequence[ChatMessage], a: float, b: float, n: int) -> list[str]:
    """Up to `n` distinct, evenly spread chat lines from [a, b)."""
    inside = [" ".join(m.text.split())[:120] for m in messages if a <= m.ts < b and m.text]
    uniq = list(dict.fromkeys(t for t in inside if t))
    if len(uniq) <= n:
        return uniq
    step = len(uniq) / n
    return [uniq[int(i * step)] for i in range(n)]


class ContentScanner:
    def __init__(
        self,
        cfg: ContentScanConfig,
        llm: LLMClient | None,
        *,
        streamer: str,
        category: str | None,
        chat_rate_baseline: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.llm = llm
        self.streamer = streamer
        self.category = category
        self.chat_rate_baseline = chat_rate_baseline

    def is_silent(self, lines: Sequence[TLine], a: float, b: float) -> bool:
        return 1.0 - speech_ratio(lines, a, b) > self.cfg.skip_if_silent_ratio

    async def scan(
        self,
        lines: Sequence[TLine],
        chat: Sequence[ChatMessage],
        win_start: float,
        win_end: float,
    ) -> ScanOutcome:
        if self.is_silent(lines, win_start, win_end):
            return ScanOutcome(win_start, win_end, skipped=True)
        if self.llm is None:
            raise RuntimeError("içerik taraması için LLM istemcisi yok (ANTHROPIC_API_KEY)")
        n_chat = sum(1 for m in chat if win_start <= m.ts < win_end)
        user = content_user(
            self.streamer,
            self.category,
            win_end - win_start,
            window_text(lines, win_start, win_end),
            chat_samples(chat, win_start, win_end, self.cfg.chat_samples),
            n_chat / max(win_end - win_start, 1.0),
            self.chat_rate_baseline,
        )
        try:
            res = await self.llm.structured(
                purpose="content_scan",
                model=self.cfg.model,
                system=CONTENT_SYSTEM,
                user=user,
                schema=ContentScanDecision,
                effort="low",
                max_tokens=1000,
            )
        except LLMRefused as exc:
            log.warning("content scan refused", extra={"t0": win_start, "error": str(exc)})
            return ScanOutcome(win_start, win_end, skipped=True, reason="reddedildi")
        except LLMBudgetExceeded as exc:
            log.warning("content scan skipped: budget", extra={"t0": win_start, "error": str(exc)})
            return ScanOutcome(win_start, win_end, skipped=True, reason=BUDGET_REASON)
        d = res.value
        length = win_end - win_start
        bs = min(max(d.best_start, 0.0), length)
        be = min(max(d.best_end, bs), length)
        if be - bs < 1.0:  # no usable span: take the whole window
            bs, be = 0.0, length
        return ScanOutcome(
            win_start,
            win_end,
            skipped=False,
            score=d.score,
            kind=d.kind,
            reason=d.reason,
            best_start=win_start + bs,
            best_end=win_start + be,
            usd=res.usd,
        )


def record_scan(conn: sqlite3.Connection, session_id: int, o: ScanOutcome) -> None:
    if o.skipped and o.score is None:
        return  # silence: nothing was sent, nothing to calibrate on
    conn.execute(
        "INSERT INTO content_scans (session_id, win_start, win_end, score, kind, reason,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, o.win_start, o.win_end, o.score, o.kind, o.reason, time.time()),
    )


class ContentAccumulator:
    """Turns scan outcomes (in time order) into content candidates. Windows overlap
    (90 s every 60 s), so a story spanning several windows arrives as consecutive high
    scores: those are joined into one candidate, at most `max_candidate_seconds` long."""

    def __init__(self, cfg: ContentScanConfig, threshold: float) -> None:
        self.cfg = cfg
        self.threshold = threshold
        self._open: Draft | None = None

    def _draft(self, o: ScanOutcome) -> Draft:
        assert o.best_start is not None and o.best_end is not None and o.score is not None
        a, b = o.best_start - self.cfg.pad_seconds, o.best_end + self.cfg.pad_seconds
        info = {"score": o.score, "kind": o.kind, "reason": o.reason, "windows": 1}
        return Draft(
            peak_ts=(o.best_start + o.best_end) / 2,
            win_start=max(a, 0.0),
            win_end=min(b, a + self.cfg.max_candidate_seconds),
            det_score=self.threshold * o.score / max(self.cfg.min_score, 1),
            sources=["content"],
            features={"content": info},
            content_score=float(o.score),
            content_reason=o.reason,
        )

    def feed(self, o: ScanOutcome) -> list[Draft]:
        hit = not o.skipped and o.score is not None and o.score >= self.cfg.min_score
        if not hit:
            return self.flush()
        new = self._draft(o)
        cur = self._open
        if cur is None:
            self._open = new
            return []
        room = cur.win_start + self.cfg.max_candidate_seconds
        if new.win_start <= cur.win_end and cur.win_end < room:
            cur.win_end = min(max(cur.win_end, new.win_end), room)
            if new.content_score is not None and new.content_score > (cur.content_score or 0):
                cur.content_score = new.content_score
                cur.content_reason = new.content_reason
                cur.det_score = new.det_score
                cur.features["content"].update(score=o.score, kind=o.kind, reason=o.reason)
            cur.features["content"]["windows"] += 1
            cur.peak_ts = (cur.win_start + cur.win_end) / 2
            return []
        closed = self.flush()
        if new.win_start < (closed[-1].win_end if closed else new.win_start):
            new.win_start = closed[-1].win_end
        self._open = new
        return closed

    def flush(self) -> list[Draft]:
        out = [self._open] if self._open is not None else []
        self._open = None
        return out
