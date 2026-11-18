"""Claude client: structured JSON output, prompt caching, one repair retry, cost tracking
in `llm_usage`, and a monthly budget guard."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from clipper.config import LLMConfig
from clipper.llm.schemas import strict_schema
from clipper.log import get_logger

log = get_logger("llm")
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class Price:
    """USD per 1M tokens."""

    input: float
    output: float
    cache_read: float

    @property
    def cache_write(self) -> float:  # 5-minute cache writes cost 1.25x input
        return self.input * 1.25


# Verified against the Claude API model table (2026-11). Haiku 5.5 prices are for prompts
# up to 100K tokens; our prompts are far below that.
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.10),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20),
    "claude-sonnet-5": Price(2.00, 10.00, 0.20),
    "claude-opus-4-8": Price(5.00, 25.00, 0.50),
}
UNKNOWN_PRICE = Price(5.00, 25.00, 0.50)  # conservative
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """User-facing (Turkish)."""


class LLMRefused(LLMError):
    pass


@dataclass(frozen=True, slots=True)
class LLMResult[T]:
    value: T
    model: str
    usd: float
    input_tokens: int
    output_tokens: int
    cache_read: int
    attempts: int


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int
) -> float:
    p = PRICES.get(model, UNKNOWN_PRICE)
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read * p.cache_read
        + cache_write * p.cache_write
    ) / 1_000_000


def month_spend(conn: sqlite3.Connection, now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time(), UTC)
    start = datetime(dt.year, dt.month, 1, tzinfo=UTC).timestamp()
    row = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) FROM llm_usage WHERE ts >= ?", (start,)
    ).fetchone()
    return float(row[0])


class LLMClient:
    def __init__(
        self, conn: sqlite3.Connection, cfg: LLMConfig, client: anthropic.AsyncAnthropic
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.client = client

    def over_budget(self) -> bool:
        return month_spend(self.conn) >= self.cfg.monthly_budget_usd

    def judge_model(self) -> tuple[str, bool]:
        """Judge model, downgraded to the prefilter model when the monthly budget is spent.
        Returns (model, downgraded)."""
        if self.over_budget():
            log.warning(
                "aylık LLM bütçesi aşıldı; judge %s modeline düşürüldü", self.cfg.prefilter_model
            )
            return self.cfg.prefilter_model, True
        return self.cfg.judge_model, False

    async def structured(
        self,
        *,
        purpose: str,
        model: str,
        system: str,
        user: str | list[dict[str, Any]],
        schema: type[T],
        effort: str,
        max_tokens: int = 16000,
    ) -> LLMResult[T]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        total = {"usd": 0.0, "in": 0, "out": 0, "cr": 0}
        last_error = ""
        for attempt in (1, 2):
            text, served_by = await self._call(
                purpose, model, system, messages, schema, effort, max_tokens, total
            )
            try:
                value = schema.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)[:1500]
                log.warning("LLM output invalid", extra={"purpose": purpose, "attempt": attempt})
                messages += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            "Cevabın şemaya uymadı ya da kurallara aykırı. Hata:\n"
                            f"{last_error}\nLütfen düzeltilmiş JSON'u yeniden ver."
                        ),
                    },
                ]
                continue
            return LLMResult(
                value, served_by, total["usd"], total["in"], total["out"], total["cr"], attempt
            )
        raise LLMError(f"LLM iki denemede de geçerli çıktı vermedi: {last_error[:300]}")

    async def _call(
        self,
        purpose: str,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        effort: str,
        max_tokens: int,
        total: dict[str, Any],
    ) -> tuple[str, str]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": strict_schema(schema)},
            },
        }
        try:
            if self.cfg.refusal_fallback and model in FALLBACK_MODELS:
                resp = await self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic API anahtarı geçersiz (ANTHROPIC_API_KEY).") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Model bulunamadı: {model} (config: llm.*_model).") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Claude isteği reddetti: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Claude API'ye bağlanılamadı.") from exc

        served_by = str(getattr(resp, "model", model) or model)
        u = resp.usage
        cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        usd = cost_usd(served_by, u.input_tokens, u.output_tokens, cache_read, cache_write)
        total["usd"] += usd
        total["in"] += u.input_tokens
        total["out"] += u.output_tokens
        total["cr"] += cache_read
        self.conn.execute(
            "INSERT INTO llm_usage (ts, model, purpose, input_tokens, output_tokens,"
            " cache_read, usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                served_by,
                purpose,
                u.input_tokens,
                u.output_tokens,
                cache_read,
                round(usd, 6),
            ),
        )
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            raise LLMRefused(f"Claude bu adayı değerlendirmeyi reddetti ({cat or 'kategori yok'}).")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Claude cevabı max_tokens sınırında kesildi.")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        return text, served_by
