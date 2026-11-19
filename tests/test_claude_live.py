"""Real Anthropic API smoke tests (audit section C / TESTPLAN ONL-08). PAID, opt-in:

    uv run pytest -m claude_live -s

Never part of the default run (`addopts` deselects the marker). Needs ANTHROPIC_API_KEY in
the environment or in `.env` (CLIPPER_ENV can point at another file). Every call goes
through the real `LLMClient`, with its own tiny budget (LIVE_BUDGET_USD) on a throwaway
database, so a mistake cannot spend more than that. Expected cost of one full run: well
under $0.05 (one models list, 4 Haiku calls, 1 Sonnet call).

Each test prints one summary line: purpose, model, attempts, tokens, cost, seconds.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from clipper import db
from clipper.config import Config, LLMConfig, load_secrets
from clipper.content.scanner import ContentScanner
from clipper.content.transcript import TLine, TWord
from clipper.llm.client import LLMClient, LLMResult
from clipper.llm.prompts import (
    PREFILTER_SYSTEM,
    candidate_context,
    judge_system,
    judge_user,
    prefilter_user,
)
from clipper.llm.schemas import JudgeDecision, PrefilterDecision
from clipper.models import ChatMessage

pytestmark = pytest.mark.claude_live

LIVE_BUDGET_USD = 0.25
CFG = Config()

# ~35 s of a Turkish gaming stream: setup, a claim, the punchline, the reaction.
LINES = [
    (0.0, 4.5, "tamam arkadaşlar şimdi bu el kesin alıyoruz bakın"),
    (4.5, 9.0, "rakip üç kişi ama ben tek atarım hepsini söz veriyorum"),
    (9.5, 14.0, "bir dakika bir dakika arkamda kim var ya"),
    (14.0, 18.5, "yok artık el bombasını kendi ayağıma attım"),
    (19.0, 24.0, "sohbet gülmeyin lan ciddiyim bu oyun bozuk"),
    (24.5, 30.0, "neyse bir dahakine kesin tek atarım hepsini"),
    (30.5, 35.0, "kim klip aldı şimdi söyleyin bana"),
]
CHAT_TEXTS = ["KEKW", "AHAHAHA", "kendi ayağına attı", "KEKW KEKW", "klip klip", "efsane"]


def _words(t0: float, t1: float, text: str) -> list[dict[str, Any]]:
    toks = text.split()
    step = (t1 - t0) / len(toks)
    return [
        {"w": w, "s": round(t0 + i * step, 2), "e": round(t0 + (i + 1) * step, 2), "p": 1.0}
        for i, w in enumerate(toks)
    ]


TRANSCRIPT = [{"start": a, "end": b, "text": t, "words": _words(a, b, t)} for a, b, t in LINES]
CHAT = [
    ChatMessage(ts=14.0 + i * 0.4, user_id=f"u{i % 9}", text=CHAT_TEXTS[i % len(CHAT_TEXTS)])
    for i in range(30)
]


@pytest.fixture(scope="module")
def llm(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LLMClient]:
    import anthropic

    env = Path(os.environ.get("CLIPPER_ENV", ".env"))
    key = load_secrets(env if env.is_file() else None).anthropic_api_key
    if key is None:
        pytest.skip("ANTHROPIC_API_KEY yok (.env ya da ortam değişkeni)")
    conn = db.open_db(tmp_path_factory.mktemp("claude_live") / "live.db")
    cfg = LLMConfig(monthly_budget_usd=LIVE_BUDGET_USD, daily_budget_usd=LIVE_BUDGET_USD)
    yield LLMClient(conn, cfg, anthropic.AsyncAnthropic(api_key=key.get_secret_value()))
    conn.close()


def _report(purpose: str, res: LLMResult[Any], seconds: float) -> None:
    print(
        f"\n[claude_live] {purpose}: model={res.model} attempts={res.attempts}"
        f" in={res.input_tokens} out={res.output_tokens} cache_read={res.cache_read}"
        f" usd={res.usd:.5f} sec={seconds:.1f}"
    )


def _usage_rows(conn: sqlite3.Connection, purpose: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM llm_usage WHERE purpose = ?", (purpose,)).fetchall()


def _prefilter(llm: LLMClient, transcript: str, purpose: str) -> LLMResult[PrefilterDecision]:
    user = prefilter_user(
        "TestYayıncı",
        "Counter-Strike 2",
        transcript,
        [{"count": 12, "text": "KEKW"}, {"count": 5, "text": "kendi ayağına attı"}],
        {"z": {"rate": 4.2, "emote": 3.1}, "score": 4.5},
    )
    t = time.monotonic()
    res = asyncio.run(
        llm.structured(
            purpose=purpose,
            model=CFG.llm.prefilter_model,
            system=PREFILTER_SYSTEM,
            user=user,
            schema=PrefilterDecision,
            effort=CFG.llm.prefilter_effort,
            max_tokens=2000,  # same limit as stages.handle_prefilter
        )
    )
    _report(purpose, res, time.monotonic() - t)
    return res


def test_configured_models_are_available(llm: LLMClient) -> None:
    """Free: lists the models this key can use and checks every configured one."""

    async def ids() -> set[str]:
        return {m.id async for m in llm.client.models.list()}

    available = asyncio.run(ids())
    wanted = {CFG.llm.prefilter_model, CFG.llm.judge_model, CFG.content_scan.model}
    print(f"\n[claude_live] models: wanted={sorted(wanted)} missing={sorted(wanted - available)}")
    assert wanted <= available


def test_prefilter_turkish_transcript(llm: LLMClient) -> None:
    text = " ".join(t for _, _, t in LINES)
    res = _prefilter(llm, text, "live_prefilter")
    assert isinstance(res.value.keep, bool) and 0 <= res.value.rough_score <= 100
    assert res.value.reason.strip()
    rows = _usage_rows(llm.conn, "live_prefilter")
    assert rows and rows[-1]["usd"] > 0 and rows[-1]["model"] == res.model


def test_prefilter_empty_transcript(llm: LLMClient) -> None:
    res = _prefilter(llm, "", "live_prefilter_empty")
    assert isinstance(res.value.keep, bool)


def test_judge_turkish_clip(llm: LLMClient) -> None:
    """Audit B-01: the judge must return every field (title, YouTube title/description)."""
    from clipper.llm.prompts import chat_timeline, transcript_lines

    duration = 35.0
    user = judge_user(
        "TestYayıncı",
        "Counter-Strike 2",
        duration,
        transcript_lines(TRANSCRIPT),
        chat_timeline(CHAT, 0.0, duration),
        "(ses verisi yok)",
        [],
        context=candidate_context({"sources": ["reactive"]}),
    )
    model, _ = llm.judge_model()
    t = time.monotonic()
    res = asyncio.run(
        llm.structured(
            purpose="live_judge",
            model=model,
            system=judge_system(CFG.render.min_len, CFG.render.max_len),
            user=user,
            schema=JudgeDecision,
            effort=CFG.llm.judge_effort,
            max_tokens=8000,  # stages.JUDGE_MAX_TOKENS
        )
    )
    _report("live_judge", res, time.monotonic() - t)
    d = res.value
    assert d.title.strip() and d.texts.youtube.title.strip() and d.texts.youtube.description
    assert 0 <= d.start < d.end and 0 <= d.score <= 100
    print(f"[claude_live] judge: keep={d.keep} score={d.score} {d.start}-{d.end} {d.title!r}")


def test_content_scan_window(llm: LLMClient) -> None:
    lines = [
        TLine(a, b, t, tuple(TWord(w["w"], w["s"], w["e"]) for w in _words(a, b, t)))
        for a, b, t in LINES
    ]
    scanner = ContentScanner(CFG.content_scan, llm, streamer="TestYayıncı", category="CS2")
    t = time.monotonic()
    out = asyncio.run(scanner.scan(lines, CHAT, 0.0, 35.0))
    print(
        f"\n[claude_live] content_scan: score={out.score} kind={out.kind}"
        f" span={out.best_start}-{out.best_end} usd={out.usd:.5f}"
        f" sec={time.monotonic() - t:.1f}"
    )
    assert not out.skipped and out.score is not None


def test_total_spend_within_budget(llm: LLMClient) -> None:
    from clipper.llm.client import month_spend

    total = month_spend(llm.conn)
    print(f"\n[claude_live] total usd={total:.5f} (limit {LIVE_BUDGET_USD})")
    assert total <= LIVE_BUDGET_USD
